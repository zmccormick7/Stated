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
        self.assertEqual(self.db.connections(self.b.id).links, ())
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

    def test_update_retargets_in_place_and_survives_reopening(self):
        first = self.db.create_link(self.a.id, self.b.id, "about", author_id="alice")
        before = datetime.now(UTC)
        updated = self.db.update_link(first.id, from_id=self.c.id, to_id=self.a.id,
                                      relation="depends_on", author_id="bob", explanation="A enables C")
        self.assertEqual(updated.id, first.id)
        self.assertLessEqual(before, datetime.fromisoformat(updated.updated_at))
        self.assertEqual(self.db.connections(self.b.id, direction="in").links, ())
        self.assertEqual(self.db.connections(self.a.id).links, ())
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
