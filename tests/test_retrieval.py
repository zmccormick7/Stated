from math import exp
import unittest
from unittest.mock import patch

from stated import (DocumentContext, OpenRouterReranker, RecordStore, Reranker, SourceSegment,
                      extract_segments, retrieve)


class FixtureReranker(Reranker):
    model = "fixture"

    def _score(self, query, segments):
        return [0.9 if "needle" in segment.text else 0.05 for segment in segments]


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.db = RecordStore(":memory:")
        self.addCleanup(self.db.close)
        self.parts = ("needle A\n", "bridge\n", "needle B\n", "unrelated\n")
        self.source = self.db.ingest_source(description="Acme", document_text="".join(self.parts), author_id="agent")
        offset, ranges = 0, []
        for part in self.parts:
            end = offset + len(part.encode())
            ranges.append((offset, end))
            offset = end
        self.db.set_passages(self.source.id, ranges)
        self.seeds = self.db.search_documents("needle")

    def test_only_search_hits_are_scored_and_unreturned_bridge_is_penalized(self):
        other = self.db.ingest_source(description="Other", document_text="needle other", author_id="agent")
        self.db.chunk_source(other.id)
        reranker = FixtureReranker()
        score = reranker._score

        def inspect(query, segments):
            self.assertFalse(self.db._db.in_transaction)
            self.assertEqual({s.source_id for s in segments}, {self.source.id})
            self.assertEqual(segments, self.seeds)
            return score(query, segments)

        with patch.object(reranker, "_score", side_effect=inspect):
            result = retrieve(self.db, "needle", source_ids=[self.source.id], reranker=reranker,
                              count_tokens=len)
        excerpt, = result.excerpts
        self.assertEqual(excerpt.segment.text, "".join(self.parts[:3]))
        self.assertEqual(excerpt.token_count, len(excerpt.render()))
        self.assertAlmostEqual(excerpt.value, 0.9 - 0.18 - 0.18 + exp(-1 / 30) * 0.9 - 0.18)
        self.assertEqual([s.score for s in result.scored_passages], [0.9, 0.9])
        self.assertEqual(result.transformed_scores, (0.9, 0.9))

    def test_span_limit_bounds_gap_reads_without_a_neighbor_radius(self):
        text = "needle A\n" + "gap\n" * 6 + "needle B\n" + "outside\n"
        source = self.db.ingest_source(description="Long gap", document_text=text, author_id="agent")
        offset, ranges = 0, []
        for part in text.splitlines(keepends=True):
            ranges.append((offset, offset + len(part)))
            offset += len(part)
        self.db.set_passages(source.id, ranges)
        options = dict(source_ids=[source.id], reranker=FixtureReranker(), count_tokens=len,
                       irrelevant_chunk_penalty=0.05)
        with patch.object(self.db, "read_source_text", wraps=self.db.read_source_text) as read:
            result = retrieve(self.db, "needle", max_segment_passages=8, **options)
            self.assertEqual(read.call_count, 8)  # Two hits and six intervening passages; no exterior reads.
        excerpt, = result.excerpts
        self.assertEqual(excerpt.segment.text, text.removesuffix("outside\n"))
        self.assertAlmostEqual(excerpt.value, 0.9 + exp(-1 / 30) * 0.9 - 8 * 0.05)
        with patch.object(self.db, "read_source_text", wraps=self.db.read_source_text) as read:
            result = retrieve(self.db, "needle", max_segment_passages=7, **options)
            self.assertEqual(read.call_count, 2)  # This gap cannot fit; no need to read it.
        self.assertEqual([e.segment.text for e in result.excerpts], ["needle A\n", "needle B\n"])

    def test_gap_token_cost_and_penalty_can_prevent_a_join(self):
        options = dict(reranker=FixtureReranker(), count_tokens=len)
        for limits in ({"max_segment_tokens": 10}, {"max_total_tokens": 18},
                       {"irrelevant_chunk_penalty": 0.5, "minimum_value": 0.1}):
            with self.subTest(limits=limits):
                result = retrieve(self.db, "needle", **options, **limits)
                self.assertEqual([e.segment.text for e in result.excerpts], [self.parts[0], self.parts[2]])

    def test_search_context_and_gap_locators_survive_edits_during_gap_reads(self):
        context = self.db.set_document_context(self.source.id, title="Fresh", summary="", author_id="agent")
        self.seeds = self.db.search_documents("needle")
        read = self.db.read_source_text

        def edit_then_read(*args, **kwargs):
            self.db.set_document_context(self.source.id, title="Later", summary="", author_id="agent")
            self.db.set_passages(self.source.id, [(0, len("".join(self.parts).encode()))])
            return read(*args, **kwargs)

        with patch.object(self.db, "search_documents", return_value=self.seeds), \
                patch.object(self.db, "read_source_text", side_effect=edit_then_read):
            result = retrieve(self.db, "needle", reranker=FixtureReranker(),
                              count_tokens=len)
        excerpt, = result.excerpts
        self.assertEqual(excerpt.segment.text, "".join(self.parts[:3]))
        self.assertEqual(excerpt.segment.document_context, context)
        self.assertTrue(all(s.segment.document_context == context for s in result.scored_passages))
        with self.assertRaisesRegex(ValueError, "repeat document search"):
            self.db._rse_intervening_passages(self.seeds, 32)

    def test_invalid_options_empty_scope_and_stale_anchors_make_no_paid_calls(self):
        reranker = FixtureReranker()
        options = dict(reranker=reranker, count_tokens=len)
        with patch.object(reranker, "_score") as score:
            result = retrieve(self.db, "needle", source_ids=[], **options)
            self.assertEqual(result.excerpts, ())
            for invalid in ({"max_total_tokens": 0}, {"max_segment_passages": 0}, {"count_tokens": None}):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    retrieve(self.db, "needle", **{**options, **invalid})
            read = self.db._rse_intervening_passages

            def rechunk_then_read(*args, **kwargs):
                self.db.chunk_source(self.source.id, max_chars=4)
                return read(*args, **kwargs)

            with patch.object(self.db, "_rse_intervening_passages", side_effect=rechunk_then_read):
                with self.assertRaisesRegex(ValueError, "repeat document search"):
                    retrieve(self.db, "needle", **options)
            score.assert_not_called()

    def test_pipeline_transforms_hits_once_keeps_raw_scores_and_penalizes_gap(self):
        for model in OpenRouterReranker.MODELS:
            reranker = OpenRouterReranker(model, api_key="test")
            scores = tuple(reranker.transform(s) for s in (0.9, 0.8))
            with patch.object(reranker, "_score", return_value=[0.9, 0.8]), \
                    patch.object(reranker, "transform", wraps=reranker.transform) as transform:
                result = retrieve(self.db, "needle", reranker=reranker, count_tokens=len)
            self.assertEqual([call.args[0] for call in transform.call_args_list], [0.9, 0.8])
            self.assertEqual(result.transformed_scores, scores)
            self.assertEqual([s.score for s in result.scored_passages], [0.9, 0.8])
            excerpt, = result.excerpts
            self.assertEqual(excerpt.segment.text, "".join(self.parts[:3]))
            self.assertAlmostEqual(excerpt.value, scores[0] + exp(-1 / 30) * scores[1] - 3 * 0.18)

    def test_balanced_minimum_and_selection_overrides_are_independent_of_transform(self):
        reranker = FixtureReranker()
        options = dict(reranker=reranker, count_tokens=len, candidate_limit=1)
        with patch.object(reranker, "_score", return_value=[0.6]):
            self.assertEqual(retrieve(self.db, "needle", **options).excerpts, ())  # 0.6 - 0.18 < 0.5
            excerpt, = retrieve(self.db, "needle", minimum_value=0.4, **options).excerpts
            self.assertAlmostEqual(excerpt.value, 0.42)
            # A different score mapping changes evidence utility, never the minimum.
            with patch.object(reranker, "transform", return_value=0.1):
                result = retrieve(self.db, "needle", **options)
            self.assertEqual(result.excerpts, ())


