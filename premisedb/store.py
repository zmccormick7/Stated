"""Current records and links; each store owns one thread-affine connection."""

from dataclasses import dataclass
from collections.abc import Iterable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import json
import math
import re
import sqlite3
import unicodedata
from uuid import uuid4

from .chunking import split_ranges


_SCHEMA_VERSION = 11


@dataclass(frozen=True)
class Record:
    id: str
    kind: str
    text: str
    attributes: dict[str, object]
    author_id: str  # Last editor.
    created_by: str  # The record's creator; updates never change it.
    updated_at: str


@dataclass(frozen=True)
class Link:
    id: str
    from_id: str
    to_id: str
    relation: str
    explanation: str
    author_id: str  # Last editor.
    created_by: str
    updated_at: str


@dataclass(frozen=True)
class Connections:
    links: tuple[Link, ...]
    has_more: bool
    records: dict[str, Record]  # The other end of each returned link, by ID.


@dataclass(frozen=True)
class Reached:
    record: Record
    depth: int  # 0 for a seed.
    path: tuple[Link, ...]  # Links from a seed to this record, in traversal order.


@dataclass(frozen=True)
class Expansion:
    reached: tuple[Reached, ...]  # Seeds first, then breadth-first; each record once.
    capped: tuple[str, ...]  # Records with more matching links than per_record_limit.
    has_more: bool  # The overall limit stopped the walk.


@dataclass(frozen=True)
class RecordPage:
    records: tuple[Record, ...]
    has_more: bool


@dataclass(frozen=True)
class Citation:
    record: Record  # The citing record's current contents.
    source_id: str
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class CitationPage:
    citations: tuple[Citation, ...]
    has_more: bool


@dataclass(frozen=True)
class RelationUsage:
    relation: str
    from_kind: str
    to_kind: str
    links: int


@dataclass(frozen=True)
class Contribution:
    records: dict[str, Record]  # Keyed by the caller's ref.
    links: tuple[Link, ...]  # Explicit links only; citation links are not repeated here.


@dataclass(frozen=True)
class Passage:
    source_id: str
    ordinal: int
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class DocumentContext:
    source_id: str
    title: str
    summary: str
    author_id: str
    updated_at: str
    provenance: dict[str, object]

    @property
    def header(self) -> str:
        return f"{self.title}\n{self.summary}"


@dataclass(frozen=True)
class SourceSegment:
    source_id: str
    start_byte: int
    end_byte: int
    text: str
    document_context: DocumentContext | None = None


KINDS = ("Entity", "Source", "Finding", "Question", "Model", "Variable")


def _check_limit(value: int, name: str = "limit") -> None:
    if type(value) is not int or not 1 <= value <= 1000:
        raise ValueError(f"{name} must be an integer from 1 to 1000")


def _check_kind(kind: str | None) -> None:
    if kind is not None and kind not in KINDS:
        raise ValueError("kind must be a record kind or None")


