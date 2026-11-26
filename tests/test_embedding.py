from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from stated import Embedder, OpenAIEmbedder, RecordStore, embed_source
from stated.cli import main
from stated.embedding import DIMENSIONS, pack


def unit(first: float) -> list[float]:
    return [first] + [0.0] * (DIMENSIONS - 1)


class Lengths(Embedder):
    """A stand-in model: a text's vector starts with its length in hundredths."""
    model = "lengths"

    def __init__(self):
        self.seen = []

    def _embed(self, texts):
        self.seen.append(texts)
        return [unit(len(text) / 100) for text in texts]


def reply(texts):
    """What OpenAI answers, out of order to show the index is what places a vector."""
    rows = [{"index": n, "embedding": unit(len(text) / 100)} for n, text in enumerate(texts)]
    return BytesIO(json.dumps({"data": rows[::-1]}).encode())


class PackingTests(unittest.TestCase):
    def test_a_vector_is_stored_as_one_signed_byte_a_number(self):
        stored = pack([0.1, -0.1, 0.0012, 0.9, -0.9] + [0.0] * (DIMENSIONS - 5))
        self.assertEqual(len(stored), DIMENSIONS)
        self.assertEqual(list(memoryview(stored).cast("b")[:6]), [40, -40, 0, 127, -127, 0])  # Out-of-range numbers are held at the ends.

    def test_a_vector_of_another_length_or_with_a_number_that_is_not_one_is_refused(self):
        for vector in ([0.1] * (DIMENSIONS - 1), [float("nan")] * DIMENSIONS, ["0.1"] * DIMENSIONS):
            with self.subTest(vector=vector[:1]), self.assertRaises(ValueError):
                pack(vector)


class EmbedderTests(unittest.TestCase):
    def test_one_vector_comes_back_per_text_in_order(self):
        self.assertEqual([vector[0] for vector in Lengths().embed(["a" * 10, "b" * 20])], [40, 80])
        self.assertEqual(Lengths().embed([]), ())
        with self.assertRaises(ValueError):
            Lengths().embed(["text", ""])

    def test_a_model_that_answers_for_the_wrong_number_of_texts_is_refused(self):
        class Short(Lengths):
            def _embed(self, texts):
                return [unit(0.1)]
        with self.assertRaises(ValueError):
            Short().embed(["a", "b"])

    def test_openai_is_asked_in_batches_for_vectors_of_the_stored_length(self):
        texts = ["a" * n for n in (10, 20, 30)]
        with patch("stated.embedding.urlopen", side_effect=lambda request, timeout: reply(json.loads(request.data)["input"])) as call:
            vectors = OpenAIEmbedder(api_key="secret", batch_size=2).embed(texts)
        self.assertEqual([vector[0] for vector in vectors], [40, 80, 120])
        sent = [json.loads(request.args[0].data) for request in call.call_args_list]
        self.assertEqual([(body["model"], body["dimensions"], len(body["input"])) for body in sent],
                         [("text-embedding-3-small", DIMENSIONS, 2), ("text-embedding-3-small", DIMENSIONS, 1)])
        self.assertEqual(call.call_args_list[0].args[0].get_header("Authorization"), "Bearer secret")

    def test_a_key_is_needed_and_a_failed_call_does_not_repeat_it(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), self.assertRaises(ValueError):
            OpenAIEmbedder()
        failure = HTTPError("https://api.openai.com/v1/embeddings", 429, "secret", {}, BytesIO())
        with patch("stated.embedding.urlopen", side_effect=failure), self.assertRaises(RuntimeError) as raised:
            OpenAIEmbedder(api_key="secret").embed(["text"])
        self.assertEqual(str(raised.exception), "OpenAI HTTP 429")
        with patch("stated.embedding.urlopen", return_value=BytesIO(b'{"data": [{"index": 0}]}')), self.assertRaises(ValueError):
            OpenAIEmbedder(api_key="secret").embed(["text"])


class StoredVectorTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.db = self.enterContext(RecordStore(self.path))
        self.text = "Grid opening €5.\n\nCosts uncertain.\n\nGrid opening delayed."
        self.source = self.db.ingest_source(description="Fictional filing", document_text=self.text, author_id="agent")
        self.db.chunk_source(self.source.id, max_chars=24)

    def vectors(self, source_id=None):
        return [row[0] for row in self.db._db.execute(
            """SELECT v.vector FROM passage_vectors v JOIN passages p ON p.passage_key = v.passage_key
               JOIN records r ON r.record_key = p.source_key WHERE r.id = ? ORDER BY p.ordinal""",
            (source_id or self.source.id,))]

    def test_each_passage_s_own_text_is_embedded_and_its_vector_stored_in_order(self):
        model = Lengths()
        self.assertEqual(embed_source(self.db, self.source.id, embedder=model), 3)
        self.assertEqual(model.seen, [("Grid opening €5.\n\n", "Costs uncertain.\n\n", "Grid opening delayed.")])
        self.assertEqual([vector[0] for vector in self.vectors()], [72, 72, 84])
        self.assertEqual(tuple(self.db._db.execute("SELECT model, dimensions FROM vector_space").fetchone()), ("lengths", DIMENSIONS))

    def test_vectors_go_when_their_passages_are_replaced(self):
        embed_source(self.db, self.source.id, embedder=Lengths())
        self.db.chunk_source(self.source.id, max_chars=2000)
        self.assertEqual(self.vectors(), [])
        self.assertEqual(embed_source(self.db, self.source.id, embedder=Lengths()), 1)

    def test_a_source_needs_one_vector_for_each_passage(self):
        with self.assertRaisesRegex(ValueError, "one vector for each of the Source's 3 passages, got 2"):
            self.db.set_passage_vectors(self.source.id, [pack(unit(0.1))] * 2, model="lengths")
        with self.assertRaises(ValueError):
            self.db.set_passage_vectors(self.source.id, [b"", b"", b""], model="lengths")
        self.assertEqual(self.vectors(), [])

    def test_a_database_holds_vectors_of_one_model_and_length(self):
        embed_source(self.db, self.source.id, embedder=Lengths())
        other = self.db.ingest_source(description="Other", document_text="Grid elsewhere.", author_id="agent")
        self.db.chunk_source(other.id)
        with self.assertRaisesRegex(ValueError, "embedded with lengths at 512 dimensions"):
            self.db.set_passage_vectors(other.id, [pack(unit(0.1))], model="another-model")
        with self.assertRaisesRegex(ValueError, "embedded with lengths at 512 dimensions"):
            self.db.set_passage_vectors(other.id, [b"\x01" * 256], model="lengths")
        self.assertEqual(self.vectors(other.id), [])

    def chunk(self, key):
        out, err = StringIO(), StringIO()
        with patch.dict(os.environ, {"OPENAI_API_KEY": key}), redirect_stdout(out), redirect_stderr(err):
            code = main(["--db", str(self.path), "chunk", self.source.id, "--max-chars", "24"])
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_chunking_from_the_command_line_embeds_when_a_key_is_set(self):
        with patch("stated.embedding.urlopen", side_effect=lambda request, timeout: reply(json.loads(request.data)["input"])):
            self.assertEqual(self.chunk("secret")[:2], (0, 3))
        self.assertEqual([vector[0] for vector in self.vectors()], [72, 72, 84])
        with patch("stated.embedding.urlopen") as call:
            self.assertEqual(self.chunk(""), (0, 3, ""))  # No key: chunked, nothing asked, and the old vectors went with the old passages.
        call.assert_not_called()
        self.assertEqual(self.vectors(), [])

    def test_a_failed_embedding_call_leaves_the_source_chunked_and_warns(self):
        failure = HTTPError("https://api.openai.com/v1/embeddings", 500, "down", {}, BytesIO())
        with patch("stated.embedding.urlopen", side_effect=failure):
            code, count, warning = self.chunk("secret")
        self.assertEqual((code, count), (0, 3))
        self.assertIn("chunked without passage embeddings: OpenAI HTTP 500", json.loads(warning)["warning"])
        self.assertEqual(len(self.db.read_passages(self.source.id)), 3)


if __name__ == "__main__":
    unittest.main()
