"""Versioned records and links; each store owns one thread-affine connection."""

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


@dataclass(frozen=True)
class Link:
    id: str
    revision: int
    from_id: str
    to_id: str
    relation: str
    from_revision: int | None
    to_revision: int | None
    explanation: str
    author_id: str
    recorded_at: str


@dataclass(frozen=True)
class Connections:
    links: tuple[Link, ...]
    has_more: bool


_LINK_SELECT = """
    SELECT l.id, v.revision, f.id AS from_id, t.id AS to_id, v.relation,
           v.from_revision, v.to_revision, v.explanation, v.author_id, v.recorded_at
    FROM links l JOIN link_revisions v ON v.link_key = l.link_key
    JOIN records f ON f.record_key = v.from_key
    JOIN records t ON t.record_key = v.to_key
"""


class RevisionConflict(Exception):
    def __init__(self, target_id: str, expected: int, current: int):
        self.target_id = target_id
        self.expected = expected
        self.current = current
        super().__init__(f"{target_id}: expected revision {expected}, current is {current}")


class RecordStore:
    def __init__(self, path: str | Path):
        if sqlite3.sqlite_version_info < (3, 51, 3):
            raise RuntimeError("SQLite 3.51.3+ is required for the WAL-reset fix")
        self._db = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        try:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError(f"Unsupported schema version: {version}")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            if version == 0:
                self._db.executescript(Path(__file__).with_name("schema.sql").read_text())
            if version < 2:
                self._db.executescript(Path(__file__).with_name("links.sql").read_text())
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

    def read_link(self, link_id: str, revision: int | None = None) -> Link:
        row = self._db.execute(
            _LINK_SELECT + " WHERE l.id = ? AND v.revision = COALESCE(?, l.current_revision)",
            (link_id, revision),
        ).fetchone()
        if row is None:
            raise KeyError((link_id, revision))
        return Link(**dict(row))

    def create_link(self, from_id: str, to_id: str, relation: str, *, author_id: str,
                    explanation: str = "", from_revision: int | None = None,
                    to_revision: int | None = None) -> Link:
        link_id = str(uuid4())
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            values = self._link_values(from_id, to_id, relation, explanation,
                                       author_id, from_revision, to_revision)
            values.update(id=link_id, revision=1)
            values["link_key"] = self._db.execute(
                """INSERT INTO links (id, current_revision, from_key, to_key, relation)
                   VALUES (:id, :revision, :from_key, :to_key, :relation)""", values,
            ).lastrowid
            self._append_link(values)
            link = self.read_link(link_id, 1)
        return link

    def revise_link(self, link_id: str, *, expected_revision: int, from_id: str,
                    to_id: str, relation: str, author_id: str, explanation: str = "",
                    from_revision: int | None = None, to_revision: int | None = None) -> Link:
        """Replace all link contents; omitted pins follow record identity."""
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT link_key, current_revision FROM links WHERE id = ?", (link_id,),
            ).fetchone()
            if row is None:
                raise KeyError(link_id)
            if row["current_revision"] != expected_revision:
                raise RevisionConflict(link_id, expected_revision, row["current_revision"])
            values = self._link_values(from_id, to_id, relation, explanation,
                                       author_id, from_revision, to_revision)
            revision = expected_revision + 1
            values.update(link_key=row["link_key"], revision=revision)
            self._append_link(values)
            self._db.execute(
                """UPDATE links SET current_revision = :revision, from_key = :from_key,
                   to_key = :to_key, relation = :relation WHERE link_key = :link_key""", values,
            )
            link = self.read_link(link_id, revision)
        return link

    def connections(self, record_id: str, *, direction: str = "out",
                    relation: str | None = None, limit: int = 100) -> Connections:
        """Current one-hop links, ordered by creation; has_more reports truncation."""
        if direction not in ("out", "in"):
            raise ValueError("direction must be 'out' or 'in'")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer from 1 to 1000")
        key = self._record_key(record_id)
        column = "from_key" if direction == "out" else "to_key"
        parameters: list[object] = [key]
        sql = _LINK_SELECT + f" WHERE l.{column} = ? AND v.revision = l.current_revision"
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

    def _link_values(self, from_id: str, to_id: str, relation: str, explanation: str,
                     author_id: str, from_revision: int | None,
                     to_revision: int | None) -> dict[str, object]:
        for pin in (from_revision, to_revision):
            if pin is not None and (type(pin) is not int or pin < 1):
                raise ValueError("endpoint revisions must be positive integers or None")
        return dict(from_key=self._record_key(from_id), to_key=self._record_key(to_id),
                    relation=relation, explanation=explanation, author_id=author_id,
                    from_revision=from_revision, to_revision=to_revision,
                    recorded_at=datetime.now(UTC).isoformat())

    def _append_link(self, values: dict[str, object]) -> None:
        self._db.execute(
            """INSERT INTO link_revisions (link_key, revision, from_key, to_key, relation,
               from_revision, to_revision, explanation, author_id, recorded_at)
               VALUES (:link_key, :revision, :from_key, :to_key, :relation,
               :from_revision, :to_revision, :explanation, :author_id, :recorded_at)""", values,
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
