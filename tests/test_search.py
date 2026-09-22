from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from premisedb import RecordStore


class SearchTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)

    def create(self, text, kind="Finding", **options):
        return self.db.create(kind, text=text, author_id="alice", **options)

    def test_ranking_matches_any_word_and_breaks_ties_by_creation(self):
        partial = self.create("Utility capacity")
        best = self.create("Utility approval")
        tied = self.create("Utility approval")
        self.create("Copper exports")
        self.assertEqual(self.db.search_records("utility approval"), (best, tied, partial))
        self.assertEqual(self.db.search_records("utility approval", limit=1), (best,))
        self.assertEqual(self.db.search_records("absent"), ())

    def test_kind_filter_applies_before_limit_and_returns_full_records(self):
        self.create("Utility approval", kind="Entity")
        finding = self.create("Utility approval", attributes={"basis": "disclosed"})
        self.assertEqual(self.db.search_records("utility", kind="Finding", limit=1), (finding,))
        self.assertEqual(self.db.search_records("utility", kind="Question"), ())
        result = self.db.search_records("utility", kind="Finding")[0]
        result.attributes["basis"] = "edited locally"
        self.assertEqual(self.db.read(finding.id), finding)
        self.assertEqual(self.db.search_records("disclosed"), ())

    def test_updates_replace_searchable_text_across_connections_and_reopening(self):
        original = self.create("June ramp")
        with RecordStore(self.path) as other:
            self.assertEqual(other.search_records("June"), (original,))
            updated = self.db.update(original.id, text="September ramp", author_id="bob")
            self.assertEqual(other.search_records("June"), ())
            self.assertEqual(other.search_records("September"), (updated,))
        self.db.close()
        with RecordStore(self.path) as reopened:
            self.assertEqual(reopened.search_records("June"), ())
            self.assertEqual(reopened.search_records("September"), (updated,))

    def test_failed_writes_leave_search_results_unchanged(self):
        original = self.create("Original claim")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.update(original.id, text="Rejected claim", author_id="")
        with self.assertRaises(sqlite3.IntegrityError):
            self.create("Rejected claim", kind="Unknown")
        # Interrupt after the UPDATE and its index triggers, before the commit.
        with patch.object(self.db, "read", side_effect=RuntimeError("interrupted write")):
            with self.assertRaises(RuntimeError):
                self.db.update(original.id, text="Rejected claim", author_id="bob")
        self.assertEqual(self.db.read(original.id), original)
        self.assertEqual(self.db.search_records("original"), (original,))
        self.assertEqual(self.db.search_records("rejected"), ())

    def test_plain_text_punctuation_operators_and_unicode(self):
        finding = self.create("Utility approval at the café")
        literal = self.create("AND")
        self.assertEqual(self.db.search_records('"utility", approval?'), (finding,))
        self.assertEqual(self.db.search_records("AND"), (literal,))
        self.assertEqual(self.db.search_records("CAFE\u0301"), (finding,))
        for query in ("", " \n ", '() : * "'):
            with self.subTest(query=query):
                self.assertEqual(self.db.search_records(query), ())

    def test_invalid_inputs(self):
        for query in (None, 42):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.db.search_records(query)
        for options in ({"kind": "Unknown"}, {"limit": 0}, {"limit": 1001}, {"limit": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.db.search_records("utility", **options)
