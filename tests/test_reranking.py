from io import BytesIO
import json
from threading import Event
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from premisedb import DocumentContext, OpenRouterReranker, RecordStore, Reranker, SourceSegment


class SuppliedReranker(Reranker):
    def __init__(self, scores):
        self.scores = scores

    def _score(self, query, segments):
        return self.scores


class RerankingTests(unittest.TestCase):
    def setUp(self):
        context = DocumentContext("a", "Acme FY2025", "Annual results", "agent", "", {})
        self.segments = (SourceSegment("a", 7, 11, "€\n", context),
                         SourceSegment("b", 0, 5, "Other"))

    def provider(self, model="voyageai/rerank-2.5", **options):
        return OpenRouterReranker(model, api_key="test-key", **options)

    def response(self, value):
        return BytesIO(json.dumps(value).encode())

    def test_custom_scorer_preserves_retrieved_evidence_and_ties(self):
        with RecordStore(":memory:") as db:
            text = "Revenue €10.\r\nRevenue €20."
            source = db.ingest_source(description="Acme", document_text=text, author_id="agent")
            boundary = len("Revenue €10.\r\n".encode())
            db.set_passages(source.id, [(0, boundary), (boundary, len(text.encode()))])
            candidates = db.search_documents("revenue")
            ranked = SuppliedReranker([0.1, 0.9]).rerank("revenue", candidates)
            self.assertIs(ranked[0].segment, candidates[1])
            self.assertEqual([hit.score for hit in ranked], [0.9, 0.1])
            for hit in ranked:
                segment = hit.segment
                self.assertEqual(segment.text, db.read_source_text(
                    segment.source_id, start_byte=segment.start_byte, end_byte=segment.end_byte))
            tied = SuppliedReranker([0.5, 0.5]).rerank("revenue", candidates)
            self.assertEqual(tuple(hit.segment for hit in tied), candidates)
            self.assertEqual(db.search_documents("revenue"), candidates)

    def test_contract_rejects_partial_and_invalid_scores(self):
        with self.assertRaises(TypeError):
            Reranker()
        for scores in ([], [0.5], [0, 1, 1], [True, 0], ["0.5", 0], [None, 0],
                       [float("nan"), 0], [float("inf"), 0], [-0.01, 0], [1.01, 0]):
            with self.subTest(scores=scores), self.assertRaises(ValueError):
                SuppliedReranker(scores).rerank("revenue", self.segments)
        self.assertEqual(SuppliedReranker([0, 1]).score("revenue", self.segments), (0.0, 1.0))

    def test_no_requests_for_empty_candidates_or_invalid_inputs(self):
        with patch("premisedb.reranking.urlopen") as request:
            for model in OpenRouterReranker.MODELS:
                provider = self.provider(model)
                self.assertEqual(provider.rerank("revenue", ()), ())
                for query in (None, 12, "", "   "):
                    with self.assertRaises(ValueError):
                        provider.rerank(query, self.segments)
                with self.assertRaises(ValueError):
                    provider.rerank("revenue", ["not a segment"])
            request.assert_not_called()

    def test_batch_models_map_indices_and_keep_context_separate_from_text(self):
        for model in ("cohere/rerank-4-pro", "voyageai/rerank-2.5"):
            response = {"results": [{"index": 1, "relevance_score": 0.9},
                                    {"index": 0, "relevance_score": 0.2}]}
            with self.subTest(model=model), patch("premisedb.reranking.urlopen",
                                                  return_value=self.response(response)) as request:
                self.assertEqual(self.provider(model, timeout=7).score("revenue", self.segments), (0.2, 0.9))
                request.assert_called_once()
                sent = request.call_args.args[0]
                self.assertEqual(sent.full_url, "https://openrouter.ai/api/v1/rerank")
                self.assertEqual(sent.get_header("Authorization"), "Bearer test-key")
                self.assertEqual(request.call_args.kwargs, {"timeout": 7})
                payload = json.loads(sent.data)
                self.assertEqual((payload["model"], payload["query"], payload["top_n"]), (model, "revenue", 2))
                self.assertEqual([json.loads(text) for text in payload["documents"]], [
                    {"document_context": "Acme FY2025\nAnnual results", "passage": "€\n"},
                    {"document_context": "", "passage": "Other"},
                ])

    def test_jev_keeps_input_order_when_requests_finish_out_of_order(self):
        second_finished = Event()

        def respond(request, timeout):
            self.assertEqual(request.full_url, "https://openrouter.ai/api/alpha/decisions")
            payload = json.loads(request.data)
            self.assertEqual(payload["model"], "typesafe/jev-1.13")
            self.assertEqual(payload["state"]["query"], "revenue")
            self.assertEqual(payload["questions"]["relevance"]["type"], "noul")
            if payload["state"]["passage"] == "€\n":
                self.assertEqual(payload["state"]["document_context"], "Acme FY2025\nAnnual results")
                self.assertTrue(second_finished.wait(2))
                score = 0.2
            else:
                score = 0.9
                second_finished.set()
            return self.response({"answers": {"relevance": {"type": "noul", "noul": score}}})

        with patch("premisedb.reranking.urlopen", side_effect=respond) as request:
            scores = self.provider("typesafe/jev-1.13", max_workers=2).score("revenue", self.segments)
            self.assertEqual(scores, (0.2, 0.9))
            self.assertEqual(request.call_count, 2)

    def test_batch_rejects_missing_duplicate_and_malformed_results(self):
        row = {"index": 0, "relevance_score": 0.5}
        invalid = [None, {}, {"results": None}, {"results": []}, {"results": [row]},
                   {"results": [row, row]}, {"results": [{"index": 0}]}]
        invalid += [{"results": [row, {"index": index, "relevance_score": 0.9}]}
                    for index in (-1, 2, True, "1", 1.0)]
        for value in invalid:
            with self.subTest(value=value), patch("premisedb.reranking.urlopen",
                                                  return_value=self.response(value)):
                with self.assertRaises(ValueError):
                    self.provider().rerank("revenue", self.segments)

    def test_jev_rejects_bad_answers_and_partial_failures(self):
        valid = {"answers": {"relevance": {"type": "noul", "noul": 0.9}}}
        for bad in ({}, {"type": "score", "noul": 0.9}, {"type": "noul", "noul": True}):
            with patch("premisedb.reranking.urlopen", side_effect=[
                self.response(valid), self.response({"answers": {"relevance": bad}}),
            ]), self.assertRaises(ValueError):
                self.provider("typesafe/jev-1.13", max_workers=1).rerank("revenue", self.segments)

    def test_provider_failures_raise_without_response_body_or_retry(self):
        errors = [HTTPError("url", 429, "sensitive body", {}, BytesIO(b"sensitive body")),
                  URLError("sensitive body"), TimeoutError("sensitive body")]
        for error in errors:
            with patch("premisedb.reranking.urlopen", side_effect=error) as request:
                with self.assertRaises(RuntimeError) as caught:
                    self.provider().rerank("revenue", self.segments)
                self.assertNotIn("sensitive body", str(caught.exception))
                request.assert_called_once()
        with patch("premisedb.reranking.urlopen", return_value=BytesIO(b"not JSON")):
            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                self.provider().rerank("revenue", self.segments)

    def test_configuration_and_environment_key(self):
        for options in ({"model": "unknown"}, {"timeout": 0}, {"timeout": float("nan")},
                        {"timeout": float("inf")}, {"timeout": True}, {"max_workers": 0},
                        {"max_workers": True}, {"max_workers": 1.5}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.provider(**options)
        with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(ValueError, "API_KEY"):
            OpenRouterReranker("typesafe/jev-1.13")
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "env-key"}):
            provider = OpenRouterReranker("typesafe/jev-1.13")
            with patch("premisedb.reranking.urlopen", return_value=self.response({
                "answers": {"relevance": {"type": "noul", "noul": 0.8}},
            })) as request:
                provider.score("revenue", self.segments[:1])
                self.assertEqual(request.call_args.args[0].get_header("Authorization"), "Bearer env-key")
