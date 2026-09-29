from contextlib import closing
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from premisedb import Contribution, RecordStore


class ContributeTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)
        self.company = self.db.create("Entity", text="PowerFrame (fictional)", author_id="seed")
        self.source = self.db.ingest_source(description="Filing", author_id="seed",
                                            document_text="Customers may defer deliveries.")

    def counts(self):
        with closing(sqlite3.connect(self.path)) as sql:
            return tuple(sql.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("records", "links"))

    def contribute(self, records=(), links=()):
        return self.db.contribute(author_id="agent", records=records, links=links)

    def test_records_links_and_citations_commit_together(self):
        result = self.contribute(
            records=[{"ref": "f1", "kind": "Finding", "text": "Deliveries can slip.",
                      "attributes": {"basis": "disclosed"},
                      "citations": [{"source_id": self.source.id, "quote": "defer deliveries"}]},
                     {"ref": "q1", "kind": "Question", "text": "Could grid delays trigger deferrals?"}],
            links=[{"from_id": "f1", "to_id": self.company.id, "relation": "about"},
                   {"from_id": "f1", "to_id": "q1", "relation": "raises", "explanation": "Timing risk."}])
        self.assertIsInstance(result, Contribution)
        finding, question = result.records["f1"], result.records["q1"]
        self.assertEqual((finding.author_id, finding.attributes["basis"]), ("agent", "disclosed"))
        self.assertEqual(finding.attributes["citations"][0]["start_byte"], 14)
        self.assertEqual([(l.from_id, l.to_id, l.relation, l.explanation) for l in result.links],
                         [(finding.id, self.company.id, "about", ""), (finding.id, question.id, "raises", "Timing risk.")])
        self.assertEqual([(l.to_id, l.relation) for l in self.db.connections(finding.id).links],
                         [(self.source.id, "supported_by"), (self.company.id, "about"), (question.id, "raises")])
        self.assertEqual(self.db.read(question.id), question)
        self.assertEqual(self.contribute(), Contribution({}, ()))

    def test_text_and_explanations_name_new_records_by_ref(self):
        result = self.db.contribute(author_id="agent", records=[
            {"ref": "f1", "kind": "Finding", "text": "Deferrals raise {ref:q1}; this is {ref:f1}.",
             "attributes": {"note": "{ref:q1} stays literal"}},
            {"ref": "q1", "kind": "Question", "text": "Will deferrals grow?"}],
            links=[{"from_id": "f1", "to_id": "q1", "relation": "raises", "explanation": "See {ref:q1} and {ref:f1}."}])
        finding, question = result.records["f1"], result.records["q1"]
        self.assertEqual(finding.text, f"Deferrals raise {question.id}; this is {finding.id}.")
        self.assertEqual(finding.attributes, {"note": "{ref:q1} stays literal"})  # Attributes are data, kept verbatim.
        self.assertEqual(result.links[0].explanation, f"See {question.id} and {finding.id}.")
        self.assertEqual(self.db.search_records("Deferrals raise", kind="Finding"), (finding,))
        for records, links, where in (
                ([{"ref": "f", "kind": "Finding", "text": "Cites {ref:q_circ}."}], [], "records[0] (ref 'f')"),
                ([{"ref": "f", "kind": "Finding", "text": "Fine"}],
                 [{"from_id": "f", "to_id": self.company.id, "relation": "about", "explanation": "{ref:F}"}], "links[0]")):
            with self.subTest(where=where), self.assertRaisesRegex(ValueError, f"^{re.escape(where)}: {{ref:"):
                self.db.contribute(author_id="agent", records=records, links=links)
        self.assertEqual(self.db.search_records("Cites Fine"), ())

    def test_writes_outside_contribute_refuse_placeholders(self):
        finding = self.db.create("Finding", text="Plain", author_id="agent")
        link = self.db.create_link(finding.id, self.company.id, "about", author_id="agent")
        writes = (lambda: self.db.create("Finding", text="See {ref:q1}", author_id="agent"),
                  lambda: self.db.update(finding.id, text="See {ref:q1}", author_id="agent"),
                  lambda: self.db.patch(finding.id, text="See {ref:q1}", author_id="agent"),
                  lambda: self.db.create_link(self.company.id, finding.id, "about", author_id="agent",
                                              explanation="{ref:q1}"),
                  lambda: self.db.update_link(link.id, from_id=finding.id, to_id=self.company.id, relation="about",
                                              author_id="agent", explanation="{ref:q1}"),
                  lambda: self.db.patch_link(link.id, author_id="agent", explanation="{ref:q1}"))
        for write in writes:
            with self.assertRaisesRegex(ValueError, "resolved only in contribute"):
                write()
        self.assertEqual((self.db.read(finding.id), self.db.read_link(link.id)), (finding, link))

    def test_any_failure_rolls_back_the_whole_contribution(self):
        records = [{"ref": "f1", "kind": "Finding", "text": "Claim"}]
        before = self.counts()
        for kwargs, error in (
            ({"records": records, "links": [{"from_id": "f1", "to_id": "missing", "relation": "about"}]}, KeyError),
            ({"records": records, "links": [{"from_id": "f1", "to_id": "f1", "relation": " "}]}, sqlite3.IntegrityError),
            ({"records": records + [{"ref": "x", "kind": "Company", "text": "Bad kind"}]}, sqlite3.IntegrityError),
            ({"records": records + [{"ref": "c", "kind": "Finding", "text": "Cited",
                                     "citations": [{"source_id": self.source.id, "quote": "absent"}]}]}, ValueError),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(error):
                self.contribute(**kwargs)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.contribute(author_id=" ", records=records)
        self.assertEqual(self.counts(), before)

    def test_invalid_batches_are_rejected_before_writing(self):
        before = self.counts()
        finding = {"ref": "f1", "kind": "Finding", "text": "Claim"}
        for kwargs in ({"records": [finding, finding]}, {"records": [{**finding, "ref": " "}]},
                       {"records": [{**finding, "ref": 7}]}, {"records": [{**finding, "ref": self.company.id}]},
                       {"records": [{**finding, "extra": 1}]}, {"records": [{"ref": "f1", "kind": "Finding"}]},
                       {"records": finding}, {"links": "f1"}, {"links": [{"from_id": "f1", "to_id": "f1"}]},
                       {"records": [{**finding, "ref": str(n)} for n in range(1001)]}):
            with self.subTest(kwargs=str(kwargs)[:80]), self.assertRaises(ValueError):
                self.contribute(**kwargs)
        self.assertEqual(self.counts(), before)


if __name__ == "__main__":
    unittest.main()
