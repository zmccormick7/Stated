"""Single-record operations; each store owns one thread-affine connection."""

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
    revision: int
    text: str
    attributes: dict[str, object]
    author_id: str
    recorded_at: str


class RevisionConflict(Exception):
    def __init__(self, record_id: str, expected: int, current: int):
        self.record_id = record_id
        self.expected = expected
        self.current = current
        super().__init__(f"{record_id}: expected revision {expected}, current is {current}")


class RecordStore:
    def __init__(self, path: str | Path):
        if sqlite3.sqlite_version_info < (3, 51, 3):
            raise RuntimeError("SQLite 3.51.3+ is required for the WAL-reset fix")
        self._db = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        try:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"Unsupported schema version: {version}")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            if version == 0:
                self._db.executescript(Path(__file__).with_name("schema.sql").read_text())
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def read(self, record_id: str, revision: int | None = None) -> Record:
        """Read current or exact historical contents; missing references raise KeyError."""
        row = self._db.execute(
            """SELECT r.id, r.kind, v.revision, v.text, v.attributes,
                      v.author_id, v.recorded_at
               FROM records r JOIN record_revisions v ON v.record_key = r.record_key
               WHERE r.id = ? AND v.revision = COALESCE(?, r.current_revision)""",
            (record_id, revision),
        ).fetchone()
        if row is None:
            raise KeyError((record_id, revision))
        values = dict(row)
        values["attributes"] = json.loads(values["attributes"])
        return Record(**values)

    def create(self, kind: str, *, text: str, author_id: str,
               attributes: dict[str, object] | None = None) -> Record:
        """Create a stable identity and its first revision in one transaction."""
        encoded = self._encode(attributes)
        record_id = str(uuid4())
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            key = self._db.execute(
                "INSERT INTO records (id, kind, current_revision) VALUES (?, ?, 1)",
                (record_id, kind),
            ).lastrowid
            self._append(key, 1, text, encoded, author_id)
            record = self.read(record_id, 1)
        return record

    def revise(self, record_id: str, *, expected_revision: int, text: str,
               author_id: str, attributes: dict[str, object] | None = None) -> Record:
        """Replace text and attributes; omitted attributes means an empty object."""
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        encoded = self._encode(attributes)
        with self._db:
            # Acquire the writer lock before checking the revision.
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT record_key, current_revision FROM records WHERE id = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                raise KeyError(record_id)
            if row["current_revision"] != expected_revision:
                raise RevisionConflict(record_id, expected_revision, row["current_revision"])
            revision = expected_revision + 1
            self._append(row["record_key"], revision, text, encoded, author_id)
            self._db.execute(
                "UPDATE records SET current_revision = ? WHERE record_key = ?",
                (revision, row["record_key"]),
            )
            record = self.read(record_id, revision)
        return record

    def _append(self, key: int, revision: int, text: str,
                attributes: str, author_id: str) -> None:
        self._db.execute(
            """INSERT INTO record_revisions
               (record_key, revision, text, attributes, author_id, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (key, revision, text, attributes, author_id, datetime.now(UTC).isoformat()),
        )

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
