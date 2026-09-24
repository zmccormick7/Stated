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


_SCHEMA_VERSION = 9


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
    records: dict[str, Record]  # The other end of each returned link, by ID.


@dataclass(frozen=True)
class RecordPage:
    records: tuple[Record, ...]
    has_more: bool


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
        expression = self._search_expression(query)
        if kind is not None and kind not in ("Entity", "Source", "Finding", "Question", "Model", "Variable"):
            raise ValueError("kind must be a record kind or None")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        if not expression:
            return ()
        rows = self._db.execute(
            """SELECT r.id, r.kind, r.text, r.attributes, r.author_id, r.updated_at
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
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
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

    def locate_source_text(self, source_id: str, quote: str, *,
                           limit: int = 10) -> tuple[SourceSegment, ...]:
        """Find exact, nonoverlapping occurrences of a quotation as UTF-8 byte ranges."""
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError("quote must be a nonblank string")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
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
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
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

    def connections(self, record_id: str, *, direction: str = "both",
                    relation: str | None = None, limit: int = 100) -> Connections:
        """Current one-hop links and the records they reach, in link creation order.

        has_more reports truncation.
        """
        if direction not in ("out", "in", "both"):
            raise ValueError("direction must be 'out', 'in' or 'both'")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        key = self._record_key(record_id)
        where = {"out": "l.from_key = ?", "in": "l.to_key = ?", "both": "(l.from_key = ? OR l.to_key = ?)"}
        parameters: list[object] = [key, key] if direction == "both" else [key]
        sql = _LINK_SELECT + f" WHERE {where[direction]}"
        if relation is not None:
            sql += " AND l.relation = ?"
            parameters.append(relation)
        rows = self._db.execute(sql + " ORDER BY l.link_key LIMIT ?",
                                (*parameters, limit + 1)).fetchall()
        links = tuple(Link(**dict(row)) for row in rows[:limit])
        # Records are never deleted, so every linked record can be read.
        ids = dict.fromkeys(link.from_id if link.to_id == record_id else link.to_id for link in links)
        found = {row["id"]: self._decode_record(row) for row in self._db.execute(
            """SELECT id, kind, text, attributes, author_id, updated_at FROM records
               WHERE id IN (SELECT value FROM json_each(?))""", (json.dumps(list(ids)),))}
        return Connections(links, len(rows) > limit, {record: found[record] for record in ids})

    def list_records(self, *, kind: str | None = None, author_id: str | None = None,
                     after: str | None = None, limit: int = 100) -> RecordPage:
        """Current records in creation order, optionally filtered; pass the last ID as after to continue."""
        if kind is not None and kind not in ("Entity", "Source", "Finding", "Question", "Model", "Variable"):
            raise ValueError("kind must be a record kind or None")
        if author_id is not None and not isinstance(author_id, str):
            raise ValueError("author_id must be a string or None")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        start = 0 if after is None else self._record_key(after)
        rows = self._db.execute(
            """SELECT id, kind, text, attributes, author_id, updated_at FROM records
               WHERE record_key > ? AND (? IS NULL OR kind = ?) AND (? IS NULL OR author_id = ?)
               ORDER BY record_key LIMIT ?""",
            (start, kind, kind, author_id, author_id, limit + 1),
        ).fetchall()
        return RecordPage(tuple(self._decode_record(row) for row in rows[:limit]), len(rows) > limit)

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
                       author_id, updated_at, id) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (*values, str(uuid4())),
                )

    def _insert_link(self, from_id: str, to_id: str, relation: str, explanation: str, author_id: str) -> Link:
        link_id = str(uuid4())
        self._db.execute(
            """INSERT INTO links (from_key, to_key, relation, explanation,
               author_id, updated_at, id) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (*self._link_values(from_id, to_id, relation, explanation, author_id), link_id),
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
