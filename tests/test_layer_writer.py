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

    def test_a_layer_can_go_without_a_keyword_index_and_is_then_found_by_scope_and_embedding(self):
        from stated import Embedder
        from stated.retrieval import candidates

        class Fives(Embedder):
            model = "test-model"

            def _embed(self, texts):
                return [[0.0125] * DIMENSIONS for _ in texts]

        class Offline(Fives):
            def _embed(self, texts):
                raise RuntimeError("OpenAI HTTP 503")

        self.write(keyword_index=False)
        with RecordStore(self.path) as base:
            self.assertEqual(base.search_documents("costs"), ())  # Not found by its words.
            self.assertEqual(candidates(base, "costs", source_ids=[REPORT])[1], "every passage")  # Few enough to read whole.
            scope = dict(source_ids=[REPORT], limit=1)
            self.assertEqual(candidates(base, "costs", embedder=Fives(), **scope)[1], "embedding")
            with self.assertRaisesRegex(ValueError, "not in the keyword index.*set OPENAI_API_KEY"):
                candidates(base, "costs", **scope)
            with self.assertRaisesRegex(ValueError, "1 passages have no embedding"):
                candidates(base, "letter", embedder=Fives(), source_ids=[REPORT, LETTER], limit=2)
            with self.assertRaisesRegex(RuntimeError, "OpenAI HTTP 503"):  # No words to fall back on.
                candidates(base, "costs", embedder=Offline(), **scope)
        with RecordStore(self.directory / "workspace.sqlite") as mine:
            mine.attach_layer(self.path)
            own = mine.ingest_source(description="My note", document_text="Costs fell at Acme.", author_id="agent")
            mine.chunk_source(own.id)
            self.assertEqual([(layer.name, layer.keyword_index) for layer in mine.layers()], [("workspace", True), ("base", False)])
            self.assertEqual([s.source_id for s in mine.search_documents("costs")], [own.id])  # A search of everything covers the workspace's own.
            self.assertEqual((mine.keyword_indexed([own.id]), mine.keyword_indexed([own.id, REPORT])), (True, False))
            mine.attach_layer(self.path)  # Attaching again replaces passages the keyword index never held.
            self.assertEqual(mine.count_passages([REPORT, LETTER])[0], 3)

    def init(self, *arguments):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        import json
        from stated.cli import main
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--db", str(self.path), "init", *arguments])
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_a_published_file_becomes_a_workspace_built_on_its_layer(self):
        self.write(keyword_index=False)
        self.assertEqual(self.init("--layer", "base")[0], 1)  # The published layer has that name.
        self.assertEqual(self.init(), (0, {"database": str(self.path.resolve()), "built_on": [{"layer": "base", "version": "2026.11"}]}))
        self.assertEqual(self.init(), (0, {"database": str(self.path.resolve())}))  # Already a workspace: left as it is.
        with RecordStore(self.path) as mine:
            self.assertEqual([(layer.name, layer.writable, layer.keyword_index) for layer in mine.layers()],
                             [("workspace", True, True), ("base", False, False)])
            finding = mine.create("Finding", text="Sales rose.", author_id="agent", citations=[{"source_id": REPORT, "quote": "Net sales rose."}])
            self.assertEqual((finding.layer, mine.read(REPORT).layer), ("workspace", "base"))
            note = mine.ingest_source(description="My note", document_text="Costs fell at Acme.", author_id="agent")
            mine.chunk_source(note.id)
            mine.create_link(note.id, ACME, "about", author_id="agent")
            self.assertEqual([s.source_id for s in mine.search_documents("costs")], [note.id])  # The workspace's own documents are found by words.
            self.assertEqual(set(mine.source_ids_about(ACME)), {REPORT, LETTER, note.id})
            self.assertEqual(mine.count_passages([REPORT, LETTER]), (3, 2))  # The base's passages and vectors are where they were.
            with self.assertRaisesRegex(sqlite3.IntegrityError, "read-only layer"):
                mine.update(ACME, text="Acme Corporation", author_id="agent")

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
