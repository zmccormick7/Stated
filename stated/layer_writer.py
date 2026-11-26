"""Write a published layer file directly, a document at a time.

Exporting a workspace's layer reads all of it into memory, which suits a layer an agent built. A layer made by
a script from a large set of documents is written here instead: nothing is held but the document being written.
The file is the same kind of file an export gives, one sealed layer with its manifest, so it is read and
attached the same way.

The caller gives every record its ID. A later version of the layer built from the same inputs then keeps the
same IDs, so a workspace's links and citations to its records stay valid.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from .chunking import split_ranges
from .layers import LayerExport, digest
from .store import RecordStore, _date, _refuse_refs


class LayerWriter:
    def __init__(self, path: str | Path, *, name: str, version: str, author_id: str, written: str | None = None):
        """Start a layer file at `path`, which must not exist. `written` is the time every row is stamped with
        (default: now); giving it makes the same inputs produce the same file."""
        if not isinstance(version, str) or not version.strip():
            raise ValueError("version must be a nonblank string")
        if not isinstance(author_id, str) or not author_id.strip():
            raise ValueError("author_id must be a nonblank string")
        self.path = Path(path)
        if self.path.exists():
            raise ValueError(f"{self.path} already exists; published files are never overwritten")
        self._written = written or datetime.now(UTC).isoformat()
        if _date(self._written[:10]) is None:
            raise ValueError("written must be a time that starts with a date, like 2026-11-25T00:00:00+00:00")
        self._name, self._version, self._author = name, version, author_id
        self._store = RecordStore(self.path, layer=name)
        self._db = self._store._db
        # The file is built once and discarded if the build fails, so each commit need not reach the disk.
        self._db.execute("PRAGMA synchronous = OFF")
        self._sources = self._withheld = 0

    def _insert(self, record_id: str, kind: str, text: str, attributes: dict | None, as_of: str) -> int:
        try:
            usual = isinstance(record_id, str) and str(UUID(record_id)) == record_id
        except ValueError:
            usual = False
        if not usual:
            raise ValueError(f"a record ID must be a UUID in its usual lowercase form, not {record_id!r}")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("a record needs text")
        _refuse_refs(text)
        return self._db.execute(
            """INSERT INTO records (id, kind, text, attributes, author_id, created_by, updated_at, as_of, layer_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (record_id, kind, text, self._store._encode(attributes), self._author, self._author, self._written, as_of)).lastrowid

    def entity(self, record_id: str, text: str, attributes: dict[str, object] | None = None) -> None:
        """Add an Entity, such as a company. Entities are reference data: a read at any date sees them."""
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._insert(record_id, "Entity", text, attributes, self._written[:10])

    def source(self, record_id: str, description: str, document_text: str, *, attributes: dict[str, object],
               about: Iterable[str] = (), ranges: Iterable[tuple[int, int]] | None = None,
               vectors: Iterable[bytes] | None = None, model: str | None = None) -> None:
        """Add a document: a Source with its text, linked `about` each record named, split into passages, each
        with its embedding when `vectors` (one a passage, of `model`) are given.

        `attributes.published` is required: it is the date from which a reader sees the document and its links.
        `ranges` are the passages' byte ranges; left out, the text is split as `chunk` splits it.
        """
        published = attributes.get("published") if isinstance(attributes, dict) else None
        if not isinstance(published, str) or _date(published[:10]) is None:
            raise ValueError("a document needs attributes.published, the date it became public")
        if not isinstance(document_text, str) or not document_text:
            raise ValueError("document_text must be a nonempty string")
        content = document_text.encode("utf-8")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            key = self._insert(record_id, "Source", description, attributes, published[:10])
            self._db.execute("INSERT INTO source_content (source_key, text_utf8, text_sha256) VALUES (?, ?, ?)",
                             (key, content, sha256(content).hexdigest()))
            for other in dict.fromkeys(about):
                found = self._db.execute("SELECT record_key FROM records WHERE id = ?", (other,)).fetchone()
                if found is None:
                    raise ValueError(f"{record_id} is about {other}, which has not been written")
                # A link's ID follows from its ends, so it too is the same in every version.
                self._db.execute(
                    """INSERT INTO links (id, from_key, to_key, relation, explanation, author_id, created_by,
                       updated_at, as_of, layer_key) VALUES (?, ?, ?, 'about', '', ?, ?, ?, ?, 1)""",
                    (str(uuid5(NAMESPACE_URL, f"stated:link:{record_id}:about:{other}")), key, found[0],
                     self._author, self._author, self._written, published[:10]))
        self._store.set_passages(record_id, split_ranges(document_text) if ranges is None else ranges)
        if vectors is not None:
            if not isinstance(model, str):
                raise ValueError("vectors need the name of the model that made them")
            self._store.set_passage_vectors(record_id, vectors, model=model)
        self._sources += 1

    def seal(self) -> LayerExport:
        """Finish the file: the layer becomes read-only and its manifest is written."""
        records, links = (self._db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("records", "links"))
        content_sha256 = digest(self._db, 1)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute("UPDATE layers SET writable = 0, version = ?, content_sha256 = ?, exported_at = ?",
                             (self._version, content_sha256, self._written))
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._store.close()
        return LayerExport(str(self.path), self._name, self._version, records, links, self._sources, 0, content_sha256)
