import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from experiments.compare_rerankers import metrics, ordered_scores, request_payload, rerank


class RerankerComparisonTests(unittest.TestCase):
    def test_payload_preserves_context_and_excludes_annotations(self):
        cases = [{"query": "Revenue?", "annotated_support": True, "score": 0.9,
                  "segment": {"text": "Revenue grew €10m.",
                              "document_context": {"title": "Issuer", "summary": "Annual report"}}}]
        rubric = {"instructions": "Include contrary evidence.",
                  "criteria": {"true": "Provides evidence", "false": "Only shares a topic"}}
        payload = request_payload("model", cases, rubric)
        self.assertEqual(json.loads(payload["documents"][0]), {
            "document_context": "Issuer\nAnnual report", "passage": "Revenue grew €10m."})
        self.assertNotIn("annotated_support", json.dumps(payload))
        self.assertIn(rubric["instructions"], payload["query"])
        self.assertEqual(payload["top_n"], 1)
        self.assertEqual(request_payload("model", cases, rubric, plain_query=True)["query"], "Revenue?")
        with self.assertRaises(ValueError):
            request_payload("model", cases + [{**cases[0], "query": "Other query"}], rubric)

    def test_provider_order_maps_back_to_original_candidates(self):
        result = {"results": [{"index": 1, "relevance_score": 0.9},
                              {"index": 0, "relevance_score": 0.2}]}
        self.assertEqual(ordered_scores(result, 2), [0.2, 0.9])

    def test_incomplete_duplicate_invalid_indices_and_scores_fail(self):
        good = {"index": 0, "relevance_score": 0.2}
        for rows in ([], [good, good], [{**good, "index": True}], [{**good, "index": -1}],
                     [{**good, "index": 1}], *[[{**good, "relevance_score": x}]
                     for x in (True, -0.1, 1.1, "0.9", float("nan"), float("inf"))]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                ordered_scores({"results": rows}, 1)

    def test_metrics_count_all_required_support_and_missing_queries(self):
        cases = [{"group": "a", "bm25_rank": i + 1, "annotated_support": gold}
                 for i, gold in enumerate([True, False, True])]
        result = metrics(cases, [0.9, 0.8, 0.1], {"a": 2, "missing": 1})
        self.assertEqual(result["top1"], 0.5)
        self.assertEqual(result["all_support@3"], 0.5)
        self.assertEqual(result["recall>=0.5"], 0.25)
        self.assertNotIn("recall>=0.5", metrics(cases, [-1, -2, -3], {"a": 2}, thresholds=()))
        # A gold passage missing from retrieval must stay in the recall denominator.
        self.assertAlmostEqual(metrics(cases, [0.9, 0.8, 0.1], {"a": 3})["recall@3"], 2 / 3)
        with self.assertRaises(ValueError):
            metrics(cases, [0.9], {"a": 2})

    def test_ties_keep_bm25_order(self):
        cases = [{"group": "a", "bm25_rank": 2, "annotated_support": True},
                 {"group": "a", "bm25_rank": 1, "annotated_support": False}]
        self.assertEqual(metrics(cases, [0.5, 0.5], {"a": 1})["mrr"], 0.5)

    def test_transport_records_metadata_and_sanitizes_http_errors(self):
        response = {"results": [{"index": 0, "relevance_score": 0.8}], "model": "served",
                    "id": "request", "provider": "provider", "usage": {"cost": 0.001}}
        payload = {"documents": ["evidence"]}
        with patch("experiments.compare_rerankers.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
            result = rerank(payload, "test-key")
        self.assertEqual(result["scores"], [0.8])
        self.assertEqual(result["model"], "served")
        self.assertEqual(result["usage"]["cost"], 0.001)
        error = HTTPError("https://openrouter.ai", 401, "sensitive body", {}, io.BytesIO(b"secret"))
        with patch("experiments.compare_rerankers.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "^OpenRouter HTTP 401$"):
                rerank(payload, "test-key")
