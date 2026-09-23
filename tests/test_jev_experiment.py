import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from experiments.jev_rerank import finqa_candidates, score_passage, summarize
from premisedb import DocumentContext, RecordStore, SourceSegment


class JevExperimentTests(unittest.TestCase):
    def test_provider_payload_and_score_preserve_context_and_evidence(self):
        context = DocumentContext("s", "Issuer", "Document summary", "agent", "now", {})
        segment = SourceSegment("s", 12, 20, "Evidence", context)
        response = {"answers": {"relevance": {"type": "noul", "noul": 0.81}},
                    "model": "served-snapshot", "usage": {"cost": 0.001}, "id": "request"}
        with patch("experiments.jev_rerank.urlopen", return_value=io.BytesIO(json.dumps(response).encode())) as send:
            result = score_passage("Question", segment, "test-key")
        payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload["state"], {"query": "Question", "passage": "Evidence",
                                          "document_context": "Issuer\nDocument summary"})
        self.assertEqual(result["score"], 0.81)
        self.assertEqual(result["model"], "served-snapshot")
        self.assertEqual(segment.start_byte, 12)

    def test_invalid_scores_and_http_errors_fail_without_leaking_provider_body(self):
        segment = SourceSegment("s", 0, 1, "x")
        for score in (True, -0.1, 1.1, float("nan"), float("inf"), "0.9"):
            with self.subTest(score=score):
                body = {"answers": {"relevance": {"type": "noul", "noul": score}}}
                with patch("experiments.jev_rerank.urlopen", return_value=io.BytesIO(json.dumps(body).encode())):
                    with self.assertRaises(ValueError):
                        score_passage("q", segment, "test-key")
        error = HTTPError("https://openrouter.ai", 401, "private body", {}, io.BytesIO(b"secret"))
        with patch("experiments.jev_rerank.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "^OpenRouter HTTP 401$"):
                score_passage("q", segment, "test-key")

    def test_support_mapping_uses_post_text_offset_and_whole_table(self):
        row = {"id": "FAKE/2025/page_1.pdf-1", "pre_text": ["An introduction."],
               "post_text": ["Revenue grew €10m."],
               "table": [["Metric", "2025"], ["Revenue", "100"]],
               "qa": {"question": "revenue", "gold_inds": {"text_1": "unused", "table_1": "unused"}}}
        with RecordStore(":memory:") as db:
            cases = finqa_candidates(db, [row])
            self.assertEqual(len(cases), 2)
            self.assertTrue(all(case["annotated_support"] for case in cases))
            for case in cases:
                segment = case["segment"]
                self.assertEqual(segment.text, db.read_source_text(
                    segment.source_id, start_byte=segment.start_byte, end_byte=segment.end_byte))

    def test_metrics_include_missing_support_and_exclude_partial_failures(self):
        rows = [
            {"group": "a", "bm25_rank": 1, "annotated_support": False, "score": 0.1},
            {"group": "a", "bm25_rank": 2, "annotated_support": True, "score": 0.9},
            {"group": "failed", "error": "RuntimeError"},
        ]
        for row in rows[:2]:
            row.update(usage={"cost": 0.001}, seconds=0.2, model="snapshot")
        summary = summarize(rows, ["a", "missing", "failed"])
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["bm25"]["queries"], 2)
        self.assertEqual(summary["bm25"]["mrr"], 0.25)
        self.assertEqual(summary["jev"]["mrr"], 0.5)
        self.assertEqual(summary["jev"]["support_in_pool"], 0.5)


if __name__ == "__main__":
    unittest.main()
