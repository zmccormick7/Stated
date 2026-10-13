from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore


class PointInTimeTests(unittest.TestCase):
    """Reads at a cutoff see what a reader on that date could have; writes are stamped with it."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.live = RecordStore(self.path)  # Today, outside any cutoff.
        self.addCleanup(self.live.close)
        self.today = datetime.now(UTC).date().isoformat()
        self.bank = self.live.create("Entity", text="Silicon Valley Bank", author_id="setup")
        self.annual = self.live.ingest_source(
            description="2022 annual report", document_text="Deposits were $173.1 billion at year end.",
            author_id="setup", attributes={"published": "2023-02-24"})
        self.undated = self.live.ingest_source(description="A page", document_text="Deposits fell further.",
                                               author_id="setup")
        self.later = self.live.ingest_source(description="A release", document_text="The bank was closed.",
                                             author_id="setup", attributes={"published": "2023-03-10"})
        for source in (self.annual, self.undated, self.later):
            self.live.chunk_source(source.id)

    def at(self, date):
        store = RecordStore(self.path, as_of=date)
        self.addCleanup(store.close)
        return store

    def test_writes_are_stamped_with_the_cutoff_or_the_date(self):
        self.assertEqual(self.bank.as_of, self.today)
        cutoff = self.at("2023-02-24")
        finding = cutoff.create("Finding", text="Deposits were falling.", author_id="agent",
                                attributes={"basis": "inference"})
        link = cutoff.create_link(finding.id, self.bank.id, "about", author_id="agent")
        self.assertEqual((finding.as_of, link.as_of), ("2023-02-24", "2023-02-24"))
        # An edit from another date restamps: the contents are only known to be as of the last write.
        self.assertEqual(self.live.patch(finding.id, author_id="agent", attributes={"topic": "deposits"}).as_of,
                         self.today)
        self.assertEqual(self.live.patch_link(link.id, author_id="agent", explanation="Subject.").as_of, self.today)

    def test_sources_are_seen_from_their_publication_date(self):
        with self.assertRaisesRegex(KeyError, "not visible as of 2023-02-23"):
            self.at("2023-02-23").read(self.annual.id)
        self.assertEqual(self.at("2023-02-24").read(self.annual.id), self.annual)
        for store in (self.at("2023-02-24"), self.at(self.today)):  # No date, never seen.
            with self.assertRaises(KeyError):
                store.read(self.undated.id)
            with self.assertRaises(KeyError):
                store.read_source_text(self.undated.id)
        day = self.at("2023-03-01")
        with self.assertRaises(KeyError):
            day.locate_source_text(self.later.id, "closed")
        self.assertEqual({r.id for r in day.search_records("report release")}, {self.annual.id})
        self.assertEqual({r.id for r in day.list_records(kind="Source").records}, {self.annual.id})
        self.assertEqual({s.source_id for s in day.search_documents("deposits")}, {self.annual.id})
        with self.assertRaisesRegex(KeyError, "not visible"):
            day.resolve_id(self.later.id[:8])
        self.assertEqual(len(self.live.list_records(kind="Source").records), 3)

    def test_disclosed_findings_are_seen_from_their_evidence(self):
        cite = lambda *sources: [{"source_id": s.id, "quote": "Deposits"} for s in sources]
        disclosed = self.live.create("Finding", text="Deposits were $173.1 billion.", author_id="agent",
                                     attributes={"basis": "disclosed"}, citations=cite(self.annual))
        mixed = self.live.create("Finding", text="Deposits fell.", author_id="agent",
                                 attributes={"basis": "disclosed"}, citations=cite(self.annual, self.undated))
        inference = self.live.create("Finding", text="Deposits looked fragile.", author_id="agent",
                                     attributes={"basis": "inference"}, citations=cite(self.annual))
        unlabelled = self.live.create("Finding", text="Deposits.", author_id="agent", citations=cite(self.annual))
        day = self.at("2023-02-24")
        self.assertEqual({r.id for r in day.list_records(kind="Finding").records}, {disclosed.id})
        self.assertEqual([c.record.id for c in day.cited_by(self.annual.id).citations], [disclosed.id])
        self.assertEqual({r.id for r in self.at(self.today).list_records(kind="Finding").records},
                         {disclosed.id, mixed.id, inference.id, unlabelled.id})
        with self.assertRaises(KeyError):
            self.at("2023-02-23").read(disclosed.id)

    def test_records_written_at_a_cutoff_are_seen_from_it(self):
        then = self.at("2023-02-24")
        finding = then.create("Finding", text="Uninsured deposits were 96% of the total.", author_id="agent",
                              attributes={"basis": "inference"})
        question = then.create("Question", text="How fast could deposits leave?", author_id="agent")
        then.create_link(finding.id, question.id, "raises", author_id="agent")
        then.create_link(finding.id, self.bank.id, "about", author_id="agent")
        self.assertEqual(self.at("1999-01-01").read(self.bank.id), self.bank)  # Entities are always seen.
        before = self.at("2023-02-23")
        self.assertEqual(before.list_records(kind="Finding").records, ())
        self.assertEqual(before.connections(self.bank.id).links, ())
        for store in (then, self.at("2023-03-01"), self.live):
            reached = {r.record.id for r in store.expand([self.bank.id], depth=2).reached}
            self.assertEqual(reached, {self.bank.id, finding.id, question.id})
            self.assertEqual([q.id for q in store.list_records(kind="Question").records], [question.id])

    def test_links_need_their_stamp_and_both_ends(self):
        then = self.at("2023-02-24")
        first = then.create("Finding", text="First", author_id="agent", attributes={"basis": "inference"})
        second = then.create("Finding", text="Second", author_id="agent", attributes={"basis": "inference"})
        link = self.at("2023-03-01").create_link(second.id, first.id, "depends_on", author_id="agent")
        self.assertEqual(then.connections(first.id).links, ())
        self.assertEqual([r.record.id for r in then.expand([first.id]).reached], [first.id])
        with self.assertRaisesRegex(KeyError, "not visible as of 2023-02-24"):
            then.read_link(link.id)
        self.assertEqual(then.relation_usage(), ())
        march = self.at("2023-03-01")
        self.assertEqual(march.connections(first.id).links, (link,))
        self.assertEqual([u.links for u in march.relation_usage()], [1])
        # A link stamped early still waits for its ends: the live inference is not seen in February.
        late = self.live.create("Finding", text="Hindsight", author_id="agent", attributes={"basis": "inference"})
        self.live.create_link(late.id, first.id, "qualifies", author_id="agent")
        self.assertEqual(self.at("2023-02-24").connections(first.id).links, ())
        self.assertEqual(len(self.live.connections(first.id).links), 2)

    def test_writes_cannot_reach_what_the_cutoff_hides(self):
        then = self.at("2023-02-24")
        hidden = self.live.create("Finding", text="Written today", author_id="agent", attributes={"basis": "inference"})
        with self.assertRaisesRegex(KeyError, "not visible as of 2023-02-24"):
            then.create("Finding", text="Closed.", author_id="agent", attributes={"basis": "disclosed"},
                        citations=[{"source_id": self.later.id, "quote": "closed"}])
        with self.assertRaisesRegex(KeyError, "not visible as of 2023-02-24"):
            then.create_link(self.bank.id, hidden.id, "raises", author_id="agent")
        with self.assertRaisesRegex(KeyError, "not visible as of 2023-02-24"):
            then.patch(hidden.id, author_id="agent", text="Changed")
        with self.assertRaisesRegex(KeyError, "not visible as of 2023-02-24"):
            then.contribute(author_id="agent", records=[{"ref": "f", "kind": "Finding", "text": "x"}],
                            links=[{"from_id": "f", "to_id": hidden.id, "relation": "qualifies"}])
        self.assertEqual(then.list_records(kind="Finding").records, ())  # Nothing was written.
        self.assertEqual(self.live.read(hidden.id), hidden)

    def test_the_cutoff_must_be_a_date(self):
        for value in ("2023-2-24", "2023-02-30", "2023-02-24T00:00", 20230224, ""):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "as_of must be a date"):
                RecordStore(self.path, as_of=value)

    def test_published_layers_keep_the_stamps(self):
        then = self.at("2023-02-24")
        finding = then.create("Finding", text="Stamped", author_id="agent", attributes={"basis": "inference"})
        link = then.create_link(finding.id, self.bank.id, "about", author_id="agent")
        published = self.path.with_name("base.sqlite")
        self.live.export_layer(published, version="1")
        with RecordStore(self.path.with_name("fund.sqlite"), layer="fund") as fund:
            fund.attach_layer(published)
            self.assertEqual((fund.read(finding.id).as_of, fund.read_link(link.id).as_of), ("2023-02-24",) * 2)
            self.assertEqual(fund.read(self.annual.id).as_of, self.annual.as_of)
        with RecordStore(self.path.with_name("fund.sqlite"), layer="fund", as_of="2023-02-23") as fund:
            self.assertEqual({r.id for r in fund.list_records().records}, {self.bank.id})
