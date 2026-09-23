"""Current records and links; each store owns one thread-affine connection."""

from dataclasses import dataclass
from collections.abc import Iterable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import json
import re
import sqlite3
import unicodedata
from uuid import uuid4

from .chunking import split_ranges


_SCHEMA_VERSION = 7


@dataclass(frozen=True)
class Record:
    id: str
    kind: str
    text: str
    attributes: dict[str, object]
    author_id: str
    updated_at: str


@dataclass(frozen=True)
class Link:
    id: str
    from_id: str
    to_id: str
    relation: str
    explanation: str
    author_id: str
    updated_at: str


@dataclass(frozen=True)
class Connections:
    links: tuple[Link, ...]
    has_more: bool


@dataclass(frozen=True)
class Passage:
    source_id: str
    ordinal: int
    start_byte: int
    end_byte: int


_LINK_SELECT = """
    SELECT l.id, f.id AS from_id, t.id AS to_id, l.relation,
           l.explanation, l.author_id, l.updated_at
    FROM links l JOIN records f ON f.record_key = l.from_key
    JOIN records t ON t.record_key = l.to_key
"""


class RecordStore:
    def __init__(self, path: str | Path):
        if sqlite3.sqlite_version_info < (3, 51, 3):
            raise RuntimeError("SQLite 3.51.3+ is required for the WAL-reset fix")
        self._db = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        try:
            version = self._schema_version()
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            self._db.execute("PRAGMA foreign_keys = ON")
            if version == 0:
                self._initialize()
        except BaseException:
            self._db.close()
            raise

    def _schema_version(self) -> int:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, _SCHEMA_VERSION):
            raise ValueError(
                f"Incompatible schema version {version}; expected {_SCHEMA_VERSION}. "
                "Migrations are not supported; create a new database."
            )
        return version

    def _initialize(self) -> None:
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if self._schema_version() == _SCHEMA_VERSION:
                return
            # executescript would commit before running; keep schema creation atomic.
            statement = ""
            for line in Path(__file__).with_name("schema.sql").read_text().splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    self._db.execute(statement)
                    statement = ""
            if statement.strip():
                raise ValueError("Incomplete statement in schema.sql")
            self._db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def close(self) -> None:
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def read(self, record_id: str) -> Record:
        """Read current contents; a missing record raises KeyError."""
        row = self._db.execute(
            "SELECT id, kind, text, attributes, author_id, updated_at FROM records WHERE id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise KeyError(record_id)
        return self._decode_record(row)

    def search_records(self, query: str, *, kind: str | None = None,
                       limit: int = 10) -> tuple[Record, ...]:
        """Match any plain-text query word; return current records in BM25 order."""
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        if kind is not None and kind not in ("Entity", "Source", "Finding", "Question", "Model", "Variable"):
            raise ValueError("kind must be a record kind or None")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        words = dict.fromkeys(re.findall(r"[^\W_]+", unicodedata.normalize("NFC", query)))
        if not words:
            return ()
        expression = " OR ".join(f'"{word}"' for word in words)
        rows = self._db.execute(
            """SELECT r.id, r.kind, r.text, r.attributes, r.author_id, r.updated_at
               FROM records_fts JOIN records r ON r.record_key = records_fts.rowid
               WHERE records_fts MATCH ? AND (? IS NULL OR r.kind = ?)
               ORDER BY bm25(records_fts), r.record_key LIMIT ?""",
            (expression, kind, kind, limit),
        ).fetchall()
        return tuple(self._decode_record(row) for row in rows)

    def create(self, kind: str, *, text: str, author_id: str,
               attributes: dict[str, object] | None = None) -> Record:
        encoded = self._encode(attributes)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            record = self._insert_record(kind, text, encoded, author_id)
        return record

    def ingest_source(self, *, description: str, document_text: str, author_id: str,
                      attributes: dict[str, object] | None = None) -> Record:
        """Atomically create a Source record and its fixed canonical UTF-8 text."""
        if not isinstance(document_text, str) or not document_text:
            raise ValueError("document_text must be a nonempty string")
        encoded = self._encode(attributes)
        content = document_text.encode("utf-8")
        digest = sha256(content).hexdigest()
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            source = self._insert_record("Source", description, encoded, author_id)
            self._db.execute(
                "INSERT INTO source_content (source_key, text_utf8, text_sha256) VALUES (?, ?, ?)",
                (self._record_key(source.id), content, digest),
            )
        return source

    def read_source_text(self, source_id: str, *, start_byte: int = 0,
                         end_byte: int | None = None) -> str:
        """Read canonical text or a half-open UTF-8 byte range, without normalization."""
        if type(start_byte) is not int or (end_byte is not None and type(end_byte) is not int):
            raise ValueError("byte offsets must be integers")
        source_key = self._source_key(source_id)
        with self._db.blobopen("source_content", "text_utf8", source_key, readonly=True) as content:
            end_byte = len(content) if end_byte is None else end_byte
            if not 0 <= start_byte <= end_byte <= len(content):
                raise ValueError("byte range is outside source text")
            for offset in (start_byte, end_byte):
                if offset < len(content):
                    content.seek(offset)
                    if content.read(1)[0] & 0xC0 == 0x80:
                        raise ValueError("byte offsets must be UTF-8 character boundaries")
            content.seek(start_byte)
            return content.read(end_byte - start_byte).decode("utf-8")

    def chunk_source(self, source_id: str, *, max_chars: int = 2000) -> int:
        """Replace the source's passages using the default splitter; return their count."""
        ranges = split_ranges(self.read_source_text(source_id), max_chars=max_chars)
        return self.set_passages(source_id, ranges)

    def set_passages(self, source_id: str, ranges: Iterable[tuple[int, int]]) -> int:
        """Replace passages with a complete, nonoverlapping partition of source bytes."""
        source_key = self._source_key(source_id)
        ranges = tuple((start, end) for start, end in ranges)
        previous_end = 0
        with self._db.blobopen("source_content", "text_utf8", source_key, readonly=True) as content:
            for start, end in ranges:
                if type(start) is not int or type(end) is not int:
                    raise ValueError("passage offsets must be integers")
                if not previous_end == start < end <= len(content):
                    raise ValueError("passages must be ordered, contiguous, nonempty byte ranges")
                if end < len(content):
                    content.seek(end)
                    if content.read(1)[0] & 0xC0 == 0x80:
                        raise ValueError("passage boundaries must be UTF-8 character boundaries")
                previous_end = end
            if previous_end != len(content):
                raise ValueError("passages must cover the entire source text")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute("DELETE FROM passages WHERE source_key = ?", (source_key,))
            self._db.executemany(
                "INSERT INTO passages (source_key, ordinal, start_byte, end_byte) VALUES (?, ?, ?, ?)",
                ((source_key, ordinal, start, end) for ordinal, (start, end) in enumerate(ranges)),
            )
        return len(ranges)

    def read_passages(self, source_id: str, *, start_ordinal: int = 0,
                      limit: int = 100) -> tuple[Passage, ...]:
        """Read current passage locators in source order, starting at an inclusive ordinal."""
        if type(start_ordinal) is not int or start_ordinal < 0:
            raise ValueError("start_ordinal must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        rows = self._db.execute(
            """SELECT ordinal, start_byte, end_byte FROM passages
               WHERE source_key = ? AND ordinal >= ? ORDER BY ordinal LIMIT ?""",
            (self._source_key(source_id), start_ordinal, limit),
        ).fetchall()
        return tuple(Passage(source_id, **dict(row)) for row in rows)

    def update(self, record_id: str, *, text: str, author_id: str,
               attributes: dict[str, object] | None = None) -> Record:
        """Replace contents in place; omitted attributes becomes an empty object."""
        encoded = self._encode(attributes)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            result = self._db.execute(
                """UPDATE records SET text = ?, attributes = ?, author_id = ?, updated_at = ?
                   WHERE id = ?""",
                (text, encoded, author_id, datetime.now(UTC).isoformat(), record_id),
            )
            if result.rowcount == 0:
                raise KeyError(record_id)
            record = self.read(record_id)
        return record

    def read_link(self, link_id: str) -> Link:
        row = self._db.execute(_LINK_SELECT + " WHERE l.id = ?", (link_id,)).fetchone()
        if row is None:
            raise KeyError(link_id)
        return Link(**dict(row))

    def create_link(self, from_id: str, to_id: str, relation: str, *, author_id: str,
                    explanation: str = "") -> Link:
        link_id = str(uuid4())
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                """INSERT INTO links (from_key, to_key, relation, explanation,
                   author_id, updated_at, id) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (*self._link_values(from_id, to_id, relation, explanation, author_id), link_id),
            )
            link = self.read_link(link_id)
        return link

    def update_link(self, link_id: str, *, from_id: str, to_id: str, relation: str,
                    author_id: str, explanation: str = "") -> Link:
        """Replace current link contents and refresh its last-edited timestamp."""
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            result = self._db.execute(
                """UPDATE links SET from_key = ?, to_key = ?, relation = ?, explanation = ?,
                   author_id = ?, updated_at = ? WHERE id = ?""",
                (*self._link_values(from_id, to_id, relation, explanation, author_id), link_id),
            )
            if result.rowcount == 0:
                raise KeyError(link_id)
            link = self.read_link(link_id)
        return link

    def connections(self, record_id: str, *, direction: str = "out",
                    relation: str | None = None, limit: int = 100) -> Connections:
        """Current one-hop links, ordered by creation; has_more reports truncation."""
        if direction not in ("out", "in"):
            raise ValueError("direction must be 'out' or 'in'")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        column = "from_key" if direction == "out" else "to_key"
        parameters: list[object] = [self._record_key(record_id)]
        sql = _LINK_SELECT + f" WHERE l.{column} = ?"
        if relation is not None:
            sql += " AND l.relation = ?"
            parameters.append(relation)
        rows = self._db.execute(sql + " ORDER BY l.link_key LIMIT ?",
                                (*parameters, limit + 1)).fetchall()
        return Connections(tuple(Link(**dict(row)) for row in rows[:limit]), len(rows) > limit)

    def _record_key(self, record_id: str) -> int:
        row = self._db.execute("SELECT record_key FROM records WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise KeyError(record_id)
        return row[0]

    def _source_key(self, source_id: str) -> int:
        row = self._db.execute(
            """SELECT s.source_key FROM source_content s
               JOIN records r ON r.record_key = s.source_key WHERE r.id = ?""",
            (source_id,),
        ).fetchone()
        if row is None:
            raise KeyError(source_id)
        return row[0]

    def _insert_record(self, kind: str, text: str, encoded: str, author_id: str) -> Record:
        record_id = str(uuid4())
        self._db.execute(
            """INSERT INTO records (id, kind, text, attributes, author_id, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (record_id, kind, text, encoded, author_id, datetime.now(UTC).isoformat()),
        )
        return self.read(record_id)

    def _link_values(self, from_id: str, to_id: str, relation: str,
                     explanation: str, author_id: str) -> tuple:
        return (self._record_key(from_id), self._record_key(to_id), relation,
                explanation, author_id, datetime.now(UTC).isoformat())

    @staticmethod
    def _decode_record(row: sqlite3.Row) -> Record:
        values = dict(row)
        values["attributes"] = json.loads(values["attributes"])
        return Record(**values)

    @staticmethod
    def _encode(attributes: dict[str, object] | None) -> str:
        attributes = {} if attributes is None else attributes
        if not isinstance(attributes, dict):
            raise ValueError("attributes must be a JSON object")
        encoded = json.dumps(attributes, allow_nan=False)
        # Reject silent JSON coercions such as integer keys or tuple values.
        if json.loads(encoded) != attributes:
            raise ValueError("attributes must use JSON types and string keys")
        return encoded
