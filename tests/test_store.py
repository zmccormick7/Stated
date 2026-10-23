from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from premisedb import RecordStore


class RecordStoreTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.store = RecordStore(self.path)
        self.addCleanup(self.store.close)

    def test_update_replaces_contents_and_survives_reopening(self):
        original = self.store.create("Finding", text="Ramp in June", author_id="alice",
                                     attributes={"basis": "estimate"})
        before = datetime.now(UTC)
        updated = self.store.update(original.id, text="Ramp in September", author_id="bob")
        self.assertEqual((updated.id, updated.kind), (original.id, "Finding"))
        self.assertEqual((updated.text, updated.attributes, updated.author_id),
                         ("Ramp in September", {}, "bob"))
        self.assertLessEqual(before, datetime.fromisoformat(updated.updated_at))
        self.assertLessEqual(datetime.fromisoformat(updated.updated_at), datetime.now(UTC))
        self.store.close()
        with RecordStore(self.path) as reopened:
            self.assertEqual(reopened.read(original.id), updated)
        with closing(sqlite3.connect(self.path)) as sql:
            self.assertEqual(sql.execute("SELECT count(*) FROM records").fetchone()[0], 1)
            self.assertEqual(sql.execute("SELECT name FROM sqlite_master WHERE name = 'record_revisions'").fetchall(), [])

    def test_last_committed_update_wins(self):
        original = self.store.create("Finding", text="Initial", author_id="alice")
        with RecordStore(self.path) as other:
            other.read(original.id)
            self.store.update(original.id, text="Bob's edit", author_id="bob")
            latest = other.update(original.id, text="Carol's edit", author_id="carol")
        self.assertEqual(self.store.read(original.id), latest)

    def test_failed_write_keeps_current_contents(self):
        original = self.store.create("Finding", text="Valid", author_id="alice")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.update(original.id, text="Rejected", author_id="")
        self.assertEqual(self.store.read(original.id), original)
        self.store.update(original.id, text="Accepted", author_id="bob")

    def test_missing_references_and_invalid_inputs(self):
        with self.assertRaises(KeyError):
            self.store.read("missing")
        with self.assertRaises(KeyError):
            self.store.update("missing", text="x", author_id="alice")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create("Unknown", text="x", author_id="alice")
        for attributes in ([], {1: "coerced key"}, {"score": float("nan")}):
            with self.subTest(attributes=attributes), self.assertRaises(ValueError):
                self.store.create("Finding", text="x", author_id="alice", attributes=attributes)

    def test_resolve_id_accepts_exact_ids_and_unique_prefixes(self):
        ids = ["abcdef12-0000-4000-8000-000000000001", "abcdef12-9999-4000-8000-000000000002",
               "abcdef13-0000-4000-8000-000000000003", "a1111111-0000-4000-8000-000000000004"]
        with patch("premisedb.store.uuid4", side_effect=ids):
            first, second, third = [self.store.create("Finding", text=t, author_id="a") for t in "ABC"]
            link = self.store.create_link(first.id, third.id, "about", author_id="a")
        self.assertEqual(self.store.resolve_id(first.id), first.id)
        self.assertEqual(self.store.resolve_id("abcdef13"), third.id)
        self.assertEqual(self.store.resolve_id("abcdef12-9"), second.id)
        self.assertEqual(self.store.resolve_id("a1111111", link=True), link.id)
        with self.assertRaisesRegex(ValueError, "matches more than one record"):
            self.store.resolve_id("abcdef12")
        for missing in ("abcdef1", "abcdef14", "a1111111", "zzzzzzzz"):
            with self.subTest(missing=missing), self.assertRaises(KeyError):
                self.store.resolve_id(missing)
        for invalid in ("", None, 7):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.resolve_id(invalid)

    def test_creator_survives_edits_and_lists_filter_by_creator_and_time(self):
        record = self.store.create("Finding", text="Ramp in June", author_id="alice")
        other = self.store.create("Finding", text="Ramp in July", author_id="bob")
        self.assertEqual((record.author_id, record.created_by), ("alice", "alice"))
        patched = self.store.patch(record.id, author_id="carol", attributes={"basis": "inference"})
        updated = self.store.update(record.id, text="Ramp in September", author_id="dave")
        self.assertEqual([(r.author_id, r.created_by) for r in (patched, updated)], [("carol", "alice"), ("dave", "alice")])
        link = self.store.create_link(record.id, other.id, "depends_on", author_id="erin")
        moved = self.store.patch_link(link.id, author_id="frank", relation="qualifies")
        self.assertEqual((moved.author_id, moved.created_by), ("frank", "erin"))
        self.assertEqual(self.store.list_records(created_by="alice").records, (updated,))
        self.assertEqual(self.store.list_records(author_id="dave").records, (updated,))
        self.assertEqual(self.store.list_records(since=updated.updated_at).records, (updated,))  # Edits count.
        self.assertEqual(len(self.store.list_records(since="2000-01-01").records), 2)
        self.assertEqual(self.store.list_records(since="2999-01-01").records, ())
        for options in ({"since": 2026}, {"created_by": 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.store.list_records(**options)

    def test_payload_ids_accept_unique_prefixes(self):
        source = self.store.ingest_source(description="Filing", document_text="Power is scarce.", author_id="a")
        target = self.store.create("Entity", text="PowerFrame", author_id="a")
        result = self.store.contribute(author_id="a", records=[{
            "ref": "f", "kind": "Finding", "text": "Power binds",
            "citations": [{"source_id": source.id[:8], "quote": "Power"}]}],
            links=[{"from_id": "f", "to_id": target.id[:10], "relation": "about"}])
        finding = result.records["f"]
        self.assertEqual(finding.attributes["citations"][0]["source_id"], source.id)  # Stored in full.
        self.assertEqual(result.links[0].to_id, target.id)
        self.assertEqual(self.store.patch_link(result.links[0].id, author_id="b", to_id=source.id[:8]).to_id, source.id)
        self.assertEqual(self.store.cited_by(source.id[:8]).citations[0].record.id, finding.id)
        with self.assertRaises(KeyError):
            self.store.create_link(finding.id, "abc", "about", author_id="a")  # Too short to be a prefix.

    def test_incompatible_schema_is_rejected_without_changing_file(self):
        for version in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 17):
            with self.subTest(version=version):
                path = self.path.with_name(f"schema-{version}.sqlite")
                with closing(sqlite3.connect(path, isolation_level=None)) as sql:
                    sql.execute("CREATE TABLE existing_data (text TEXT)")
                    sql.execute("INSERT INTO existing_data VALUES ('Keep this')")
                    sql.execute(f"PRAGMA user_version = {version}")
                original = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "Incompatible schema version.*create a new database"):
                    RecordStore(path)
                self.assertEqual(path.read_bytes(), original)

    def test_failed_initialization_rolls_back_and_can_be_retried(self):
        path = self.path.with_name("new.sqlite")
        read_text = Path.read_text

        def fail_after_schema(file):
            return read_text(file) + "\nINVALID SQL;\n"

        with patch.object(Path, "read_text", fail_after_schema):
            with self.assertRaises(sqlite3.OperationalError):
                RecordStore(path)
        with closing(sqlite3.connect(path)) as sql:
            self.assertEqual(sql.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(sql.execute("SELECT name FROM sqlite_master").fetchall(), [])
        with RecordStore(path) as retried:
            record = retried.create("Entity", text="Company", author_id="alice")
            retried.create_link(record.id, record.id, "related_to", author_id="alice")
