from contextlib import contextmanager
from hashlib import sha256
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



class ExportTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.db = RecordStore(self.directory / "research.sqlite", layer="base")
        self.addCleanup(self.db.close)
        self.terms = self.db.ingest_source(description="Terms", document_text="Deliveries may be rescheduled.",
                                           author_id="a", attributes={"publisher": "PowerFrame"})
        self.db.chunk_source(self.terms.id)
        self.db.set_document_context(self.terms.id, title="Delivery terms", summary="Rescheduling.", author_id="a")
        self.call = self.db.ingest_source(description="Call transcript", document_text="We see deferrals ahead.",
                                          author_id="a", attributes={"redistributable": False})
        self.db.chunk_source(self.call.id)
        self.finding = self.db.create("Finding", text="Backlog may slip.", author_id="a",
                                      attributes={"basis": "disclosed"},
                                      citations=[{"source_id": self.terms.id, "quote": "rescheduled"},
                                                 {"source_id": self.call.id, "quote": "deferrals"}])
        self.question = self.db.create("Question", text="Who defers?", author_id="b")
        self.raises = self.db.create_link(self.finding.id, self.question.id, "raises", author_id="b",
                                          explanation="Open.")

    def test_published_file_holds_the_layer_sealed_with_its_manifest(self):
        path = self.directory / "base-1.sqlite"
        export = self.db.export_layer(path, version="2026.09.30")
        self.assertEqual((export.name, export.version, export.records, export.links, export.sources,
                          export.withheld), ("base", "2026.09.30", 4, 3, 1, 1))
        with RecordStore(path) as published:
            layer, = published.layers()
            self.assertEqual((layer.name, layer.version, layer.writable, layer.content_sha256),
                             ("base", "2026.09.30", False, export.content_sha256))
            self.assertIsNotNone(layer.exported_at)
            # Rows are copied verbatim, IDs, authors and timestamps included.
            self.assertEqual(published.read(self.finding.id), self.finding)
            self.assertEqual(published.read_link(self.raises.id), self.raises)
            self.assertEqual([r.id for r in published.list_records().records],
                             [self.terms.id, self.call.id, self.finding.id, self.question.id])
            self.assertEqual(published.read_source_text(self.terms.id), "Deliveries may be rescheduled.")
            self.assertEqual(published.read_document_context(self.terms.id).title, "Delivery terms")
            self.assertEqual(published.search_records("backlog")[0].id, self.finding.id)
            self.assertEqual([c.record.id for c in published.cited_by(self.terms.id).citations], [self.finding.id])
            # The withheld transcript keeps its record and hash but not its text, passages or citations.
            self.assertEqual(published.read(self.call.id).attributes, {"redistributable": False})
            with self.assertRaises(KeyError):
                published.read_source_text(self.call.id)
            self.assertEqual(published._db.execute(
                """SELECT text_sha256 FROM withheld_sources w JOIN records r ON r.record_key = w.source_key
                   WHERE r.id = ?""", (self.call.id,)).fetchone()[0], sha256(b"We see deferrals ahead.").hexdigest())
            self.assertEqual(published._db.execute("SELECT count(*) FROM passages").fetchone()[0], 1)
            # Sealed: nothing can be added to a published file.
            with self.assertRaises(sqlite3.IntegrityError):
                published.create("Question", text="Later", author_id="c")

    def test_export_is_deterministic_and_refuses_to_overwrite(self):
        first = self.db.export_layer(self.directory / "one.sqlite", version="1")
        second = self.db.export_layer(self.directory / "two.sqlite", version="2")
        self.assertEqual(first.content_sha256, second.content_sha256)
        self.db.patch(self.question.id, author_id="b", attributes={"status": "open"})
        third = self.db.export_layer(self.directory / "three.sqlite", version="3")
        self.assertNotEqual(first.content_sha256, third.content_sha256)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.db.export_layer(self.directory / "one.sqlite", version="4")
        with self.assertRaisesRegex(ValueError, "nonblank"):
            self.db.export_layer(self.directory / "four.sqlite", version=" ")

    def test_only_the_workspace_layer_is_exported_and_cross_layer_links_are_refused(self):
        sql = self.db._db
        sql.execute("UPDATE layers SET writable = 0")
        sql.execute("INSERT INTO layers (name, version, writable) VALUES ('attached', '1', 1)")
        published = self.db.create("Finding", text="Published elsewhere.", author_id="p")
        sql.execute("UPDATE layers SET writable = 0")
        sql.execute("UPDATE layers SET writable = 1 WHERE name = 'base'")
        export = self.db.export_layer(self.directory / "own.sqlite", version="1")
        self.assertEqual(export.records, 4)
        with RecordStore(self.directory / "own.sqlite") as file:
            with self.assertRaises(KeyError):
                file.read(published.id)
        self.db.create_link(self.finding.id, published.id, "corroborates", author_id="a")
        with self.assertRaisesRegex(ValueError, "1 links reach records in other layers"):
            self.db.export_layer(self.directory / "crossing.sqlite", version="2")
