"""Current records; each store owns one thread-affine connection."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import json
import sqlite3
from uuid import uuid4


@dataclass(frozen=True)
class Record:
    id: str
    kind: str
    text: str
    attributes: dict[str, object]
    author_id: str
    updated_at: str


class RecordStore:
    def __init__(self, path: str | Path):
        if sqlite3.sqlite_version_info < (3, 51, 3):
            raise RuntimeError("SQLite 3.51.3+ is required for the WAL-reset fix")
        self._db = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        try:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 3):
                raise ValueError(f"Unsupported schema version: {version}")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            if version < 3:
                self._db.execute("PRAGMA foreign_keys = OFF")
                self._initialize()
            self._db.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            self._db.close()
            raise

    def _initialize(self) -> None:
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version == 3:
                return
            if version == 1:
                self._db.execute("ALTER TABLE records RENAME TO old_records")
            self._db.execute(Path(__file__).with_name("schema.sql").read_text())
            if version == 1:
                self._db.execute("""
                    INSERT INTO records
                    SELECT r.record_key, r.id, r.kind, v.text, v.attributes,
                           v.author_id, v.recorded_at
                    FROM old_records r JOIN record_revisions v
                    ON v.record_key = r.record_key AND v.revision = r.current_revision
                """)
                self._db.execute("DROP TABLE record_revisions")
                self._db.execute("DROP TABLE old_records")
            self._db.execute("PRAGMA user_version = 3")

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
        values = dict(row)
        values["attributes"] = json.loads(values["attributes"])
        return Record(**values)

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
