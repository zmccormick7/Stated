from dataclasses import replace
from math import exp
import unittest

from stated import DocumentContext, RecordStore, SourceSegment, extract_segments


def select(*args, **kwargs):
    # A one-character-per-token counter makes the original reference fixtures transparent.
    return extract_segments(*args, count_tokens=len, **kwargs)


def passages(parts, source_id="source", context=None):
    result, offset = [], 0
    for text in parts:
        end = offset + len(text.encode())
        result.append(SourceSegment(source_id, offset, end, text, context))
        offset = end
    return tuple(result)


class RSETests(unittest.TestCase):
    def test_bridges_unscored_and_weak_passages_but_trims_negative_edges(self):
        chunks = passages(["Noise.", "Revenue grew.", "Footnote.", "Margin rose.", "Noise."])
        for bridge_score in (None, 0.05):
            with self.subTest(bridge_score=bridge_score):
                result, = select(chunks, [0, 0.95, bridge_score, 0.9, 0])
                self.assertEqual(result.segment.text, "Revenue grew.Footnote.Margin rose.")
                self.assertEqual((result.segment.start_byte, result.segment.end_byte), (6, 40))
                bridge_value = -0.18 if bridge_score is None else exp(-2 / 30) * bridge_score - 0.18
                self.assertAlmostEqual(result.value, 0.95 - 0.18 + bridge_value + exp(-1 / 30) * 0.9 - 0.18)

    def test_long_unhelpful_gap_produces_disjoint_excerpts(self):
        chunks = passages(["Useful A"] + ["Noise"] * 10 + ["Useful B"])
        results = select(chunks, [0.95] + [None] * 10 + [0.9])
        self.assertEqual([result.segment.text for result in results], ["Useful A", "Useful B"])
        self.assertEqual(select(chunks, [None] * len(chunks)), ())
        self.assertEqual(select(chunks, [0.1] * len(chunks)), ())

    def test_ranking_and_source_order_are_distinct(self):
        first, second, third = passages(["A", "B", "C"])
        result, = select([third, first, second], [0.9, 0.8, 0.85])
        self.assertEqual(result.segment.text, "ABC")
        self.assertAlmostEqual(result.value, 0.9 + exp(-1 / 30) * 0.85 + exp(-2 / 30) * 0.8 - 0.54)

    def test_sources_gaps_and_context_changes_are_boundaries(self):
        context = DocumentContext("source", "Old title", "", "agent", "", {})
        changed = replace(context, title="New title")
        chunks = passages(["A", "B", "C"], context=context)
        inputs = [chunks[2], chunks[0], replace(chunks[1], document_context=changed)]
        inputs += list(passages(["D"], source_id="other"))
        results = select(inputs, [0.99] * 4)
        self.assertEqual([result.segment.text for result in results], ["C", "A", "B", "D"])
        gaps = select([chunks[2], chunks[0]], [0.95, 0.9])
        self.assertEqual([result.segment.text for result in gaps], ["C", "A"])

    def test_character_budgets_include_headers_for_every_excerpt(self):
        context = DocumentContext("source", "Title", "", "agent", "", {})
        chunks = passages(["€", "X"], context=context)  # Header + separator = 8 characters.
        result, = select(chunks, [0.95, 0.9], max_segment_tokens=10, max_total_tokens=10)
        self.assertEqual(result.segment.text, "€X")
        self.assertEqual(result.segment.end_byte, 4)
        two = select(chunks, [0.95, 0.9], max_segment_tokens=9, max_total_tokens=18)
        self.assertEqual([result.segment.text for result in two], ["€", "X"])
        one = select(chunks, [0.95, 0.9], max_segment_tokens=9, max_total_tokens=17)
        self.assertEqual(len(one), 1)
        self.assertEqual(select(chunks, [0.95, 0.9], max_total_tokens=8), ())

    def test_limits_and_ties_preserve_greedy_selection_without_overlap(self):
        chunks = passages(["a", "b", "c", "d"])
        results = select(chunks, [0.95] * 4, max_segment_tokens=2, max_total_tokens=3)
        self.assertEqual([result.segment.text for result in results], ["ab", "c"])
        self.assertEqual(len(select(chunks, [0.95] * 4, max_segment_tokens=1, max_segments=2)), 2)
        # Rank decay makes the second equal score slightly smaller; round-off creates an exact tie here.
        tied = select(list(reversed(chunks)), [0.95] * 4, decay_rate=1e30, max_segment_tokens=1)
        self.assertEqual([result.segment.text for result in tied], ["a", "b", "c", "d"])

    def test_length_adjustment_and_inclusive_minimum(self):
        chunks = passages(["a" * 1400])
        adjusted, = select(chunks, [0.9], minimum_value=1.44)
        self.assertAlmostEqual(adjusted.value, 1.44)
        self.assertEqual(select(chunks, [0.9], minimum_value=1.44,
                                         chunk_length_adjustment=False), ())
        plain, = select(chunks, [0.9], minimum_value=0.72, chunk_length_adjustment=False)
        self.assertAlmostEqual(plain.value, 0.72)

    def test_search_to_extraction_preserves_canonical_text_after_rechunking(self):
        with RecordStore(":memory:") as db:
            text = "Revenue €10.\r\nRevenue €20."
            source = db.ingest_source(description="Acme", document_text=text, author_id="agent")
            boundary = len("Revenue €10.\r\n".encode())
            db.set_passages(source.id, [(0, boundary), (boundary, len(text.encode()))])
            old_context = db.set_document_context(source.id, title="Acme", summary="", author_id="agent")
            candidates = db.search_documents("Revenue")
            db.chunk_source(source.id, max_chars=5)
            db.set_document_context(source.id, title="New context", summary="", author_id="agent")
            result, = select(candidates, [0.95, 0.9])
            segment = result.segment
            self.assertEqual(segment.text, text)
            self.assertEqual(segment.document_context, old_context)
            self.assertEqual(segment.text, db.read_source_text(
                source.id, start_byte=segment.start_byte, end_byte=segment.end_byte))

    def test_bad_scores_parameters_and_empty_inputs(self):
        chunk = passages(["a"])
        self.assertEqual(select([], []), ())
        for scores in ([], [0, 1], [True], ["0.9"], [float("nan")], [float("inf")], [-0.1], [1.1]):
            with self.subTest(scores=scores), self.assertRaises(ValueError):
                select(chunk, scores)
        for name in ("max_segment_tokens", "max_total_tokens", "max_segments", "decay_rate",
                     "minimum_value", "irrelevant_chunk_penalty"):
            for value in (0, -1, True, float("nan"), float("inf")):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    select(chunk, [0.9], **{name: value})
        with self.assertRaises(ValueError):
            select(chunk, [0.9], chunk_length_adjustment=1)

    def test_invalid_locators_overlaps_and_mismatched_context_are_rejected(self):
        chunk, = passages(["€"])
        bad = [None, replace(chunk, source_id=""), replace(chunk, start_byte=-1),
               replace(chunk, start_byte=True), replace(chunk, end_byte=0),
               replace(chunk, end_byte=1), replace(chunk, text=""), replace(chunk, text=None),
               replace(chunk, document_context={}),
               replace(chunk, document_context=DocumentContext("other", "Title", "", "agent", "", {}))]
        for candidate in bad:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                select([candidate], [0.9])
        for other in (chunk, replace(chunk, start_byte=1, end_byte=4)):
            with self.assertRaisesRegex(ValueError, "overlap"):
                select([chunk, other], [0.9, 0.9])
