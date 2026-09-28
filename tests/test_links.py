from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore


class LinkTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "graph.sqlite"
        self.db = RecordStore(self.path)
        self.addCleanup(self.db.close)
        self.a, self.b, self.c = [self.db.create("Finding", text=name, author_id="alice")
                                 for name in ("A", "B", "C")]

    def test_directions_filters_and_limits(self):
        ab = self.db.create_link(self.a.id, self.b.id, "supported_by", author_id="alice")
        ac = self.db.create_link(self.a.id, self.c.id, "custom_relation", author_id="alice")
        self.assertEqual(self.db.connections(self.b.id, direction="in").links, (ab,))
        self.assertEqual(self.db.connections(self.b.id, direction="out").links, ())
        self.assertEqual(self.db.connections(self.a.id, relation="custom_relation").links, (ac,))
        page = self.db.connections(self.a.id, limit=1)
        self.assertEqual(page.links, (ab,))
        self.assertTrue(page.has_more)
        self.assertFalse(self.db.connections(self.a.id, limit=2).has_more)
        with self.assertRaises(KeyError):
            self.db.connections("missing")
        for options in ({"direction": "sideways"}, {"limit": 0}):
            with self.assertRaises(ValueError):
                self.db.connections(self.a.id, **options)

    def test_both_directions_return_linked_records(self):
        ab = self.db.create_link(self.a.id, self.b.id, "raises", author_id="alice")
        cb = self.db.create_link(self.c.id, self.b.id, "depends_on", author_id="alice")
        loop = self.db.create_link(self.b.id, self.b.id, "custom", author_id="alice")
        again = self.db.create_link(self.a.id, self.b.id, "about", author_id="alice")
        both = self.db.connections(self.b.id)
        self.assertEqual((both.links, both.has_more), ((ab, cb, loop, again), False))
        self.assertEqual(list(both.records), [self.a.id, self.c.id, self.b.id])
        self.assertEqual(both.records[self.a.id], self.a)
        self.db.update(self.c.id, text="C revised", author_id="bob")
        self.assertEqual(self.db.connections(self.b.id, relation="depends_on").records[self.c.id].text, "C revised")
        page = self.db.connections(self.b.id, limit=2)
        self.assertEqual((page.links, page.has_more, list(page.records)), ((ab, cb), True, [self.a.id, self.c.id]))
        outgoing = self.db.connections(self.a.id, direction="out")
        self.assertEqual((outgoing.links, outgoing.records), ((ab, again), {self.b.id: self.b}))
        self.assertEqual(self.db.connections(self.c.id, relation="raises").records, {})

    def test_list_records_filters_and_pages_in_creation_order(self):
        question = self.db.create("Question", text="Q", author_id="bob")
        self.assertEqual(self.db.list_records().records, (self.a, self.b, self.c, question))
        self.assertEqual(self.db.list_records(kind="Finding", limit=2).records, (self.a, self.b))
        self.assertTrue(self.db.list_records(kind="Finding", limit=2).has_more)
        self.assertEqual(self.db.list_records(kind="Finding", after=self.b.id),
                         self.db.list_records(kind="Finding", after=self.b.id, limit=1))
        self.assertEqual(self.db.list_records(kind="Finding", after=self.b.id).records, (self.c,))
        self.assertFalse(self.db.list_records(kind="Finding", after=self.b.id).has_more)
        self.assertEqual(self.db.list_records(author_id="bob").records, (question,))
        self.assertEqual(self.db.list_records(author_id="nobody").records, ())
        with self.assertRaises(KeyError):
            self.db.list_records(after="missing")
        for options in ({"kind": "Company"}, {"limit": 0}, {"limit": 1001}, {"author_id": 3}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.db.list_records(**options)

    def test_relation_usage_counts_links_by_relation_and_record_kinds(self):
        self.assertEqual(self.db.relation_usage(), ())
        question = self.db.create("Question", text="Q", author_id="alice")
        for from_id, to_id, relation in ((self.a.id, self.b.id, "depends_on"), (self.c.id, self.b.id, "depends_on"),
                                         (self.a.id, question.id, "raises"), (question.id, self.c.id, "depends_on"),
                                         (question.id, self.a.id, "answered_by")):
            self.db.create_link(from_id, to_id, relation, author_id="alice")
        usage = [(u.relation, u.from_kind, u.to_kind, u.links) for u in self.db.relation_usage()]
        self.assertEqual(usage, [("answered_by", "Question", "Finding", 1), ("depends_on", "Finding", "Finding", 2),
                                 ("depends_on", "Question", "Finding", 1), ("raises", "Finding", "Question", 1)])
        link = self.db.connections(question.id, relation="depends_on").links[0]
        self.db.update_link(link.id, from_id=self.a.id, to_id=self.b.id, relation="updates", author_id="bob")
        self.assertIn(("updates", "Finding", "Finding", 1),
                      [(u.relation, u.from_kind, u.to_kind, u.links) for u in self.db.relation_usage()])
        self.assertNotIn("Question", {u.from_kind for u in self.db.relation_usage() if u.relation == "depends_on"})

    def test_update_retargets_in_place_and_survives_reopening(self):
        first = self.db.create_link(self.a.id, self.b.id, "about", author_id="alice")
        before = datetime.now(UTC)
        updated = self.db.update_link(first.id, from_id=self.c.id, to_id=self.a.id,
                                      relation="depends_on", author_id="bob", explanation="A enables C")
        self.assertEqual(updated.id, first.id)
        self.assertLessEqual(before, datetime.fromisoformat(updated.updated_at))
        self.assertEqual(self.db.connections(self.b.id, direction="in").links, ())
        self.assertEqual(self.db.connections(self.a.id, direction="out").links, ())
        self.assertEqual(self.db.connections(self.c.id, relation="depends_on").links, (updated,))
        self.db.update(self.a.id, text="New understanding", author_id="carol")
        self.db.close()
        with RecordStore(self.path) as reopened:
            self.assertEqual(reopened.read_link(first.id), updated)
            self.assertEqual(reopened.read(updated.to_id).text, "New understanding")
        with closing(sqlite3.connect(self.path)) as sql:
            self.assertEqual(sql.execute("SELECT count(*) FROM links").fetchone()[0], 1)

    def test_failed_updates_keep_current_link_and_connections(self):
        first = self.db.create_link(self.a.id, self.b.id, "about", author_id="alice")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.update_link(first.id, from_id=self.c.id, to_id=self.a.id,
                                relation="", author_id="bob")
        with self.assertRaises(KeyError):
            self.db.update_link(first.id, from_id="missing", to_id=self.a.id,
                                relation="about", author_id="bob")
        with self.assertRaises(KeyError):
            self.db.update_link("missing", from_id=self.a.id, to_id=self.b.id,
                                relation="about", author_id="bob")
        self.assertEqual(self.db.read_link(first.id), first)
        self.assertEqual(self.db.connections(self.b.id, direction="in").links, (first,))
