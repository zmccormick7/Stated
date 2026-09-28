from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore


class CitedByTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)
        #                                 0         10        20        30        40
        self.source = self.db.ingest_source(description="Filing", author_id="a",
                                            document_text="Backlog rose. Power is scarce. Prices rose again.")
        self.other = self.db.ingest_source(description="Call", author_id="a", document_text="Power is scarce.")

    def cite(self, text, *quotes, source=None):
        return self.db.create("Finding", text=text, author_id="a",
                              citations=[{"source_id": (source or self.source).id, "quote": q} for q in quotes])

    def ranges(self, page):
        return [(c.record.text, c.start_byte, c.end_byte) for c in page.citations]

    def test_overlapping_citations_are_returned_in_source_order(self):
        power = self.cite("Power binds", "Power is scarce.")
        both = self.cite("Backlog and prices", "Prices rose again.", "Backlog rose.")
        self.cite("Other source", "Power is scarce.", source=self.other)
        self.assertEqual(self.ranges(self.db.cited_by(self.source.id)),
                         [("Backlog and prices", 0, 13), ("Power binds", 14, 30), ("Backlog and prices", 31, 49)])
        page = self.db.cited_by(self.source.id, start_byte=20, end_byte=32)
        self.assertEqual(self.ranges(page), [("Power binds", 14, 30), ("Backlog and prices", 31, 49)])
        self.assertEqual((page.citations[0].record, page.citations[0].source_id, page.has_more), (power, self.source.id, False))
        self.assertEqual(self.ranges(self.db.cited_by(self.source.id, start_byte=13, end_byte=14)), [])
        first = self.db.cited_by(self.source.id, limit=1)
        self.assertEqual((self.ranges(first), first.has_more), ([("Backlog and prices", 0, 13)], True))
        self.assertEqual(self.db.cited_by(self.source.id).citations[0].record, both)

    def test_index_follows_update_patch_and_raw_attributes(self):
        finding = self.cite("Claim", "Backlog rose.")
        self.db.patch(finding.id, author_id="b", citations=[{"source_id": self.source.id, "quote": "Prices rose"}])
        self.assertEqual(self.ranges(self.db.cited_by(self.source.id)), [("Claim", 31, 42)])
        self.db.patch(finding.id, author_id="b", attributes={"basis": "inference"})
        self.assertEqual(len(self.db.cited_by(self.source.id).citations), 1)
        self.db.update(finding.id, text="Claim", author_id="b")
        self.assertEqual(self.db.cited_by(self.source.id).citations, ())
        raw = self.db.create("Finding", text="Raw", author_id="a", attributes={"citations": [
            {"source_id": self.source.id, "start_byte": 0, "end_byte": 7},
            {"source_id": "missing", "start_byte": 0, "end_byte": 7},
            {"source_id": self.source.id, "start_byte": 5, "end_byte": 5},
            {"source_id": self.source.id, "start_byte": 0, "end_byte": 999},
            {"source_id": self.source.id, "start_byte": "0", "end_byte": 7}, "not an object"]})
        self.assertEqual(self.ranges(self.db.cited_by(self.source.id)), [("Raw", 0, 7)])
        self.db.create("Finding", text="Object", author_id="a", attributes={"citations": {"source_id": self.source.id}})
        with closing(sqlite3.connect(self.path)) as sql:
            self.assertEqual(sql.execute("SELECT count(*) FROM citations").fetchone()[0], 1)
        self.assertEqual(self.db.read(raw.id).attributes["citations"][5], "not an object")

    def test_invalid_lookups(self):
        for kwargs in ({"start_byte": -1}, {"start_byte": 5, "end_byte": 5}, {"end_byte": 999},
                       {"start_byte": "0"}, {"limit": 0}, {"limit": 1001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.db.cited_by(self.source.id, **kwargs)
        finding = self.db.create("Finding", text="No text", author_id="a")
        for missing in ("missing", finding.id):
            with self.subTest(missing=missing), self.assertRaises(KeyError):
                self.db.cited_by(missing)


if __name__ == "__main__":
    unittest.main()
