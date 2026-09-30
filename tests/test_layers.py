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


class AttachTests(unittest.TestCase):
    """A publisher's workspace exports versions; a subscriber's workspace attaches them and builds on them."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.publisher = RecordStore(self.directory / "publisher.sqlite", layer="base")
        self.addCleanup(self.publisher.close)
        self.terms = self.publisher.ingest_source(description="Terms", document_text="Deliveries may be rescheduled.",
                                                  author_id="p")
        self.publisher.chunk_source(self.terms.id)
        self.publisher.set_document_context(self.terms.id, title="Delivery terms", summary="", author_id="p")
        self.finding = self.publisher.create("Finding", text="Backlog may slip.", author_id="p",
                                             citations=[{"source_id": self.terms.id, "quote": "rescheduled"}])
        self.dropped = self.publisher.create("Question", text="Dropped later.", author_id="p")
        self.orphan = self.publisher.create("Question", text="Dropped later, unlinked.", author_id="p")
        self.raises = self.publisher.create_link(self.finding.id, self.dropped.id, "raises", author_id="p")
        self.v1 = self.directory / "base-1.sqlite"
        self.publisher.export_layer(self.v1, version="1")
        self.subscriber = RecordStore(self.directory / "fund.sqlite", layer="fund")
        self.addCleanup(self.subscriber.close)

    def test_attach_copies_the_layer_read_only_and_the_workspace_builds_on_it(self):
        attachment = self.subscriber.attach_layer(self.v1)
        self.assertEqual((attachment.name, attachment.version, attachment.records, attachment.links, attachment.review),
                         ("base", "1", {"added": 4, "updated": 0, "removed": 0, "kept": 0},
                          {"added": 2, "updated": 0, "removed": 0}, {}))
        own, base = self.subscriber.layers()
        self.assertEqual((own.name, own.writable, base.name, base.version, base.writable), ("fund", True, "base", "1", False))
        self.assertEqual(base.content_sha256, attachment.content_sha256)
        self.assertEqual(self.subscriber.read(self.finding.id), self.finding)
        self.assertEqual(self.subscriber.read_link(self.raises.id), self.raises)
        self.assertEqual(self.subscriber.read_source_text(self.terms.id), "Deliveries may be rescheduled.")
        self.assertEqual(self.subscriber.read_document_context(self.terms.id).title, "Delivery terms")
        self.assertEqual([c.record.id for c in self.subscriber.cited_by(self.terms.id).citations], [self.finding.id])
        self.assertEqual(self.subscriber.search_records("backlog")[0].layer, "base")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "read-only layer"):
            self.subscriber.patch(self.finding.id, author_id="f", text="Edited")
        challenge = self.subscriber.create("Finding", text="Customers rarely defer.", author_id="f",
                                           citations=[{"source_id": self.terms.id, "quote": "may be"}])
        link = self.subscriber.create_link(challenge.id, self.finding.id, "contradicts", author_id="f")
        self.assertEqual((challenge.layer, link.layer), ("fund", "fund"))
        # Attaching the same version again changes nothing.
        again = self.subscriber.attach_layer(self.v1)
        self.assertEqual((again.records, again.links), ({"added": 0, "updated": 0, "removed": 0, "kept": 0},
                                                        {"added": 0, "updated": 0, "removed": 0}))

    def test_a_new_version_updates_in_place_and_reports_what_to_review(self):
        self.subscriber.attach_layer(self.v1)
        challenge = self.subscriber.create("Finding", text="Customers rarely defer.", author_id="f",
                                           citations=[{"source_id": self.terms.id, "quote": "may be"}])
        self.subscriber.create_link(challenge.id, self.finding.id, "contradicts", author_id="f")
        self.subscriber.create_link(challenge.id, self.dropped.id, "partly_answers", author_id="f")
        key_before = self.subscriber._record_key(self.finding.id)
        # Version 2: the finding is revised, one question is added, two are dropped, the raises link changes.
        self.publisher.patch(self.finding.id, author_id="p", text="Backlog may slip a quarter.")
        added = self.publisher.create("Question", text="Added in version 2.", author_id="p")
        self.publisher.patch_link(self.raises.id, author_id="p", to_id=added.id)
        sql = self.publisher._db
        for record in (self.dropped, self.orphan):
            sql.execute("DELETE FROM records WHERE id = ?", (record.id,))
        v2 = self.directory / "base-2.sqlite"
        self.publisher.export_layer(v2, version="2")

        attachment = self.subscriber.attach_layer(v2)
        self.assertEqual(attachment.records, {"added": 1, "updated": 1, "removed": 1, "kept": 1})
        self.assertEqual(attachment.links, {"added": 0, "updated": 1, "removed": 0})
        self.assertEqual(attachment.review, {"changed_linked_records": [self.finding.id],
                                             "removed_upstream_but_linked": [self.dropped.id]})
        self.assertEqual(self.subscriber.layers()[1].version, "2")
        self.assertEqual(self.subscriber.read(self.finding.id).text, "Backlog may slip a quarter.")
        self.assertEqual(self.subscriber._record_key(self.finding.id), key_before)  # Keys survive versions.
        self.assertEqual(self.subscriber.read_link(self.raises.id).to_id, added.id)
        self.assertEqual(self.subscriber.read(self.dropped.id).layer, "base")
        with self.assertRaises(KeyError):
            self.subscriber.read(self.orphan.id)
        self.assertEqual({c.record.id for c in self.subscriber.cited_by(self.terms.id).citations},
                         {self.finding.id, challenge.id})
        self.assertEqual(self.subscriber.search_records("quarter")[0].id, self.finding.id)

    def test_withdrawn_text_leaves_and_changed_text_is_refused(self):
        self.subscriber.attach_layer(self.v1)
        cite = self.subscriber.create("Finding", text="Cites terms.", author_id="f",
                                      citations=[{"source_id": self.terms.id, "quote": "may be"}])
        self.publisher.patch(self.terms.id, author_id="p", attributes={"redistributable": False})
        v2 = self.directory / "base-2.sqlite"
        self.publisher.export_layer(v2, version="2")
        self.subscriber.attach_layer(v2)
        for read in (self.subscriber.read_source_text, self.subscriber.read_document_context):
            with self.assertRaises(KeyError):  # No text, so nothing derived from it either.
                read(self.terms.id)
        self.assertEqual(self.subscriber.read(cite.id).attributes["citations"][0]["source_id"], self.terms.id)
        self.assertEqual(self.subscriber._db.execute("SELECT count(*) FROM passages").fetchone()[0], 0)
        # A different text under the same Source ID can never replace the fixed copy.
        other = RecordStore(self.directory / "other.sqlite", layer="base")
        self.addCleanup(other.close)
        other._db.execute("INSERT INTO records (id, kind, text, attributes, author_id, created_by, updated_at, layer_key)"
                          " VALUES (?, 'Source', 'Terms', '{}', 'p', 'p', 'now', 1)", (self.terms.id,))
        other._db.execute("INSERT INTO source_content (source_key, text_utf8, text_sha256) VALUES (1, ?, ?)",
                          (b"Different.", sha256(b"Different.").hexdigest()))
        v3 = self.directory / "base-3.sqlite"
        other.export_layer(v3, version="3")
        fresh = RecordStore(self.directory / "fresh.sqlite")
        self.addCleanup(fresh.close)
        fresh.attach_layer(self.v1)
        with self.assertRaisesRegex(ValueError, "text differs"):
            fresh.attach_layer(v3)
        self.assertEqual(fresh.layers()[1].version, "1")  # Nothing changed.

    def test_attach_refuses_bad_files_and_the_workspace_own_layer(self):
        with self.assertRaisesRegex(ValueError, "not a file"):
            self.subscriber.attach_layer(self.directory / "missing.sqlite")
        with self.assertRaisesRegex(ValueError, "exactly one sealed layer"):
            self.subscriber.attach_layer(self.directory / "publisher.sqlite")
        with self.assertRaisesRegex(ValueError, "own layer"):
            self.publisher.attach_layer(self.v1)
        altered = self.directory / "altered.sqlite"
        altered.write_bytes(self.v1.read_bytes())
        with RecordStore(altered) as file:
            file._db.execute("UPDATE layers SET writable = 1")
            file.patch(self.orphan.id, author_id="x", text="tampered")
            file._db.execute("UPDATE layers SET writable = 0")
        with self.assertRaisesRegex(ValueError, "manifest hash"):
            self.subscriber.attach_layer(altered)
        self.assertEqual(len(self.subscriber.layers()), 1)
