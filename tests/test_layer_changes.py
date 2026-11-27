from contextlib import closing
from hashlib import sha256
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from stated import Embedder, LayerWriter, RecordStore
from stated.embedding import DIMENSIONS
from stated.retrieval import candidates

ACME, BOLT, REPORT, LETTER, NOTICE = (str(uuid5(NAMESPACE_URL, name)) for name in ("acme", "bolt", "report", "letter", "notice"))
VECTOR = bytes([5]) * DIMENSIONS


class Fives(Embedder):
    model = "test-model"

    def _embed(self, texts):
        return [[0.0125] * DIMENSIONS for _ in texts]


class LayerChangeTests(unittest.TestCase):
    """A workspace started from a base file is brought forward by files that hold only what is new."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "research.sqlite"
        base = self.writer("base-1.sqlite", "2026.11.24")
        base.entity(ACME, "Acme Corp", {"cik": "0000000001"})
        self.document(base, REPORT, "Acme Corp 10-K filed 2024-02-01", "Net sales rose.\n\nCosts fell.", "2024-02-01T21:05:00Z", [ACME])
        self.first = base.seal()
        shutil.copy(self.first.path, self.path)
        with RecordStore(self.path) as db:
            db.start_own_layer("fund")

    def writer(self, name, version, written="2026-11-25T00:00:00+00:00"):
        return LayerWriter(self.directory / name, name="base", version=version, author_id="builder", written=written, keyword_index=False)

    def document(self, writer, record_id, description, text, published, about):
        writer.source(record_id, description, text, about=about, attributes={"published": published},
                      ranges=[(0, len(text.encode()))], vectors=[VECTOR], model="test-model")

    def day(self, name="base-2.sqlite", version="2026.11.25", acme="Acme Corp"):
        """The next day's file: one new document about a company the base holds and one it does not."""
        change = self.writer(name, version, written="2026-11-26T00:00:00+00:00")
        change.entity(ACME, acme, {"cik": "0000000001"})  # Repeated so the new document's link has its end.
        change.entity(BOLT, "Bolt Inc", {"cik": "0000000002"})
        self.document(change, LETTER, "Acme Corp 8-K filed 2026-11-25", "Acme and Bolt agreed to merge.", "2026-11-25T13:00:00Z", [ACME, BOLT])
        return change.seal()

    def test_a_change_file_adds_what_is_new_and_leaves_the_rest_as_it_was(self):
        with closing(sqlite3.connect(self.path)) as raw:
            before = raw.execute("SELECT * FROM records ORDER BY record_key").fetchall()
        change = self.day()
        with RecordStore(self.path) as db:
            applied = db.apply_layer_changes(change.path, follows="2026.11.24")
            self.assertEqual((applied.name, applied.version, applied.records, applied.links, applied.review),
                             ("base", "2026.11.25", {"added": 2, "updated": 0, "removed": 0, "kept": 0}, {"added": 2, "updated": 0, "removed": 0}, {}))
            self.assertEqual([(layer.name, layer.version, layer.writable, layer.keyword_index) for layer in db.layers()],
                             [("fund", "", True, True), ("base", "2026.11.25", False, False)])
            # The layer's hash now names the base and the change applied to it.
            chained = sha256(f"{self.first.content_sha256}\n{change.content_sha256}".encode()).hexdigest()
            self.assertEqual((applied.content_sha256, db.layers()[1].content_sha256), (chained, chained))
            self.assertEqual(db.read_source_text(LETTER), "Acme and Bolt agreed to merge.")
            self.assertEqual((db.source_ids_about(ACME), db.source_ids_about(BOLT)), ((REPORT, LETTER), (LETTER,)))
            self.assertEqual(db.count_passages([REPORT, LETTER]), (2, 2))  # Each new passage came with its embedding.
            self.assertEqual(candidates(db, "merger", source_ids=[REPORT, LETTER], embedder=Fives(), limit=1)[1], "embedding")
            self.assertEqual(db.search_documents("merge"), ())  # The layer has no keyword index, and gained none.
            mine = db.create("Finding", text="A merger was agreed.", author_id="agent", citations=[{"source_id": LETTER, "quote": "agreed to merge"}])
            self.assertEqual(mine.attributes["citations"][0]["source_id"], LETTER)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "read-only layer"):  # The base takes no writes again.
                db._db.execute("UPDATE records SET text = 'Mine' WHERE id = ?", (ACME,))
        with closing(sqlite3.connect(self.path)) as raw:  # What the layer held is untouched, stamps and all.
            self.assertEqual(raw.execute("SELECT * FROM records ORDER BY record_key").fetchall()[:len(before)], before)
        with RecordStore(self.path, as_of="2026-11-24") as then:  # A reader before the filing does not see it.
            self.assertEqual(then.source_ids_about(ACME), (REPORT,))

    def test_a_file_is_applied_only_to_the_version_it_follows(self):
        change = self.day()
        with RecordStore(self.path) as db:
            with self.assertRaisesRegex(ValueError, "follows version 2026.11.23 of base, and this workspace holds 2026.11.24"):
                db.apply_layer_changes(change.path, follows="2026.11.23")
            db.apply_layer_changes(change.path, follows="2026.11.24")
            with self.assertRaisesRegex(ValueError, "this workspace holds 2026.11.25"):  # Not twice.
                db.apply_layer_changes(change.path, follows="2026.11.24")
            self.assertEqual(db.source_ids_about(ACME), (REPORT, LETTER))
        with RecordStore(self.directory / "other.sqlite") as other, self.assertRaisesRegex(ValueError, "no attached layer named 'base'"):
            other.apply_layer_changes(change.path, follows="2026.11.24")

    def test_a_changed_record_is_rewritten_and_the_workspace_told_what_to_reassess(self):
        with RecordStore(self.path) as db:
            note = db.create("Finding", text="Acme is a maker of anvils.", author_id="agent")
            db.create_link(note.id, ACME, "about", author_id="agent")
            applied = db.apply_layer_changes(self.day(acme="Acme Holdings").path, follows="2026.11.24")
            self.assertEqual((applied.records["added"], applied.records["updated"]), (2, 1))
            self.assertEqual(applied.review, {"changed_linked_records": [ACME]})
            self.assertEqual(db.read(ACME).text, "Acme Holdings")
            self.assertEqual([record.id for record in db.search_records("Holdings")], [ACME])  # The record index follows.

    def test_a_file_that_would_replace_a_document_s_text_or_is_altered_changes_nothing(self):
        other = self.writer("other-text.sqlite", "2026.11.25")
        other.entity(ACME, "Acme Corp", {"cik": "0000000001"})
        self.document(other, REPORT, "Acme Corp 10-K filed 2024-02-01", "Net sales fell.", "2024-02-01T21:05:00Z", [ACME])
        self.document(other, NOTICE, "Acme Corp 8-K filed 2026-11-25", "A notice.", "2026-11-25", [ACME])
        altered = self.day()
        with closing(sqlite3.connect(altered.path)) as raw, raw:
            raw.execute("UPDATE layers SET writable = 1")
            raw.execute("UPDATE records SET text = 'Bolt Incorporated' WHERE id = ?", (BOLT,))
            raw.execute("UPDATE layers SET writable = 0")
        with RecordStore(self.path) as db:
            with self.assertRaisesRegex(ValueError, "text differs from the attached copy"):
                db.apply_layer_changes(other.seal().path, follows="2026.11.24")
            with self.assertRaisesRegex(ValueError, "does not match its manifest hash"):
                db.apply_layer_changes(altered.path, follows="2026.11.24")
            self.assertEqual([(layer.name, layer.version, layer.writable) for layer in db.layers()], [("fund", "", True), ("base", "2026.11.24", False)])
            self.assertEqual((db.source_ids_about(ACME), db.read_source_text(REPORT)), ((REPORT,), "Net sales rose.\n\nCosts fell."))
            with self.assertRaises(KeyError):
                db.read(NOTICE)
            db.create("Finding", text="Still mine to write.", author_id="agent")


if __name__ == "__main__":
    unittest.main()
