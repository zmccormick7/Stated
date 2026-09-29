from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from io import StringIO
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from premisedb import OpenRouterReranker, RecordStore
from premisedb.cli import main


class CLIRetrievalTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite"
        self.parts = ("Grid opening €5.\r\n", "Costs uncertain.\n", "Grid opening delayed.\n")
        self.body = "".join(self.parts)
        with RecordStore(self.path) as db:
            self.source = db.ingest_source(description="Fictional filing", document_text=self.body,
                                           author_id="agent")
            offset, ranges = 0, []
            for part in self.parts:
                end = offset + len(part.encode())
                ranges.append((offset, end))
                offset = end
            db.set_passages(self.source.id, ranges)
            self.context = db.set_document_context(self.source.id, title="PowerFrame",
                                                  summary="Facility schedule", author_id="agent")
            self.other = db.ingest_source(description="Other", document_text="Grid elsewhere.", author_id="agent")
            db.chunk_source(self.other.id)
        self.enterContext(patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only"}))
        for key in ("PREMISEDB_RERANKER_MODEL", "PREMISEDB_TOKEN_ENCODING"):
            os.environ.pop(key, None)
        # A deterministic tokenizer stub; production uses BPE, not byte estimates.
        self.encoding = SimpleNamespace(encode=lambda text, **kwargs: text.encode())
        self.get_encoding = Mock(return_value=self.encoding)
        self.enterContext(patch.dict(sys.modules, {
            "tiktoken": SimpleNamespace(get_encoding=self.get_encoding)}))

    def call(self, *arguments, status=0):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--db", str(self.path), "search-documents", *arguments])
        self.assertEqual(code, status, stderr.getvalue())
        self.assertEqual(stderr.getvalue() if status == 0 else stdout.getvalue(), "")
        if status:
            return json.loads(stderr.getvalue())
        result = json.loads(stdout.getvalue())
        self.assertEqual(list(result), ["excerpts"])  # Every command prints one JSON object.
        return result["excerpts"]

    def respond(self, endpoint, payload):
        if endpoint == "alpha/decisions":
            documents = [payload["state"]]
        else:
            documents = [json.loads(document) for document in payload["documents"]]
        self.assertTrue(all(document["passage"] in (self.parts[0], self.parts[2]) for document in documents))
        self.assertTrue(all(document["document_context"] == self.context.header for document in documents))
        if endpoint == "alpha/decisions":
            return {"answers": {"relevance": {"type": "noul", "noul": 0.98}}}
        self.assertEqual((endpoint, payload["query"], payload["top_n"]), ("v1/rerank", "Grid", 2))
        return {"results": [{"index": i, "relevance_score": 0.98} for i in reversed(range(2))]}

    def test_question_runs_full_pipeline_by_default_and_preserves_citations(self):
        for model in (None, *OpenRouterReranker.MODELS):
            with self.subTest(model=model), patch.dict(os.environ):
                if model is not None:
                    os.environ["PREMISEDB_RERANKER_MODEL"] = model
                seen = []

                def post(reranker, endpoint, payload):
                    seen.append(reranker.model)
                    return self.respond(endpoint, payload)

                with patch.object(OpenRouterReranker, "_post", new=post):
                    excerpt, = self.call("Grid", "--source-id", self.source.id)
                self.assertEqual(set(seen), {model or "voyageai/rerank-2.5"})
                self.assertEqual(excerpt, {
                    "source_id": self.source.id, "start_byte": 0, "end_byte": len(self.body.encode()),
                    "text": self.body, "document_context": asdict(self.context),
                    "token_count": len((self.context.header + "\n\n" + self.body).encode())})
        self.get_encoding.assert_called_with("cl100k_base")

    def test_output_limits_and_encoding_are_honored(self):
        budget = len((self.context.header + "\n\n" + self.parts[2]).encode())
        with patch.object(OpenRouterReranker, "_post", side_effect=self.respond):
            excerpts = self.call("Grid", "--source-id", self.source.id, "--max-tokens", str(budget))
        self.assertEqual(len(excerpts), 1)
        self.assertLessEqual(sum(excerpt["token_count"] for excerpt in excerpts), budget)
        self.assertIn(excerpts[0]["text"], (self.parts[0], self.parts[2]))
        with RecordStore(self.path) as db:
            self.assertEqual(db.read_source_text(self.source.id, start_byte=excerpts[0]["start_byte"],
                                               end_byte=excerpts[0]["end_byte"]), excerpts[0]["text"])
        response = {"results": [{"index": i, "relevance_score": 0.98} for i in range(3)]}
        with patch.object(OpenRouterReranker, "_post", return_value=response):
            self.assertEqual(len(self.call("Grid")), 2)
            self.assertEqual(len(self.call("Grid", "--limit", "1")), 1)
            os.environ["PREMISEDB_TOKEN_ENCODING"] = "deployment_encoding"
            self.call("Grid", "--source-id", self.source.id, "--source-id", self.other.id)
        self.get_encoding.assert_called_with("deployment_encoding")

    def test_empty_results_and_invalid_inputs_make_no_provider_calls(self):
        with patch.object(OpenRouterReranker, "_post") as post:
            self.assertEqual(self.call("unmatched"), [])
            for args in (("Grid", "--limit", "0"), ("Grid", "--max-tokens", "0"),
                         ("Grid", "--source-id", "missing")):
                self.assertTrue(self.call(*args, status=1)["error"])
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
                self.assertIn("OPENROUTER_API_KEY", self.call("Grid", status=1)["error"])
            with patch.dict(os.environ, {"PREMISEDB_RERANKER_MODEL": "unknown"}):
                self.assertIn("model", self.call("Grid", status=1)["error"])
            self.get_encoding.side_effect = ValueError("Unknown encoding")
            self.assertIn("encoding", self.call("Grid", status=1)["error"])
            with patch.dict(sys.modules, {"tiktoken": None}):
                self.assertIn("pip install", self.call("Grid", status=1)["error"])
            post.assert_not_called()

    def test_provider_failures_do_not_return_unranked_or_partial_results(self):
        for response in ({}, {"results": [{"index": 0, "relevance_score": 2}]}):
            with patch.object(OpenRouterReranker, "_post", return_value=response):
                self.assertTrue(self.call("Grid", status=1)["error"])
        with patch.object(OpenRouterReranker, "_post", side_effect=RuntimeError("OpenRouter HTTP 503")):
            self.assertEqual(self.call("Grid", status=1), {"error": "OpenRouter HTTP 503"})
