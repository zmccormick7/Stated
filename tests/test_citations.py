from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore


class CitationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)
        self.text = "Revenue €5M.\r\nCustomers may defer delivery. Customers may defer delivery again.\n"
        self.source = self.db.ingest_source(description="Filing", document_text=self.text, author_id="a")
        self.other = self.db.ingest_source(description="Call", document_text="Power is the limit.", author_id="a")

    def counts(self):
        with closing(sqlite3.connect(self.path)) as sql:
            return tuple(sql.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("records", "links"))

    def supported_by(self, record_id):
        return [link.to_id for link in self.db.connections(record_id, relation="supported_by").links]

    def test_create_resolves_quotes_and_links_each_source_once(self):
        finding = self.db.create("Finding", text="Revenue may slip.", author_id="agent", attributes={"basis": "disclosed"},
                                 citations=[{"source_id": self.source.id, "quote": "Revenue €5M."},
                                            {"source_id": self.source.id, "quote": "defer delivery again"},
                                            {"source_id": self.other.id, "start_byte": 0, "end_byte": 5}])
        self.assertEqual(finding.attributes, {"basis": "disclosed", "citations": [
            {"source_id": self.source.id, "start_byte": 0, "end_byte": 14, "quote": "Revenue €5M."},
            {"source_id": self.source.id, "start_byte": 60, "end_byte": 80, "quote": "defer delivery again"},
            {"source_id": self.other.id, "start_byte": 0, "end_byte": 5, "quote": "Power"}]})
        for citation in finding.attributes["citations"]:
            self.assertEqual(self.db.read_source_text(citation["source_id"], start_byte=citation["start_byte"],
                                                      end_byte=citation["end_byte"]), citation["quote"])
        self.assertEqual(self.supported_by(finding.id), [self.source.id, self.other.id])
        self.assertEqual(self.db.connections(finding.id).links[0].author_id, "agent")

    def test_ambiguous_quotes_need_a_start_byte(self):
        citation = {"source_id": self.source.id, "quote": "Customers may defer delivery"}
        with self.assertRaisesRegex(ValueError, r"occurs 2 times .*start_byte 16, 46"):
            self.db.create("Finding", text="Deferral", author_id="a", citations=[citation])
        finding = self.db.create("Finding", text="Deferral", author_id="a", citations=[{**citation, "start_byte": 46}])
        self.assertEqual(finding.attributes["citations"][0]["start_byte"], 46)

    def test_errors_name_the_failing_citation_and_suggest_near_matches(self):
        source = self.db.ingest_source(description="10-Q", author_id="a",
                                       document_text="Sales of AI\u2011optimized\nservers \u201cdoubled\u201d.")
        good = {"source_id": source.id, "quote": "servers"}
        records = [{"ref": "a", "kind": "Finding", "text": "Fine", "citations": [good]},
                   {"ref": "b", "kind": "Finding", "text": "Typed", "citations": [
                       good, {"source_id": source.id[:8], "quote": 'AI-optimized servers "doubled"'}]}]
        with self.assertRaises(ValueError) as caught:
            self.db.contribute(author_id="agent", records=records)
        message = str(caught.exception)
        self.assertTrue(message.startswith(f"records[1] (ref 'b'): citations[1]: quote not found in source {source.id}"))
        self.assertIn("the source has 'AI\u2011optimized\\nservers \u201cdoubled\u201d' at start_byte 9", message)
        self.assertEqual(self.db.search_records("Fine Typed"), ())
        with self.assertRaisesRegex(ValueError, r"^citations\[0\]: quote not found in source [-0-9a-f]+$"):
            self.db.create("Finding", text="Absent", author_id="agent",
                           citations=[{"source_id": source.id, "quote": "tripled"}])

    def test_invalid_citations_write_nothing(self):
        before = self.counts()
        for citations, error in (
            ([{"source_id": self.source.id, "quote": "customers may"}], ValueError),  # Exact case only.
            ([{"source_id": self.source.id, "quote": "Revenue", "start_byte": 3}], ValueError),
            ([{"source_id": self.source.id, "start_byte": 0}], ValueError),
            ([{"source_id": self.source.id, "start_byte": 14, "end_byte": 16}], ValueError),  # Whitespace only.
            ([{"source_id": self.source.id, "quote": "Revenue", "page": 1}], ValueError),
            ([{"quote": "Revenue"}], ValueError),
            ({"source_id": self.source.id, "quote": "Revenue"}, ValueError),
            ([{"source_id": "missing", "quote": "Revenue"}], KeyError),
            ([{"source_id": self.source.id, "quote": "Revenue"},
              {"source_id": self.source.id, "quote": "absent"}], ValueError),
        ):
            with self.subTest(citations=citations), self.assertRaises(error):
                self.db.create("Finding", text="Claim", author_id="a", citations=citations)
        with self.assertRaisesRegex(ValueError, "not both"):
            self.db.create("Finding", text="Claim", author_id="a", attributes={"citations": []},
                           citations=[{"source_id": self.source.id, "quote": "Revenue"}])
        self.assertEqual(self.counts(), before)

    def test_update_replaces_citations_and_adds_missing_links(self):
        finding = self.db.create("Finding", text="Claim", author_id="a",
                                 citations=[{"source_id": self.source.id, "quote": "Revenue"}])
        updated = self.db.update(finding.id, text="Revised claim", author_id="b", attributes={"basis": "inference"},
                                 citations=[{"source_id": self.other.id, "quote": "limit"},
                                            {"source_id": self.source.id, "quote": "Revenue"}])
        self.assertEqual([c["quote"] for c in updated.attributes["citations"]], ["limit", "Revenue"])
        self.assertEqual(updated.attributes["basis"], "inference")
        self.assertEqual(self.supported_by(finding.id), [self.source.id, self.other.id])
        before = self.counts()
        with self.assertRaises(ValueError):
            self.db.update(finding.id, text="Bad", author_id="c", citations=[{"source_id": self.other.id, "quote": "x"}])
        self.assertEqual((self.db.read(finding.id), self.counts()), (updated, before))
        plain = self.db.update(finding.id, text="No citation argument", author_id="c")
        self.assertEqual((plain.attributes, len(self.supported_by(finding.id))), ({}, 2))


if __name__ == "__main__":
    unittest.main()
