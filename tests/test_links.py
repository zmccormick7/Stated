from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

import premisedb
from premisedb import RecordStore, RevisionConflict


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
        ab = self.db.create_link(self.a.id, self.b.id, "depends_on", author_id="alice")
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

    def test_pins_history_retargeting_and_stale_edits(self):
        first = self.db.create_link(self.a.id, self.b.id, "supported_by", author_id="alice",
                                    from_revision=1, to_revision=1, explanation="B supports A")
        self.db.revise(self.a.id, expected_revision=1, text="Changed claim", author_id="bob")
        self.assertEqual(self.db.read_link(first.id), first)
        self.assertEqual(self.db.read(first.from_id, first.from_revision), self.a)
        with RecordStore(self.path) as other:
            seen = other.read_link(first.id)
            second = self.db.revise_link(first.id, expected_revision=1, from_id=self.b.id,
                                         to_id=self.c.id, relation="about", author_id="bob")
            with self.assertRaises(RevisionConflict) as caught:
                other.revise_link(seen.id, expected_revision=seen.revision, from_id=self.a.id,
                                  to_id=self.b.id, relation="about", author_id="carol")
            self.assertEqual((caught.exception.expected, caught.exception.current), (1, 2))
            self.assertEqual(caught.exception.target_id, first.id)
        self.assertEqual(self.db.connections(self.a.id).links, ())
        self.assertEqual(self.db.connections(self.b.id, direction="in").links, ())
        self.assertEqual(self.db.connections(self.c.id, direction="in").links, (second,))
        self.assertEqual(self.db.connections(self.b.id, relation="about").links, (second,))
        self.assertEqual(self.db.connections(self.b.id, relation="supported_by").links, ())
        self.db.close()
        with RecordStore(self.path) as reopened:
            self.assertEqual(reopened.read_link(first.id, 1), first)
            self.assertEqual(reopened.read_link(first.id), second)

    def test_invalid_endpoints_and_pins_leave_no_orphan_links(self):
        with self.assertRaises(KeyError):
            self.db.create_link(self.a.id, "missing", "about", author_id="alice")
        for relation, options in (("supported_by", {}), ("about", {"to_revision": 99})):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.create_link(self.a.id, self.b.id, relation, author_id="alice", **options)
        with closing(sqlite3.connect(self.path)) as sql:
            self.assertEqual(sql.execute("SELECT count(*) FROM links").fetchone()[0], 0)

    def test_history_immutability_and_failed_retarget_rollback(self):
        first = self.db.create_link(self.a.id, self.b.id, "about", author_id="alice")
        with closing(sqlite3.connect(self.path, isolation_level=None)) as sql:
            for statement in ("UPDATE link_revisions SET relation = 'changed'",
                              "DELETE FROM link_revisions"):
                with self.assertRaises(sqlite3.IntegrityError):
                    sql.execute(statement)
            sql.executescript("""
                CREATE TRIGGER fail_link_head BEFORE UPDATE ON links BEGIN
                    SELECT RAISE(ABORT, 'injected failure');
                END;
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.revise_link(first.id, expected_revision=1, from_id=self.a.id,
                                to_id=self.c.id, relation="about", author_id="bob")
        self.assertEqual(self.db.read_link(first.id), first)
        self.assertEqual(self.db.connections(self.b.id, direction="in").links, (first,))
        self.assertEqual(self.db.connections(self.c.id, direction="in").links, ())
        with self.assertRaises(KeyError):
            self.db.read_link(first.id, 2)

    def test_upgrades_v1_database_without_changing_records(self):
        path = self.path.with_name("v1.sqlite")
        schema = Path(premisedb.__file__).with_name("schema.sql").read_text()
        with closing(sqlite3.connect(path, isolation_level=None)) as sql:
            sql.execute("PRAGMA foreign_keys = ON")
            sql.executescript(schema + """
                BEGIN;
                INSERT INTO records VALUES (1, 'existing', 'Finding', 1);
                INSERT INTO record_revisions VALUES
                    (1, 1, 'Prior research', '{}', 'alice', '2026-09-22T00:00:00+00:00');
                COMMIT;
            """)
        with RecordStore(path) as upgraded:
            old = upgraded.read("existing")
            self.assertEqual((old.revision, old.text, old.author_id), (1, "Prior research", "alice"))
            link = upgraded.create_link(old.id, old.id, "related_to", author_id="bob")
        with RecordStore(path) as reopened:
            self.assertEqual(reopened.read("existing"), old)
            self.assertEqual(reopened.read_link(link.id), link)
