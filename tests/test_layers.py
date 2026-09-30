from contextlib import contextmanager
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from premisedb import Layer, RecordStore


class LayerTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = RecordStore(self.path, layer="base")
        self.addCleanup(self.db.close)

    @contextmanager
    def attached(self, name):
        """Writes inside the block land in a new layer that is sealed afterwards, as attaching
        a published layer will fill it."""
        sql = self.db._db
        sql.execute("UPDATE layers SET writable = 0")
        sql.execute("INSERT INTO layers (name, version, writable) VALUES (?, '1', 1)", (name,))
        yield
        sql.execute("UPDATE layers SET writable = 0")
        sql.execute("UPDATE layers SET writable = 1 WHERE name = 'base'")

    def test_new_rows_belong_to_the_named_writable_layer(self):
        self.assertEqual(self.db.layers(), (Layer("base", "", True),))
        finding = self.db.create("Finding", text="Backlog may slip.", author_id="a")
        question = self.db.create("Question", text="Which customers can defer?", author_id="a")
        link = self.db.create_link(finding.id, question.id, "raises", author_id="a")
        self.assertEqual((finding.layer, self.db.read(finding.id).layer, link.layer), ("base", "base", "base"))
        self.assertEqual(self.db.search_records("backlog")[0].layer, "base")
        self.assertEqual(self.db.list_records().records[0].layer, "base")
        self.assertEqual(self.db.list_links().links[0].layer, "base")
        self.assertEqual(self.db.connections(finding.id).records[question.id].layer, "base")
        self.assertEqual(self.db.connections(finding.id).links[0].layer, "base")
        self.db.close()
        with RecordStore(self.path, layer="ignored-for-existing-databases") as reopened:
            self.assertEqual(reopened.layers(), (Layer("base", "", True),))

    def test_layer_names_must_be_nonblank(self):
        with self.assertRaisesRegex(ValueError, "nonblank"):
            RecordStore(self.path.with_name("blank.sqlite"), layer=" ")

    def test_read_only_layer_refuses_every_edit_but_accepts_links_from_the_workspace(self):
        with self.attached("published"):
            source = self.db.ingest_source(description="Terms", document_text="Deliveries may be rescheduled.",
                                           author_id="a")
            finding = self.db.create("Finding", text="Backlog may slip.", author_id="a",
                                     citations=[{"source_id": source.id, "quote": "rescheduled"}])
            support = self.db.connections(finding.id).links[0]
        self.assertEqual(self.db.layers(), (Layer("base", "", True), Layer("published", "1", False)))
        self.assertEqual(self.db.read(finding.id).layer, "published")
        for edit in (
            lambda: self.db.update(finding.id, text="Edited", author_id="b"),
            lambda: self.db.patch(finding.id, author_id="b", text="Edited"),
            lambda: self.db.update_link(support.id, from_id=finding.id, to_id=source.id,
                                        relation="qualifies", author_id="b"),
            lambda: self.db.patch_link(support.id, author_id="b", explanation="Edited"),
            lambda: self.db.set_document_context(source.id, title="Terms", summary="", author_id="b"),
        ):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "read-only layer"):
                edit()
        self.assertEqual((self.db.read(finding.id).text, self.db.read_link(support.id).explanation,
                          self.db.read_document_context(source.id)), ("Backlog may slip.", "Cited evidence.", None))
        # The workspace builds on published records with its own records and links, in both directions.
        challenge = self.db.create("Finding", text="Customers rarely defer.", author_id="b",
                                   citations=[{"source_id": source.id, "quote": "may be"}])
        contradiction = self.db.create_link(challenge.id, finding.id, "contradicts", author_id="b")
        question = self.db.contribute(author_id="b", records=[{"ref": "q", "kind": "Question", "text": "Who defers?"}],
                                      links=[{"from_id": finding.id, "to_id": "q", "relation": "raises"}])
        self.assertEqual((challenge.layer, contradiction.layer, question.records["q"].layer,
                          question.links[0].layer), ("base", "base", "base", "base"))
        self.assertEqual({link.layer for link in self.db.connections(finding.id).links}, {"base", "published"})
        self.assertEqual({c.record.layer for c in self.db.cited_by(source.id).citations}, {"base", "published"})

    def test_rows_cannot_move_between_layers(self):
        finding = self.db.create("Finding", text="Backlog may slip.", author_id="a")
        self.db._db.execute("INSERT INTO layers (name, version, writable) VALUES ('other', '1', 0)")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "read-only layer"):
            self.db._db.execute("UPDATE records SET layer_key = (SELECT layer_key FROM layers WHERE name = 'other')")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "writable layer"):
            self.db._db.execute(
                """INSERT INTO records (id, kind, text, attributes, author_id, created_by, updated_at, layer_key)
                   SELECT 'x', kind, text, attributes, author_id, created_by, updated_at,
                          (SELECT layer_key FROM layers WHERE name = 'other') FROM records""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "layers.writable"):
            self.db._db.execute("UPDATE layers SET writable = 1")
        self.assertEqual(self.db.read(finding.id).layer, "base")

