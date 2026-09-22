from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import premisedb
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

    def test_migrate_v2_keeps_latest_records_and_links_only(self):
        path = self.path.with_name("v2.sqlite")
        with closing(sqlite3.connect(path, isolation_level=None)) as sql:
            sql.executescript("""
                CREATE TABLE records (record_key, id, kind, current_revision);
                CREATE TABLE record_revisions (record_key, revision, text, attributes, author_id, recorded_at);
                CREATE TABLE links (link_key, id, current_revision, from_key, to_key, relation);
                CREATE TABLE link_revisions (link_key, revision, from_key, to_key, relation,
                    from_revision, to_revision, explanation, author_id, recorded_at);
                INSERT INTO records VALUES (1, 'a', 'Finding', 2), (2, 'b', 'Finding', 1);
                INSERT INTO record_revisions VALUES
                    (1, 1, 'Old', '{}', 'alice', 'old'), (1, 2, 'Current A', '{}', 'bob', 'latest'),
                    (2, 1, 'Current B', '{}', 'alice', 'latest');
                INSERT INTO links VALUES (5, 'edge', 2, 2, 1, 'about');
                INSERT INTO link_revisions VALUES
                    (5, 1, 1, 2, 'supported_by', 1, 1, 'Old basis', 'alice', 'old'),
                    (5, 2, 2, 1, 'about', NULL, NULL, 'Corrected', 'bob', 'latest');
                PRAGMA user_version = 2;
            """)
        with patch("pathlib.Path.read_text", side_effect=OSError("unreadable schema")):
            with self.assertRaises(OSError):
                RecordStore(path)
        with closing(sqlite3.connect(path)) as sql:
            self.assertEqual(sql.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(sql.execute("SELECT count(*) FROM link_revisions").fetchone()[0], 2)
        with RecordStore(path) as migrated:
            edge = migrated.read_link('edge')
            self.assertEqual((edge.from_id, edge.to_id, edge.explanation, edge.updated_at),
                             ('b', 'a', 'Corrected', 'latest'))
            self.assertEqual(migrated.read('a').text, 'Current A')
            self.assertEqual(migrated.connections('a', direction='in').links, (edge,))
        with closing(sqlite3.connect(path)) as sql:
            tables = {row[0] for row in sql.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertEqual(tables, {"records", "links", "sqlite_sequence"})
            self.assertEqual(sql.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_open_current_record_database_adds_links(self):
        path = self.path.with_name("v3.sqlite")
        with closing(sqlite3.connect(path, isolation_level=None)) as sql:
            sql.execute(Path(premisedb.__file__).with_name("schema.sql").read_text())
            sql.execute("PRAGMA user_version = 3")
        with RecordStore(path) as upgraded:
            record = upgraded.create("Entity", text="Company", author_id="alice")
            upgraded.create_link(record.id, record.id, "related_to", author_id="alice")
