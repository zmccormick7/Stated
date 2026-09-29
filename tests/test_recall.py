from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore, recall


class RecallTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = RecordStore(Path(directory.name) / "research.sqlite")
        self.addCleanup(self.db.close)
        db = self.db
        self.source = db.ingest_source(description="Utility filing", author_id="a",
                                       document_text="Interconnection takes four years. Turbines are sold out.")
        make = lambda kind, text, **attributes: db.create(kind, text=text, author_id="a", attributes=attributes)
        self.company = make("Entity", "PowerFrame")
        self.layer = make("Entity", "Energy layer hub")
        self.claim = make("Finding", "Grid interconnection limits data center growth.", basis="inference")
        self.evidence = db.create("Finding", text="Interconnection takes years.", author_id="a", attributes={"basis": "disclosed"},
                                  citations=[{"source_id": self.source.id, "quote": "Interconnection takes four years."}])
        self.legacy = make("Finding", "Turbines are scarce.", basis="disclosed",
                           citations=[{"source_id": self.source.id, "start_byte": 34, "end_byte": 56}])
        self.variable = make("Variable", "Deployment growth input.", key="growth", value=0.3, basis="assumption")
        self.caveat = make("Finding", "Some utilities have spare capacity.", basis="inference")
        self.open_q = make("Question", "Which sites have firm service?", status="open")
        self.done_q = make("Question", "Is the queue public?", status="answered")
        self.other = make("Finding", "PowerFrame hired a new CFO.", basis="disclosed")
        link = lambda a, b, relation: db.create_link(a.id, b.id, relation, author_id="a")
        link(self.claim, self.evidence, "depends_on")
        link(self.evidence, self.legacy, "depends_on")
        self.qualifies = link(self.caveat, self.claim, "qualifies")
        link(self.variable, self.claim, "depends_on")
        link(self.claim, self.open_q, "raises")
        link(self.claim, self.done_q, "raises")
        link(self.claim, self.company, "about")
        link(self.other, self.company, "about")
        link(self.company, self.layer, "in_layer")

    def test_walks_reasoning_links_and_scores_by_hop(self):
        result = recall(self.db, "grid")
        by_id = {r.record.id: r for r in result.records}
        self.assertEqual(result.records[0].record, self.claim)
        self.assertEqual(result.records[0].path, ())
        self.assertEqual([l.relation for l in by_id[self.legacy.id].path], ["depends_on", "depends_on"])
        self.assertGreater(by_id[self.evidence.id].score, by_id[self.legacy.id].score)
        self.assertGreater(by_id[self.variable.id].score, by_id[self.company.id].score)  # about is weaker.
        self.assertIn(self.company.id, by_id)
        for absent in (self.other, self.layer, self.source):  # about is one hop; in_layer, supported_by not walked.
            self.assertNotIn(absent.id, by_id)
        self.assertEqual((result.coverage["seeds"], result.coverage["truncated"]), (0, False))

    def test_evidence_issues_and_open_questions(self):
        result = recall(self.db, "grid")
        quotes = {(e.start_byte, e.end_byte): (e.quote, e.record_ids) for e in result.evidence}
        self.assertEqual(quotes[(0, 33)], ("Interconnection takes four years.", (self.evidence.id,)))
        self.assertEqual(quotes[(34, 56)], ("Turbines are sold out.", (self.legacy.id,)))  # Read from the source.
        self.assertEqual(result.issue_links, (self.qualifies,))
        self.assertEqual(result.open_questions, (self.open_q,))

    def test_seeds_come_first_and_limits_fill_the_frontier(self):
        result = recall(self.db, "nothing matches this", seeds=[self.variable.id[:8]], max_records=2)
        self.assertEqual([r.record for r in result.records], [self.variable, self.claim])
        self.assertEqual(result.records[0].channels, ("seed",))
        self.assertTrue(result.coverage["truncated"])
        self.assertIn(self.evidence.id, result.frontier)
        empty = recall(self.db, "nothing matches this")
        self.assertEqual((empty.records, empty.coverage["entry_points"]), ((), 0))

    def test_invalid_input(self):
        for kwargs in ({"max_records": 0}, {"max_records": 101}, {"entry_limit": 0}, {"seeds": "abc"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                recall(self.db, "grid", **kwargs)
        with self.assertRaises(ValueError):
            recall(self.db, None)
        with self.assertRaises(KeyError):
            recall(self.db, "grid", seeds=["missing-id"])


if __name__ == "__main__":
    unittest.main()
