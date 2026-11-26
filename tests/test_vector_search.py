from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from stated import Embedder, OpenRouterReranker, RecordStore, Reranker, retrieve
from stated.cli import main
from stated.embedding import DIMENSIONS, pack
from stated.retrieval import candidates


def vector(first: float) -> list[float]:
    return [first] + [0.0] * (DIMENSIONS - 1)


class Marks(Embedder):
    """A stand-in model: a text's vector starts with the number written in it, in hundredths."""
    model = "marks"

    def __init__(self):
        self.asked = []

    def _embed(self, texts):
        self.asked.append(texts)
        return [vector(int(text.split()[-1]) / 100) for text in texts]


class Offline(Marks):
    def _embed(self, texts):
        raise RuntimeError("OpenAI HTTP 503")


class Needles(Reranker):
    def _score(self, query, segments):
        return [0.9 if "25" in segment.text else 0.05 for segment in segments]


class VectorSearchTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = self.enterContext(RecordStore(self.path))
        self.company = self.db.create("Entity", text="Acme", author_id="agent")
        self.report = self.add("report", range(30), "2024-02-01")  # Passages "report 0" to "report 29".
        self.letter = self.add("letter", (25, 3), "2024-06-01")

    def add(self, name, numbers, published, embed=True):
        parts = [f"{name} {n}\n" for n in numbers]
        source = self.db.ingest_source(description=name, document_text="".join(parts), author_id="agent", attributes={"published": published})
        ranges, offset = [], 0
        for part in parts:
            ranges.append((offset, offset + len(part)))
            offset += len(part)
        self.db.set_passages(source.id, ranges)
        if embed:
            self.db.set_passage_vectors(source.id, Marks().embed(parts), model="marks")
        self.db.create_link(source.id, self.company.id, "about", author_id="agent")
        return source

    def test_the_nearest_passages_come_first_and_ties_keep_document_order(self):
        both = [self.report.id, self.letter.id]
        found = self.db.nearest_passages(pack(vector(0.3)), source_ids=both, limit=4)
        self.assertEqual([s.text for s in found], ["report 29\n", "report 28\n", "report 27\n", "report 26\n"])
        self.assertEqual([s.text for s in self.db.nearest_passages(pack(vector(-0.3)), source_ids=both, limit=2)], ["report 0\n", "report 1\n"])
        self.assertEqual([s.text for s in self.db.nearest_passages(pack(vector(0.3)), source_ids=[self.letter.id])], ["letter 25\n", "letter 3\n"])
        self.assertEqual(self.db.count_passages(both), (32, 32))
        with self.assertRaises(ValueError):
            self.db.nearest_passages(b"\x01" * 8, source_ids=both)

    def test_a_search_small_enough_to_read_whole_takes_every_passage_and_asks_no_model(self):
        model = Marks()
        found, how = candidates(self.db, "anything 25", source_ids=[self.report.id], embedder=model, limit=30)
        self.assertEqual((len(found), how, model.asked), (30, "every passage", []))
        self.assertEqual(found, self.db.every_passage([self.report.id]))

    def test_a_larger_search_inside_documents_takes_the_nearest_by_embedding(self):
        model = Marks()
        found, how = candidates(self.db, "question 20", source_ids=[self.report.id, self.letter.id], embedder=model, limit=3)
        self.assertEqual(([s.text for s in found], how), (["report 29\n", "report 28\n", "report 27\n"], "embedding"))
        self.assertEqual(model.asked, [("question 20",)])

    def test_words_are_used_when_embeddings_cannot_be(self):
        scope = dict(source_ids=[self.report.id, self.letter.id], limit=3)
        different = Marks()
        different.model = "another-model"
        for name, embedder in (("no embedder", None), ("another model's vectors", different), ("the model is unreachable", Offline())):
            with self.subTest(name):
                found, how = candidates(self.db, "letter", embedder=embedder, **scope)
                self.assertEqual(([s.text for s in found], how), (["letter 25\n", "letter 3\n"], "keyword"))
        self.assertEqual(candidates(self.db, "letter", embedder=Marks(), limit=3)[1], "keyword")  # A search of everything.
        bare = self.add("memo", range(4), "2024-03-01", embed=False)
        self.assertEqual(self.db.count_passages([self.report.id, bare.id]), (34, 30))
        self.assertEqual(candidates(self.db, "memo", source_ids=[self.report.id, bare.id], embedder=Marks(), limit=3)[1], "keyword")

    def test_a_search_about_a_company_covers_its_documents_as_of_the_cutoff(self):
        result = retrieve(self.db, "question 25", about=self.company.id, reranker=Needles(), count_tokens=len, embedder=Marks(), candidate_limit=6)
        self.assertEqual(result.stage_one, "embedding")
        self.assertEqual({e.segment.text for e in result.excerpts}, {"report 25\n", "letter 25\n"})
        with RecordStore(self.path, as_of="2024-03-31") as then:  # The letter was not yet published.
            self.assertEqual(then.source_ids_about(self.company.id), (self.report.id,))
            earlier = retrieve(then, "question 25", about=self.company.id, reranker=Needles(), count_tokens=len, embedder=Marks(), candidate_limit=6)
        self.assertEqual([e.segment.text for e in earlier.excerpts], ["report 25\n"])
        with self.assertRaisesRegex(ValueError, "source_ids or about, not both"):
            retrieve(self.db, "question", about=self.company.id, source_ids=[self.report.id], reranker=Needles(), count_tokens=len)

    def test_the_command_line_searches_one_company_by_embedding_when_a_key_is_set(self):
        self.add("annex", range(40), "2024-07-01")  # 72 passages in all: more than the 50 a search reads whole.
        self.db.close()
        with RecordStore(self.path) as db:  # As OpenAI's model would have written them.
            db._db.execute("UPDATE vector_space SET model = 'text-embedding-3-small'")
        asked, scored = [], []

        def openai(request, timeout):
            asked.append(json.loads(request.data)["input"])
            return BytesIO(json.dumps({"data": [{"index": 0, "embedding": vector(0.3)}]}).encode())

        def rerank(reranker, endpoint, payload):
            scored.extend(json.loads(document)["passage"] for document in payload["documents"])
            return {"results": [{"index": n, "relevance_score": 0.9} for n in range(len(payload["documents"]))]}

        out, tokens = StringIO(), SimpleNamespace(get_encoding=lambda name: SimpleNamespace(encode=lambda text, **kwargs: text.encode()))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "OPENROUTER_API_KEY": "test-only"}), patch.dict(sys.modules, {"tiktoken": tokens}), \
                patch("stated.embedding.urlopen", side_effect=openai), patch.object(OpenRouterReranker, "_post", new=rerank), \
                redirect_stdout(out), redirect_stderr(StringIO()):
            code = main(["--db", str(self.path), "search-documents", "which passage", "--about", self.company.id[:8], "--limit", "1"])
        self.assertEqual((code, asked), (0, [["which passage"]]))
        self.assertEqual(len(scored), 50)  # The nearest 50 of the 72 went to the reranker: the highest marks, none of the lowest.
        self.assertTrue({"annex 39\n", "report 29\n", "letter 25\n"} <= set(scored) and not {"annex 5\n", "report 0\n", "letter 3\n"} & set(scored))
        self.assertEqual(len(json.loads(out.getvalue())["excerpts"]), 1)


if __name__ == "__main__":
    unittest.main()
