from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from stated import LayerWriter, RecordStore
from stated.embedding import DIMENSIONS
from stated.layers import _snapshot, digest

ACME, REPORT, LETTER = (str(uuid5(NAMESPACE_URL, name)) for name in ("acme", "report", "letter"))
VECTOR = bytes([5]) * DIMENSIONS


class LayerWriterTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "base.sqlite"

    def write(self, path=None, **options):
        writer = LayerWriter(path or self.path, name="base", version="2026.11", author_id="builder",
                             written="2026-11-25T00:00:00+00:00", **options)
        writer.entity(ACME, "Acme Corp", {"cik": "0000000001"})
        writer.source(REPORT, "Acme Corp 10-K filed 2024-02-01", "Net sales rose.\n\nCosts fell.", about=[ACME],
                      attributes={"published": "2024-02-01T21:05:00Z", "form": "10-K"},
                      ranges=[(0, 17), (17, 28)], vectors=[VECTOR, VECTOR], model="test-model")
        writer.source(LETTER, "Acme Corp 8-K filed 2024-06-03", "A letter to holders.", about=[ACME],
                      attributes={"published": "2024-06-03"})
        return writer, writer.seal()

    def test_the_file_is_a_sealed_layer_holding_what_was_written_under_the_ids_given(self):
        _, export = self.write()
        self.assertEqual((export.name, export.version, export.records, export.links, export.sources), ("base", "2026.11", 3, 2, 2))
        with RecordStore(self.path) as db:
            self.assertEqual([(layer.name, layer.version, layer.writable) for layer in db.layers()], [("base", "2026.11", False)])
            self.assertEqual(db.read(ACME).attributes, {"cik": "0000000001"})
            self.assertEqual(db.read_source_text(REPORT), "Net sales rose.\n\nCosts fell.")
            self.assertEqual([(p.start_byte, p.end_byte) for p in db.read_passages(REPORT)], [(0, 17), (17, 28)])
            self.assertEqual([(p.start_byte, p.end_byte) for p in db.read_passages(LETTER)], [(0, 20)])  # Split as `chunk` splits.
            self.assertEqual(db.count_passages([REPORT, LETTER]), (3, 2))
            self.assertEqual(db.vector_model(), "test-model")
            self.assertEqual(db.source_ids_about(ACME), (REPORT, LETTER))
            self.assertEqual(db.search_documents("costs")[0].text, "Costs fell.")
            with self.assertRaises(sqlite3.IntegrityError):  # The file holds no layer that takes writes.
                db.create("Finding", text="Mine", author_id="agent")

    def test_a_reader_at_a_date_sees_a_document_and_its_link_from_the_day_it_was_published(self):
        self.write()
        with RecordStore(self.path, as_of="2024-03-31") as then:
            self.assertEqual(then.source_ids_about(ACME), (REPORT,))
            self.assertEqual([record.id for record in then.sources_about(ACME).records], [REPORT])
            with self.assertRaises(KeyError):
                then.read(LETTER)
        with RecordStore(self.path, as_of="2024-01-31") as before:
            self.assertEqual(before.source_ids_about(ACME), ())
            self.assertEqual(before.read(ACME).text, "Acme Corp")  # A company is reference data, always seen.

    def test_the_same_inputs_give_the_same_file_and_it_attaches_like_an_exported_one(self):
        _, first = self.write()
        _, second = self.write(self.directory / "again.sqlite")
        self.assertEqual(first.content_sha256, second.content_sha256)
        with sqlite3.connect(self.path) as raw:
            raw.row_factory = sqlite3.Row
            self.assertEqual(digest(raw, 1), _snapshot(raw, 1).digest())  # Read a row at a time, the same hash.
            self.assertEqual(raw.execute("SELECT content_sha256 FROM layers").fetchone()[0], first.content_sha256)
        with RecordStore(self.directory / "workspace.sqlite") as mine:
            attached = mine.attach_layer(self.path)
            self.assertEqual((attached.name, attached.version), ("base", "2026.11"))
            finding = mine.create("Finding", text="Sales rose.", author_id="agent", citations=[{"source_id": REPORT, "quote": "Net sales rose."}])
            self.assertEqual(finding.attributes["citations"][0]["source_id"], REPORT)

    def test_what_cannot_be_written_is_refused_by_name(self):
        writer = LayerWriter(self.path, name="base", version="1", author_id="builder")
        writer.entity(ACME, "Acme Corp")
        dated = {"published": "2024-02-01"}
        for problem, call in (
                ("already exists", lambda: LayerWriter(self.path, name="base", version="1", author_id="builder")),
                ("UNIQUE", lambda: writer.entity(ACME, "Acme again")),
                ("must be a UUID", lambda: writer.entity("acme", "Acme Corp")),
                ("needs attributes.published", lambda: writer.source(REPORT, "Undated", "Text.", attributes={})),
                ("which has not been written", lambda: writer.source(REPORT, "Report", "Text.", attributes=dated, about=[LETTER])),
                ("cover the entire source text", lambda: writer.source(REPORT, "Report", "Some text.", attributes=dated, ranges=[(0, 4)])),
                ("one vector for each", lambda: writer.source(LETTER, "Letter", "Some text.", attributes=dated, vectors=[], model="m"))):
            with self.subTest(problem), self.assertRaisesRegex((ValueError, sqlite3.IntegrityError), problem):
                call()


if __name__ == "__main__":
    unittest.main()
