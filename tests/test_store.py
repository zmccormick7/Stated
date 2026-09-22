from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore


class RecordStoreTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.store = RecordStore(self.path)
        self.addCleanup(self.store.close)

    def test_update_replaces_contents_and_survives_reopening(self):
        original = self.store.create("Finding", text="Ramp in June", author_id="alice",
                                     attributes={"basis": "estimate"})
        before = datetime.now(UTC)
        updated = self.store.update(original.id, text="Ramp in September", author_id="bob")
        self.assertEqual((updated.id, updated.kind), (original.id, "Finding"))
        self.assertEqual((updated.text, updated.attributes, updated.author_id),
                         ("Ramp in September", {}, "bob"))
        self.assertLessEqual(before, datetime.fromisoformat(updated.updated_at))
        self.assertLessEqual(datetime.fromisoformat(updated.updated_at), datetime.now(UTC))
        self.store.close()
        with RecordStore(self.path) as reopened:
            self.assertEqual(reopened.read(original.id), updated)
        with closing(sqlite3.connect(self.path)) as sql:
            self.assertEqual(sql.execute("SELECT count(*) FROM records").fetchone()[0], 1)
            self.assertEqual(sql.execute("SELECT name FROM sqlite_master WHERE name = 'record_revisions'").fetchall(), [])

    def test_last_committed_update_wins(self):
        original = self.store.create("Finding", text="Initial", author_id="alice")
        with RecordStore(self.path) as other:
            other.read(original.id)
            self.store.update(original.id, text="Bob's edit", author_id="bob")
            latest = other.update(original.id, text="Carol's edit", author_id="carol")
        self.assertEqual(self.store.read(original.id), latest)

    def test_failed_write_keeps_current_contents(self):
        original = self.store.create("Finding", text="Valid", author_id="alice")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.update(original.id, text="Rejected", author_id="")
        self.assertEqual(self.store.read(original.id), original)
        self.store.update(original.id, text="Accepted", author_id="bob")

    def test_missing_references_and_invalid_inputs(self):
        with self.assertRaises(KeyError):
            self.store.read("missing")
        with self.assertRaises(KeyError):
            self.store.update("missing", text="x", author_id="alice")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create("Unknown", text="x", author_id="alice")
        for attributes in ([], {1: "coerced key"}, {"score": float("nan")}):
            with self.subTest(attributes=attributes), self.assertRaises(ValueError):
                self.store.create("Finding", text="x", author_id="alice", attributes=attributes)

    def test_migration_keeps_latest_contents_and_removes_history(self):
        path = self.path.with_name("legacy.sqlite")
        with closing(sqlite3.connect(path, isolation_level=None)) as sql:
            sql.executescript("""
                CREATE TABLE records (record_key INTEGER PRIMARY KEY, id TEXT, kind TEXT,
                    current_revision INTEGER, FOREIGN KEY (record_key, current_revision)
                    REFERENCES record_revisions (record_key, revision));
                CREATE TABLE record_revisions (record_key INTEGER REFERENCES records,
                    revision INTEGER, text TEXT, attributes TEXT, author_id TEXT,
                    recorded_at TEXT, PRIMARY KEY (record_key, revision));
                INSERT INTO records VALUES (7, 'existing', 'Finding', 2);
                INSERT INTO record_revisions VALUES
                    (7, 1, 'Old claim', '{}', 'alice', '2026-09-21T00:00:00+00:00'),
                    (7, 2, 'Current claim', '{}', 'bob', '2026-09-22T00:00:00+00:00');
                PRAGMA user_version = 1;
            """)
        with RecordStore(path) as migrated:
            current = migrated.read("existing")
            self.assertEqual((current.text, current.author_id, current.updated_at),
                             ("Current claim", "bob", "2026-09-22T00:00:00+00:00"))
            migrated.update(current.id, text="Another edit", author_id="carol")
        with closing(sqlite3.connect(path)) as sql:
            self.assertEqual(sql.execute("SELECT record_key FROM records").fetchone()[0], 7)
            tables = {row[0] for row in sql.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertEqual(tables, {"records", "links", "sqlite_sequence"})
        with RecordStore(path) as reopened:
            self.assertEqual(reopened.read("existing").text, "Another edit")