_LINK_SELECT = """
    SELECT l.id, f.id AS from_id, t.id AS to_id, l.relation,
           l.explanation, l.author_id, l.created_by, l.updated_at
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
            "SELECT id, kind, text, attributes, author_id, created_by, updated_at FROM records WHERE id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise KeyError(record_id)
        return self._decode_record(row)

    def resolve_id(self, value: str, *, link: bool = False) -> str:
        """Return the full record ID, or link ID, for an exact ID or a unique prefix of 8+ characters."""
        table = "links" if link else "records"
        if not isinstance(value, str) or not value:
            raise ValueError("an ID must be a nonempty string")
        if self._db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (value,)).fetchone():
            return value
        if len(value) < 8:
            raise KeyError(value)
        # IDs sharing the prefix sort between it and the prefix with its last character incremented.
        rows = self._db.execute(f"SELECT id FROM {table} WHERE id >= ? AND id < ? ORDER BY id LIMIT 2",
                                (value, value[:-1] + chr(ord(value[-1]) + 1))).fetchall()
        if not rows:
            raise KeyError(value)
        if len(rows) > 1:
            raise ValueError(f"ID prefix {value} matches more than one {'link' if link else 'record'}; "
                             "use more characters")
        return rows[0][0]

    def search_records(self, query: str, *, kind: str | None = None,
                       limit: int = 10) -> tuple[Record, ...]:
        """Match any plain-text query word; return current records in BM25 order."""
        expression = self._search_expression(query)
        _check_kind(kind)
        _check_limit(limit)
        if not expression:
            return ()
        rows = self._db.execute(
            """SELECT r.id, r.kind, r.text, r.attributes, r.author_id, r.created_by, r.updated_at
               FROM records_fts JOIN records r ON r.record_key = records_fts.rowid
               WHERE records_fts MATCH ? AND (? IS NULL OR r.kind = ?)
               ORDER BY bm25(records_fts), r.record_key LIMIT ?""",
            (expression, kind, kind, limit),
        ).fetchall()
        return tuple(self._decode_record(row) for row in rows)

    def search_documents(self, query: str, *, source_ids: Iterable[str] | None = None,
                         limit: int = 10) -> tuple[SourceSegment, ...]:
        """Rank passage headers/bodies; return exact source text with separate context."""
        expression = self._search_expression(query)
        _check_limit(limit)
        source_keys = None
        if source_ids is not None:
            if isinstance(source_ids, (str, bytes)) or not isinstance(source_ids, Iterable):
                raise ValueError("source_ids must be an iterable of strings or None")
            ids = tuple(source_ids)
            if any(not isinstance(source_id, str) for source_id in ids):
                raise ValueError("source_ids must contain only strings")
            source_keys = json.dumps([self._source_key(source_id) for source_id in dict.fromkeys(ids)])
        if not expression or source_keys == "[]":
            return ()
        rows = self._db.execute(
            """SELECT r.id AS source_id, p.start_byte, p.end_byte,
                      c.title, c.summary, c.author_id, c.updated_at, c.provenance
               FROM passages_fts JOIN passages p ON p.passage_key = passages_fts.rowid
               JOIN records r ON r.record_key = p.source_key
               LEFT JOIN document_context c ON c.source_key = p.source_key
               WHERE passages_fts MATCH ?
                 AND (? IS NULL OR p.source_key IN (SELECT value FROM json_each(?)))
               ORDER BY bm25(passages_fts, 0.5, 1.0), p.source_key, p.ordinal LIMIT ?""",
            (expression, source_keys, source_keys, limit),
        ).fetchall()
        # Context was captured with the hits; fixed source ranges survive rechunking.
        return tuple(SourceSegment(
            row["source_id"], row["start_byte"], row["end_byte"],
            self.read_source_text(row["source_id"], start_byte=row["start_byte"],
                                  end_byte=row["end_byte"]),
            self._decode_document_context(row["source_id"], row),
        ) for row in rows)

    def _rse_intervening_passages(self, candidates: tuple[SourceSegment, ...],
                                max_segment_passages: int) -> tuple[SourceSegment, ...]:
        """Read unreturned passages between hits for RSE penalties and token accounting."""
        if not candidates:
            return ()
        anchors = [(s.source_id, s.start_byte, s.end_byte) for s in candidates]
        rows = self._db.execute(
            """WITH anchors AS (
                   SELECT CAST(j.key AS INTEGER) AS hit, p.source_key, p.ordinal,
                          lead(p.ordinal) OVER (
                              PARTITION BY p.source_key ORDER BY p.ordinal) AS next_ordinal
                   FROM json_each(?) j
                   LEFT JOIN records r ON r.id = json_extract(j.value, '$[0]')
                   LEFT JOIN passages p ON p.source_key = r.record_key
                     AND p.start_byte = json_extract(j.value, '$[1]')
                     AND p.end_byte = json_extract(j.value, '$[2]')
               )
               SELECT a.hit, a.ordinal, p.start_byte, p.end_byte
               FROM anchors a LEFT JOIN passages p ON p.source_key = a.source_key
                 AND p.ordinal > a.ordinal AND p.ordinal < a.next_ordinal
                 AND a.next_ordinal - a.ordinal < ?
               ORDER BY a.hit, p.ordinal""",
            (json.dumps(anchors), max_segment_passages),
        ).fetchall()
        if any(row["ordinal"] is None for row in rows):
            raise ValueError("A hit is no longer a current passage; repeat document search")
        # Larger gaps cannot fit the passage limit. Never read outside the hits.
        # Capture locators before body reads; retain the search-time context.
        intervening = []
        for row in rows:
            if row["start_byte"] is not None:
                hit = candidates[row["hit"]]
                intervening.append(SourceSegment(
                    hit.source_id, row["start_byte"], row["end_byte"],
                    self.read_source_text(hit.source_id, start_byte=row["start_byte"], end_byte=row["end_byte"]),
                    hit.document_context,
                ))
        return tuple(intervening)

    def set_document_context(self, source_id: str, *, title: str, summary: str,
                             author_id: str, provenance: dict[str, object] | None = None) -> DocumentContext:
        """Replace document-level enrichment and refresh its passage index atomically."""
        if not isinstance(title, str) or not isinstance(summary, str):
            raise ValueError("title and summary must be strings")
        encoded = self._encode(provenance)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                """INSERT INTO document_context
                   (source_key, title, summary, author_id, updated_at, provenance)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source_key) DO UPDATE SET
                   title = excluded.title, summary = excluded.summary,
                   author_id = excluded.author_id, updated_at = excluded.updated_at,
                   provenance = excluded.provenance""",
                (self._source_key(source_id), title, summary, author_id,
                 datetime.now(UTC).isoformat(), encoded),
            )
            context = self.read_document_context(source_id)
            assert context is not None
        return context

    def read_document_context(self, source_id: str) -> DocumentContext | None:
        """Read current enrichment; an ingested Source without context returns None."""
        row = self._db.execute(
            "SELECT title, summary, author_id, updated_at, provenance FROM document_context WHERE source_key = ?",
            (self._source_key(source_id),),
        ).fetchone()
        return self._decode_document_context(source_id, row)

    def create(self, kind: str, *, text: str, author_id: str,
               attributes: dict[str, object] | None = None,
               citations: Iterable[dict[str, object]] | None = None) -> Record:
        """Create a record; citations are resolved and linked supported_by in the same transaction."""
        attributes, source_ids = self._cite(attributes, citations)
        encoded = self._encode(attributes)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            record = self._insert_record(kind, text, encoded, author_id)
            self._link_sources(record.id, source_ids, author_id)
        return record

    def ingest_source(self, *, description: str, document_text: str, author_id: str,
                      attributes: dict[str, object] | None = None, allow_duplicate: bool = False) -> Record:
        """Atomically create a Source record and its fixed canonical UTF-8 text.

        Text identical to an ingested Source is refused, naming that Source, unless allow_duplicate.
        """
        if not isinstance(document_text, str) or not document_text:
            raise ValueError("document_text must be a nonempty string")
        encoded = self._encode(attributes)
        content = document_text.encode("utf-8")
        digest = sha256(content).hexdigest()
        if type(allow_duplicate) is not bool:
            raise ValueError("allow_duplicate must be a boolean")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")  # The duplicate check and insert share one write lock.
            existing = self._db.execute(
                """SELECT r.id FROM source_content s JOIN records r ON r.record_key = s.source_key
                   WHERE s.text_sha256 = ? ORDER BY s.source_key LIMIT 1""", (digest,)).fetchone()
            if existing and not allow_duplicate:
                raise ValueError(f"identical text is already ingested as Source {existing[0]}; "
                                 "reuse it, or pass allow_duplicate")
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

    def locate_source_text(self, source_id: str, quote: str, *,
                           limit: int = 10) -> tuple[SourceSegment, ...]:
        """Find exact, nonoverlapping occurrences of a quotation as UTF-8 byte ranges."""
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError("quote must be a nonblank string")
        _check_limit(limit)
        needle = quote.encode("utf-8")
        with self._db.blobopen("source_content", "text_utf8", self._source_key(source_id),
                               readonly=True) as content:
            body = content.read()
        # A valid UTF-8 needle can only match whole characters in valid UTF-8 text.
        matches, start = [], body.find(needle)
        while start != -1 and len(matches) < limit:
            matches.append(SourceSegment(source_id, start, start + len(needle), quote))
            start = body.find(needle, start + len(needle))
        return tuple(matches)

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
        _check_limit(limit)
        rows = self._db.execute(
            """SELECT ordinal, start_byte, end_byte FROM passages
               WHERE source_key = ? AND ordinal >= ? ORDER BY ordinal LIMIT ?""",
            (self._source_key(source_id), start_ordinal, limit),
        ).fetchall()
        return tuple(Passage(source_id, **dict(row)) for row in rows)

    def update(self, record_id: str, *, text: str, author_id: str,
               attributes: dict[str, object] | None = None,
               citations: Iterable[dict[str, object]] | None = None) -> Record:
        """Replace contents in place; omitted attributes becomes an empty object.

        Citations replace attributes["citations"] and add missing supported_by links;
        existing links are kept.
        """
        attributes, source_ids = self._cite(attributes, citations)
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
            self._link_sources(record_id, source_ids, author_id)
            record = self.read(record_id)
        return record

    def patch(self, record_id: str, *, author_id: str, text: str | None = None,
              attributes: dict[str, object] | None = None, remove: Iterable[str] = (),
              citations: Iterable[dict[str, object]] | None = None) -> Record:
        """Change only the given text and top-level attributes; the rest of the record is kept.

        The current record is read and rewritten in one transaction, so concurrent patches to
        different attributes are all kept. Removing an absent attribute changes nothing.
        """
        if attributes is not None and not isinstance(attributes, dict):
            raise ValueError("attributes must be a JSON object")
        if isinstance(remove, (str, bytes)):
            raise ValueError("remove must be a list of attribute names")
        remove = tuple(remove)
        if any(not isinstance(key, str) for key in remove):
            raise ValueError("remove must be a list of attribute names")
        if text is None and not attributes and not remove and citations is None:
            raise ValueError("patch needs text, attributes, remove or citations")
        changes, source_ids = self._cite(attributes or {}, citations)
        if set(changes) & set(remove):
            raise ValueError("an attribute cannot be both set and removed")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            current = self.read(record_id)
            merged = {key: value for key, value in current.attributes.items() if key not in remove}
            self._db.execute(
                """UPDATE records SET text = ?, attributes = ?, author_id = ?, updated_at = ?
                   WHERE id = ?""",
                (current.text if text is None else text, self._encode({**merged, **changes}),
                 author_id, datetime.now(UTC).isoformat(), record_id),
            )
            self._link_sources(record_id, source_ids, author_id)
            record = self.read(record_id)
        return record

    def read_link(self, link_id: str) -> Link:
        row = self._db.execute(_LINK_SELECT + " WHERE l.id = ?", (link_id,)).fetchone()
        if row is None:
            raise KeyError(link_id)
        return Link(**dict(row))

    def create_link(self, from_id: str, to_id: str, relation: str, *, author_id: str,
                    explanation: str = "") -> Link:
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            link = self._insert_link(from_id, to_id, relation, explanation, author_id)
        return link

    def contribute(self, *, author_id: str, records: Iterable[dict[str, object]] = (),
                   links: Iterable[dict[str, object]] = ()) -> Contribution:
        """Create records and links in one transaction; links may name new records by ref."""
        records = self._batch(records, "records", {"ref", "kind", "text"}, {"attributes", "citations"})
        links = self._batch(links, "links", {"from_id", "to_id", "relation"}, {"explanation"})
        refs = [record["ref"] for record in records]
        if any(not isinstance(ref, str) or not ref.strip() for ref in refs) or len(set(refs)) != len(refs):
            raise ValueError("record refs must be unique, nonblank strings")
        prepared = []
        for record in records:
            attributes, source_ids = self._cite(record.get("attributes"), record.get("citations"))
            prepared.append((record, self._encode(attributes), source_ids))
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if refs and self._db.execute(f"SELECT 1 FROM records WHERE id IN ({','.join('?' * len(refs))})",
                                         refs).fetchone():
                raise ValueError("record refs must not be existing record IDs")
            created = {}
            for record, encoded, source_ids in prepared:
                created[record["ref"]] = self._insert_record(record["kind"], record["text"], encoded, author_id)
                self._link_sources(created[record["ref"]].id, source_ids, author_id)
            ids = {ref: record.id for ref, record in created.items()}
            made = tuple(self._insert_link(ids.get(link["from_id"], link["from_id"]),
                                           ids.get(link["to_id"], link["to_id"]), link["relation"],
                                           link.get("explanation", ""), author_id) for link in links)
        return Contribution(created, made)

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

    def patch_link(self, link_id: str, *, author_id: str, from_id: str | None = None,
                   to_id: str | None = None, relation: str | None = None,
                   explanation: str | None = None) -> Link:
        """Change only the given link fields in one transaction; the rest of the link is kept."""
        if from_id is None and to_id is None and relation is None and explanation is None:
            raise ValueError("patch_link needs from_id, to_id, relation or explanation")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            current = self.read_link(link_id)
            self._db.execute(
                """UPDATE links SET from_key = ?, to_key = ?, relation = ?, explanation = ?,
                   author_id = ?, updated_at = ? WHERE id = ?""",
                (*self._link_values(current.from_id if from_id is None else from_id,
                                    current.to_id if to_id is None else to_id,
                                    current.relation if relation is None else relation,
                                    current.explanation if explanation is None else explanation,
                                    author_id), link_id),
            )
            link = self.read_link(link_id)
        return link

    def connections(self, record_id: str, *, direction: str = "both",
                    relation: str | None = None, limit: int = 100) -> Connections:
        """Current one-hop links and the records they reach, in link creation order.

        has_more reports truncation.
        """
        self._check_walk(direction, limit)
        links, has_more = self._links_of(record_id, direction, None if relation is None else (relation,), limit)
        # Records are never deleted, so every linked record can be read.
        ids = dict.fromkeys(link.from_id if link.to_id == record_id else link.to_id for link in links)
        found = {row["id"]: self._decode_record(row) for row in self._db.execute(
            """SELECT id, kind, text, attributes, author_id, created_by, updated_at FROM records
               WHERE id IN (SELECT value FROM json_each(?))""", (json.dumps(list(ids)),))}
        return Connections(links, has_more, {record: found[record] for record in ids})

    def expand(self, seed_ids: Iterable[str], *, depth: int = 1, direction: str = "both",
               relations: Iterable[str] | None = None, limit: int = 100,
               per_record_limit: int = 50) -> Expansion:
        """Walk current links breadth-first from seed records, at most two hops, in one snapshot.

        Each record is reported once, at its shortest depth, with the first path found. At most
        per_record_limit links to records not yet reached are followed from any one record; records
        with more are listed as capped.
        """
        if isinstance(seed_ids, (str, bytes)):
            raise ValueError("seed_ids must be a list of record IDs")
        seeds = tuple(dict.fromkeys(seed_ids))
        if not 1 <= len(seeds) <= 100 or any(not isinstance(seed, str) for seed in seeds):
            raise ValueError("seed_ids must contain 1 to 100 record IDs")
        if type(depth) is not int or depth not in (1, 2):
            raise ValueError("depth must be 1 or 2")
        if relations is not None:
            if isinstance(relations, (str, bytes)):
                raise ValueError("relations must be a list of relation names")
            relations = tuple(relations)
            if not relations or any(not isinstance(name, str) or not name.strip() for name in relations):
                raise ValueError("relations must be a nonempty list of relation names")
        self._check_walk(direction, limit)
        _check_limit(per_record_limit, "per_record_limit")
        with self._db:
            self._db.execute("BEGIN")  # One read snapshot for the whole walk.
            reached = {seed: Reached(self.read(seed), 0, ()) for seed in seeds}
            frontier, capped, has_more = list(seeds), [], False
            for level in range(1, depth + 1):
                following = []
                for node in frontier:
                    links, more = self._links_of(node, direction, relations, per_record_limit, exclude=reached)
                    if more:
                        capped.append(node)
                    for link in links:
                        other = link.from_id if link.to_id == node else link.to_id
                        if other in reached:  # Reached earlier in this level.
                            continue
                        if len(reached) - len(seeds) >= limit:
                            has_more = True
                            break
                        reached[other] = Reached(self.read(other), level, reached[node].path + (link,))
                        following.append(other)
                    if has_more:
                        break
                if has_more:
                    break
                frontier = following
        return Expansion(tuple(reached.values()), tuple(capped), has_more)

    @staticmethod
    def _check_walk(direction: str, limit: int) -> None:
        if direction not in ("out", "in", "both"):
            raise ValueError("direction must be 'out', 'in' or 'both'")
        _check_limit(limit)

    def _links_of(self, record_id: str, direction: str, relations: tuple[str, ...] | None,
                  limit: int, exclude: Iterable[str] = ()) -> tuple[tuple[Link, ...], bool]:
        """Links touching a record in creation order, and whether more than limit matched.

        Links whose other end is in exclude are skipped before the limit applies.
        """
        key = self._record_key(record_id)
        where = {"out": "l.from_key = ?", "in": "l.to_key = ?", "both": "(l.from_key = ? OR l.to_key = ?)"}
        parameters: list[object] = [key, key] if direction == "both" else [key]
        sql = _LINK_SELECT + f" WHERE {where[direction]}"
        if relations is not None:
            sql += " AND l.relation IN (SELECT value FROM json_each(?))"
            parameters.append(json.dumps(relations))
        if exclude:
            sql += " AND (CASE WHEN l.from_key = ? THEN t.id ELSE f.id END) NOT IN (SELECT value FROM json_each(?))"
            parameters += [key, json.dumps(list(exclude))]
        rows = self._db.execute(sql + " ORDER BY l.link_key LIMIT ?", (*parameters, limit + 1)).fetchall()
        return tuple(Link(**dict(row)) for row in rows[:limit]), len(rows) > limit

    def list_records(self, *, kind: str | None = None, author_id: str | None = None,
                     created_by: str | None = None, since: str | None = None,
                     where: dict[str, str | int | float | bool | None] | None = None,
                     after: str | None = None, limit: int = 100) -> RecordPage:
        """Current records in creation order, optionally filtered; pass the last ID as after to continue.

        where matches top-level attributes exactly: every named key must be present with that
        JSON scalar value. since keeps records created or edited at or after a UTC ISO timestamp
        or date, such as "2026-09-28" or "2026-09-28T18:00".
        """
        _check_kind(kind)
        for name, value in (("author_id", author_id), ("created_by", created_by), ("since", since)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or None")
        _check_limit(limit)
        if where is not None and not isinstance(where, dict):
            raise ValueError("where must be an object of attribute values")
        conditions, values = [], []
        for key, value in (where or {}).items():
            if not isinstance(key, str) or not key or '"' in key:
                raise ValueError("where keys must be nonempty attribute names without quotes")
            if not (value is None or isinstance(value, (str, int, float, bool))) or (
                    isinstance(value, float) and not math.isfinite(value)):
                raise ValueError("where values must be strings, finite numbers, booleans or null")
            # Match the JSON type as well as the value, so 1, 1.0, true and "1" stay distinct.
            path = f'$."{key}"'
            if value is None or isinstance(value, bool):
                conditions.append("json_type(attributes, ?) = ?")
                values += [path, "null" if value is None else str(value).lower()]
            else:
                conditions.append("json_type(attributes, ?) = ? AND json_extract(attributes, ?) = ?")
                values += [path, {str: "text", int: "integer", float: "real"}[type(value)], path, value]
        start = 0 if after is None else self._record_key(after)
        rows = self._db.execute(
            f"""SELECT id, kind, text, attributes, author_id, created_by, updated_at FROM records
               WHERE record_key > ? AND (? IS NULL OR kind = ?) AND (? IS NULL OR author_id = ?)
                 AND (? IS NULL OR created_by = ?) AND (? IS NULL OR updated_at >= ?)
               {"".join(" AND " + condition for condition in conditions)}
               ORDER BY record_key LIMIT ?""",
            (start, kind, kind, author_id, author_id, created_by, created_by, since, since, *values, limit + 1),
        ).fetchall()
        return RecordPage(tuple(self._decode_record(row) for row in rows[:limit]), len(rows) > limit)

    def cited_by(self, source_id: str, *, start_byte: int = 0, end_byte: int | None = None,
                 limit: int = 100) -> CitationPage:
        """Records whose citations overlap a half-open byte range of a source, in source order."""
        if type(start_byte) is not int or (end_byte is not None and type(end_byte) is not int):
            raise ValueError("byte offsets must be integers")
        _check_limit(limit)
        source_key = self._source_key(source_id)
        length = self._db.execute("SELECT length(text_utf8) FROM source_content WHERE source_key = ?",
                                  (source_key,)).fetchone()[0]
        end_byte = length if end_byte is None else end_byte
        if not 0 <= start_byte < end_byte <= length:
            raise ValueError("byte range is outside source text")
        rows = self._db.execute(
            """SELECT r.id, r.kind, r.text, r.attributes, r.author_id, r.created_by, r.updated_at,
                      c.start_byte AS cited_start, c.end_byte AS cited_end
               FROM citations c JOIN records r ON r.record_key = c.record_key
               WHERE c.source_key = ? AND c.start_byte < ? AND c.end_byte > ?
               ORDER BY c.start_byte, c.end_byte, c.record_key, c.ordinal LIMIT ?""",
            (source_key, end_byte, start_byte, limit + 1),
        ).fetchall()
        citations = tuple(Citation(Record(row["id"], row["kind"], row["text"], json.loads(row["attributes"]),
                                          row["author_id"], row["created_by"], row["updated_at"]),
                                   source_id, row["cited_start"], row["cited_end"]) for row in rows[:limit])
        return CitationPage(citations, len(rows) > limit)

    def relation_usage(self) -> tuple[RelationUsage, ...]:
        """Count current links by relation and the kinds of record they connect."""
        rows = self._db.execute(
            """SELECT l.relation, f.kind AS from_kind, t.kind AS to_kind, count(*) AS links
               FROM links l JOIN records f ON f.record_key = l.from_key
               JOIN records t ON t.record_key = l.to_key
               GROUP BY l.relation, f.kind, t.kind ORDER BY l.relation, links DESC, f.kind, t.kind""")
        return tuple(RelationUsage(**dict(row)) for row in rows)

    def _cite(self, attributes: dict[str, object] | None,
              citations: Iterable[dict[str, object]] | None) -> tuple[dict[str, object] | None, tuple[str, ...]]:
        """Resolve citations to exact byte ranges with their quotes; return attributes and cited sources."""
        if citations is None:
            return attributes, ()
        if attributes is not None and not isinstance(attributes, dict):
            raise ValueError("attributes must be a JSON object")
        if attributes and "citations" in attributes:
            raise ValueError("supply citations as an argument or an attribute, not both")
        if isinstance(citations, (str, bytes, dict)):
            raise ValueError("citations must be a list of objects")
        resolved = []
        for citation in citations:
            if (not isinstance(citation, dict) or "source_id" not in citation
                    or not set(citation) <= {"source_id", "quote", "start_byte", "end_byte"}):
                raise ValueError("each citation needs source_id and a quote or start_byte/end_byte")
            source_id, quote = citation["source_id"], citation.get("quote")
            if isinstance(source_id, str):
                source_id = self.resolve_id(source_id)  # Citations always store the full Source ID.
            start, end = citation.get("start_byte"), citation.get("end_byte")
            if quote is None:
                if start is None or end is None:
                    raise ValueError("a citation without a quote needs start_byte and end_byte")
                quote = self.read_source_text(source_id, start_byte=start, end_byte=end)
                if not quote.strip():
                    raise ValueError("a cited range must contain text")
            else:
                matches = [m for m in self.locate_source_text(source_id, quote, limit=1000)
                           if start in (None, m.start_byte) and end in (None, m.end_byte)]
                if len(matches) != 1:
                    offsets = ", ".join(str(m.start_byte) for m in matches[:10])
                    where = "" if start is None and end is None else " at the given offsets"
                    raise ValueError(f"quote not found in source {source_id}{where}" if not matches else
                                     f"quote occurs {len(matches)} times in source {source_id} "
                                     f"(start_byte {offsets}); add start_byte to choose one")
                start, end = matches[0].start_byte, matches[0].end_byte
            resolved.append({"source_id": source_id, "start_byte": start, "end_byte": end, "quote": quote})
        return {**(attributes or {}), "citations": resolved}, tuple(dict.fromkeys(c["source_id"] for c in resolved))

    def _link_sources(self, record_id: str, source_ids: tuple[str, ...], author_id: str) -> None:
        """Add a supported_by link to each cited source that is not already linked."""
        for source_id in source_ids:
            values = self._link_values(record_id, source_id, "supported_by", "Cited evidence.", author_id)
            if self._db.execute("""SELECT 1 FROM links WHERE from_key = ? AND to_key = ?
                                   AND relation = 'supported_by'""", values[:2]).fetchone() is None:
                self._db.execute(
                    """INSERT INTO links (from_key, to_key, relation, explanation,
                       author_id, updated_at, id, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*values, str(uuid4()), author_id),
                )

    def _insert_link(self, from_id: str, to_id: str, relation: str, explanation: str, author_id: str) -> Link:
        link_id = str(uuid4())
        self._db.execute(
            """INSERT INTO links (from_key, to_key, relation, explanation,
               author_id, updated_at, id, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (*self._link_values(from_id, to_id, relation, explanation, author_id), link_id, author_id),
        )
        return self.read_link(link_id)

    @staticmethod
    def _batch(items: Iterable[dict[str, object]], name: str, required: set[str],
               optional: set[str]) -> list[dict[str, object]]:
        if isinstance(items, (str, bytes, dict)):
            raise ValueError(f"{name} must be a list of objects")
        items = list(items)
        if len(items) > 1000:
            raise ValueError(f"{name} can contain at most 1,000 items")
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not required <= set(item) <= required | optional:
                raise ValueError(f"{name}[{index}] needs {', '.join(sorted(required))}"
                                 f" and may have {', '.join(sorted(optional))}")
        return items

    def _record_key(self, record_id: str) -> int:
        """Key for an exact ID or a unique prefix of 8+ characters, so payload IDs can be short."""
        row = self._db.execute("SELECT record_key FROM records WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            if not isinstance(record_id, str):
                raise KeyError(record_id)
            row = self._db.execute("SELECT record_key FROM records WHERE id = ?",
                                   (self.resolve_id(record_id),)).fetchone()
        return row[0]

    def _source_key(self, source_id: str) -> int:
        query = """SELECT s.source_key FROM source_content s
                   JOIN records r ON r.record_key = s.source_key WHERE r.id = ?"""
        row = self._db.execute(query, (source_id,)).fetchone()
        if row is None and isinstance(source_id, str) and len(source_id) >= 8:
            row = self._db.execute(query, (self.resolve_id(source_id),)).fetchone()
        if row is None:
            raise KeyError(source_id)
        return row[0]

    def _insert_record(self, kind: str, text: str, encoded: str, author_id: str) -> Record:
        record_id = str(uuid4())
        self._db.execute(
            """INSERT INTO records (id, kind, text, attributes, author_id, created_by, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (record_id, kind, text, encoded, author_id, author_id, datetime.now(UTC).isoformat()),
        )
        return self.read(record_id)

    def _link_values(self, from_id: str, to_id: str, relation: str,
                     explanation: str, author_id: str) -> tuple:
        return (self._record_key(from_id), self._record_key(to_id), relation,
                explanation, author_id, datetime.now(UTC).isoformat())

    @staticmethod
    def _decode_document_context(source_id: str, row: sqlite3.Row | None) -> DocumentContext | None:
        if row is None or row["title"] is None:
            return None
        return DocumentContext(source_id, row["title"], row["summary"], row["author_id"],
                               row["updated_at"], json.loads(row["provenance"]))

    @staticmethod
    def _search_expression(query: str) -> str:
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        words = dict.fromkeys(re.findall(r"[^\W_]+", unicodedata.normalize("NFC", query)))
        return " OR ".join(f'"{word}"' for word in words)

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
