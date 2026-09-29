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

    def test_list_records_where_matches_attribute_type_and_value(self):
        make = lambda text, **attributes: self.db.create("Question", text=text, author_id="a", attributes=attributes)
        open_q, answered, number, integer, flag, empty = (
            make("Open", status="open", owner="nvda"), make("Answered", status="answered"),
            make("Float", value=0.45), make("Integer", value=1), make("Flag", value=True), make("Null", value=None))
        text_one = make("Text one", value="1")
        where = lambda **conditions: [r.text for r in self.db.list_records(kind="Question", where=conditions).records]
        self.assertEqual(where(status="open"), ["Open"])
        self.assertEqual(where(status="open", owner="nvda"), ["Open"])
        self.assertEqual(where(status="open", owner="mu"), [])
        self.assertEqual((where(value=0.45), where(value=1), where(value=True), where(value=None), where(value="1")),
                         (["Float"], ["Integer"], ["Flag"], ["Null"], ["Text one"]))
        self.assertEqual(where(missing=None), [])
        for bad in ([], {"": 1}, {'a"b': 1}, {"value": [1]}, {"value": {"a": 1}}, {"value": float("nan")}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.db.list_records(where=bad)

    def test_identical_links_are_refused(self):
        link = self.db.create_link(self.a.id, self.b.id, "qualifies", author_id="alice", explanation="Why.")
        with self.assertRaisesRegex(ValueError, f"identical qualifies link already exists: {link.id}"):
            self.db.create_link(self.a.id, self.b.id[:8], "qualifies", author_id="bob")
        self.db.create_link(self.b.id, self.a.id, "qualifies", author_id="bob")  # The reverse is a different link.
        other = self.db.create_link(self.a.id, self.b.id, "corroborates", author_id="bob")
        with self.assertRaisesRegex(ValueError, link.id):
            self.db.patch_link(other.id, author_id="bob", relation="qualifies")
        with self.assertRaisesRegex(ValueError, link.id):
            self.db.update_link(other.id, from_id=self.a.id, to_id=self.b.id, relation="qualifies", author_id="bob")
        self.assertEqual(self.db.patch_link(link.id, author_id="bob", explanation="Sharper.").explanation, "Sharper.")
        with self.assertRaisesRegex(ValueError, r"^links\[1\]: an identical about link"):
            self.db.contribute(author_id="carol", records=[{"ref": "n", "kind": "Finding", "text": "Newest"}],
                               links=[{"from_id": "n", "to_id": self.a.id, "relation": "about"},
                                      {"from_id": "n", "to_id": self.a.id, "relation": "about"}])
        self.assertEqual(self.db.search_records("Newest"), ())  # The batch rolled back.

    def test_list_links_filters_and_pages_in_creation_order(self):
        ab = self.db.create_link(self.a.id, self.b.id, "depends_on", author_id="alice")
        bc = self.db.create_link(self.b.id, self.c.id, "qualifies", author_id="bob")
        ca = self.db.create_link(self.c.id, self.a.id, "depends_on", author_id="bob")
        self.assertEqual(self.db.list_links().links, (ab, bc, ca))
        self.assertEqual(self.db.list_links(relation="depends_on").links, (ab, ca))
        page = self.db.list_links(limit=2)
        self.assertEqual((page.links, page.has_more), ((ab, bc), True))
        self.assertEqual(self.db.list_links(after=bc.id[:8]).links, (ca,))
        edited = self.db.patch_link(ab.id, author_id="carol", explanation="Checked.")
        self.assertEqual(self.db.list_links(since=edited.updated_at).links, (edited,))  # Edits count.
        self.assertEqual((self.db.list_links(author_id="carol").links, self.db.list_links(created_by="alice").links),
                         ((edited,), (edited,)))
        self.assertEqual(self.db.list_links(since="2999-01-01").links, ())
        with self.assertRaises(KeyError):
            self.db.list_links(after="missing-link")
        for options in ({"limit": 0}, {"since": 2026}, {"relation": 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.db.list_links(**options)

    def test_patch_link_changes_only_named_fields(self):
        link = self.db.create_link(self.a.id, self.b.id, "qualifies", author_id="alice", explanation="Why it matters.")
        relabelled = self.db.patch_link(link.id, author_id="bob", relation="corroborates")
        self.assertEqual((relabelled.from_id, relabelled.to_id, relabelled.relation, relabelled.explanation,
                          relabelled.author_id), (self.a.id, self.b.id, "corroborates", "Why it matters.", "bob"))
        moved = self.db.patch_link(link.id, author_id="carol", to_id=self.c.id, explanation="")
        self.assertEqual((moved.to_id, moved.relation, moved.explanation), (self.c.id, "corroborates", ""))
        for kwargs, error in (({}, ValueError), ({"to_id": "missing"}, KeyError), ({"relation": " "}, sqlite3.IntegrityError)):
            with self.subTest(kwargs=kwargs), self.assertRaises(error):
                self.db.patch_link(link.id, author_id="dan", **kwargs)
        with self.assertRaises(KeyError):
            self.db.patch_link("missing", author_id="dan", relation="about")
        self.assertEqual(self.db.read_link(link.id), moved)

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
