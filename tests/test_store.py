from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from premisedb import RecordStore, RevisionConflict


class RecordStoreTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.store = RecordStore(self.path)
        self.addCleanup(self.store.close)

    def test_revisions_survive_reopening(self):
        original = self.store.create("Finding", text="Ramp in June", author_id="alice",
                                     attributes={"basis": "estimate", "units": None})
        updated = self.store.revise(original.id, expected_revision=1,
                                    text="Ramp in September", author_id="bob")
        self.store.close()
        with RecordStore(self.path) as reopened:
            self.assertEqual(reopened.read(original.id, 1), original)
            self.assertEqual(reopened.read(original.id), updated)
        self.assertEqual((updated.id, updated.kind, updated.revision),
                         (original.id, "Finding", 2))
        self.assertEqual(updated.attributes, {})
        self.assertEqual(updated.author_id, "bob")
        self.assertIsNotNone(datetime.fromisoformat(updated.recorded_at).tzinfo)

    def test_concurrent_edits_have_one_winner(self):
        original = self.store.create("Finding", text="Initial", author_id="alice")
        barrier = Barrier(2)

        def edit(author):
            with RecordStore(self.path) as other:
                seen = other.read(original.id)
                barrier.wait(timeout=5)
                try:
                    return other.revise(seen.id, expected_revision=seen.revision,
                                        text=author, author_id=author)
                except RevisionConflict as error:
                    return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, ["bob", "carol"]))
        conflicts = [r for r in results if isinstance(r, RevisionConflict)]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual((conflicts[0].expected, conflicts[0].current), (1, 2))
        self.assertEqual(conflicts[0].target_id, original.id)
        winner = next(r for r in results if not isinstance(r, RevisionConflict))
        self.assertEqual(self.store.read(original.id), winner)
        self.assertEqual(self.store.read(original.id, 1), original)
        with self.assertRaises(KeyError):
            self.store.read(original.id, 3)

    def test_failed_writes_roll_back_and_connection_remains_usable(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create("Finding", text="Rejected", author_id="")
        with closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM records").fetchone()[0], 0)
        original = self.store.create("Finding", text="Valid", author_id="alice")
        # Fail after the new revision is inserted, before its head is advanced.
        with closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            connection.executescript("""
                CREATE TRIGGER fail_head BEFORE UPDATE ON records BEGIN
                    SELECT RAISE(ABORT, 'injected failure');
                END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.revise(original.id, expected_revision=1,
                              text="Rejected", author_id="bob")
        self.assertEqual(self.store.read(original.id), original)
        with self.assertRaises(KeyError):
            self.store.read(original.id, 2)
        with closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            connection.execute("DROP TRIGGER fail_head")
        updated = self.store.revise(original.id, expected_revision=1,
                                    text="Accepted", author_id="bob")
        self.assertEqual(updated.revision, 2)

    def test_history_cannot_be_overwritten_or_deleted(self):
        self.store.create("Entity", text="Supplier A", author_id="alice")
        with closing(sqlite3.connect(self.path, isolation_level=None)) as connection:
            for sql in ("UPDATE record_revisions SET text = 'changed'",
                        "DELETE FROM record_revisions"):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql)

    def test_missing_references_and_invalid_inputs(self):
        with self.assertRaises(KeyError):
            self.store.read("missing")
        with self.assertRaises(KeyError):
            self.store.revise("missing", expected_revision=1, text="x", author_id="alice")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create("Unknown", text="x", author_id="alice")
        for attributes in ([], {1: "coerced key"}, {"score": float("nan")}):
            with self.subTest(attributes=attributes), self.assertRaises(ValueError):
                self.store.create("Finding", text="x", author_id="alice", attributes=attributes)


if __name__ == "__main__":
    unittest.main()
