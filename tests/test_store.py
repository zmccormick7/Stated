from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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

    def test_incompatible_schema_is_rejected_without_changing_file(self):
        for version in (1, 2, 3, 4, 5, 7):
            with self.subTest(version=version):
                path = self.path.with_name(f"schema-{version}.sqlite")
                with closing(sqlite3.connect(path, isolation_level=None)) as sql:
                    sql.execute("CREATE TABLE existing_data (text TEXT)")
                    sql.execute("INSERT INTO existing_data VALUES ('Keep this')")
                    sql.execute(f"PRAGMA user_version = {version}")
                original = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "Incompatible schema version.*create a new database"):
                    RecordStore(path)
                self.assertEqual(path.read_bytes(), original)

    def test_failed_initialization_rolls_back_and_can_be_retried(self):
        path = self.path.with_name("new.sqlite")
        read_text = Path.read_text

        def fail_after_schema(file):
            return read_text(file) + "\nINVALID SQL;\n"

        with patch.object(Path, "read_text", fail_after_schema):
            with self.assertRaises(sqlite3.OperationalError):
                RecordStore(path)
        with closing(sqlite3.connect(path)) as sql:
            self.assertEqual(sql.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(sql.execute("SELECT name FROM sqlite_master").fetchall(), [])
        with RecordStore(path) as retried:
            record = retried.create("Entity", text="Company", author_id="alice")
            retried.create_link(record.id, record.id, "related_to", author_id="alice")