class TokenBudgetTests(unittest.TestCase):
    def test_counter_sees_complete_rendering_and_counts_can_decrease_with_more_text(self):
        context = DocumentContext("s", "H", "", "agent", "", {})
        chunks = [SourceSegment("s", 0, 1, "a", context), SourceSegment("s", 1, 2, "b", context)]
        counts = {"H\n\n\na": 3, "H\n\n\nb": 3, "H\n\n\nab": 2}
        result, = extract_segments(chunks, [0.95, 0.9], count_tokens=counts.__getitem__,
                                   max_segment_tokens=2, max_total_tokens=2)
        self.assertEqual(result.render(), "H\n\n\nab")
        self.assertEqual(result.token_count, 2)
        for invalid in (True, -1, 1.2):
            with self.assertRaises(ValueError):
                extract_segments(chunks, [0.95, 0.9], count_tokens=lambda _: invalid)

    def test_multiple_excerpts_charge_repeated_headers_and_respect_span_cap(self):
        context = DocumentContext("s", "H", "", "agent", "", {})
        chunks = [SourceSegment("s", i, i + 1, "x", context) for i in range(3)]
        results = extract_segments(chunks, [0.95] * 3, count_tokens=lambda text: len(text.encode()),
                                   max_total_tokens=10, max_segment_passages=1)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(r.token_count for r in results), 10)

    def test_real_bpe_counter_with_unicode_and_special_token_text(self):
        try:
            import tiktoken
        except ImportError:
            self.skipTest("Install the evaluation extra for a real BPE check")
        encoding = tiktoken.get_encoding("cl100k_base")
        count = lambda text: len(encoding.encode(text, disallowed_special=()))
        text = "Revenue €10. 中文 <|endoftext|>"
        context = DocumentContext("s", "Results", "Annual filing", "agent", "", {})
        chunk = SourceSegment("s", 0, len(text.encode()), text, context)
        expected = count(context.header + "\n\n" + text)
        result, = extract_segments([chunk], [0.95], count_tokens=count, max_total_tokens=expected)
        self.assertEqual(result.token_count, count(result.render()))
        self.assertEqual(extract_segments([chunk], [0.95], count_tokens=count, max_total_tokens=expected - 1), ())
