import unittest

from experiments.evaluate_retrieval import first_hit, summarize


def span(source, start, end):
    return {"source_id": source, "start_byte": start, "end_byte": end}


class RetrievalEvalTests(unittest.TestCase):
    def test_hits_need_an_overlapping_range_in_the_cited_source(self):
        citations = [span("s", 100, 120), span("t", 0, 10)]
        self.assertEqual(first_hit([span("s", 0, 100), span("u", 100, 120), span("s", 119, 200)], citations), 3)
        self.assertEqual(first_hit([span("t", 9, 12)], citations), 1)
        self.assertIsNone(first_hit([span("s", 120, 130), span("t", 10, 20)], citations))
        self.assertIsNone(first_hit([], citations))

    def test_summary_reports_rank_rates_and_reading_cost(self):
        run = lambda rank, tokens, to_hit: {"hit_rank": rank, "excerpts": 2 if tokens else 0,
                                            "tokens": tokens, "tokens_to_hit": to_hit}
        results = [{"candidates": 1, "scoped_candidates": 1, "finding": 2, "pipeline": run(1, 900, 400)},
                   {"candidates": 7, "scoped_candidates": None, "finding": 1, "pipeline": run(2, 1000, 800)},
                   {"candidates": None, "scoped_candidates": 3, "finding": None, "pipeline": run(None, 0, None)}]
        summary = summarize(results)
        self.assertEqual(summary["candidates"], {"hit@1": 0.333, "hit@5": 0.333, "hit@10": 0.667, "hit@50": 0.667})
        self.assertEqual(summary["finding"]["hit@1"], 0.333)
        self.assertEqual(summary["pipeline"], {"hit": 0.667, "first_excerpt_hit": 0.333, "empty": 1,
                                               "mean_tokens": 633, "median_tokens_to_hit": 600})
        self.assertNotIn("reranked", summary)


if __name__ == "__main__":
    unittest.main()
