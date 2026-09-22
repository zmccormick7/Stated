"""Current records and links; each store owns one thread-affine connection."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import json
import re
import sqlite3
import unicodedata
from uuid import uuid4


_SCHEMA_VERSION = 5


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
            for name in ("schema.sql", "links.sql", "search.sql"):
                statement = ""
                for line in Path(__file__).with_name(name).read_text().splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        self._db.execute(statement)
                        statement = ""
                if statement.strip():
                    raise ValueError(f"Incomplete schema statement in {name}")
            for direction, column in (("out", "from_key"), ("in", "to_key")):
                self._db.execute(f"CREATE INDEX links_{direction} ON links ({column}, relation, link_key)")
                self._db.execute(f"CREATE INDEX links_{direction}_all ON links ({column}, link_key)")
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
        record_id = str(uuid4())
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                """INSERT INTO records (id, kind, text, attributes, author_id, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (record_id, kind, text, encoded, author_id, datetime.now(UTC).isoformat()),
            )
            record = self.read(record_id)
        return record

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
