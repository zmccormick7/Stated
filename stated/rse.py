"""Single-query Relevant Segment Extraction; dsRAG attribution in NOTICE."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import exp, isfinite

from .store import DocumentContext, SourceSegment


@dataclass(frozen=True)
class ExtractedSegment:
    segment: SourceSegment
    value: float  # Sum of passage utilities, not a probability or reranker score.
    token_count: int

    def render(self) -> str:
        """The exact string charged to the token budget; context stays separately identified."""
        context = self.segment.document_context
        return (context.header + "\n\n" if context else "") + self.segment.text


def extract_segments(segments: Sequence[SourceSegment], scores: Sequence[float | None], *,
                     count_tokens: Callable[[str], int],
                     max_segment_tokens: int = 2000, max_total_tokens: int = 6000,
                     max_segments: int = 5, max_segment_passages: int = 32,
                     irrelevant_chunk_penalty: float = 0.18,
                     minimum_value: float = 0.5, decay_rate: float = 30,
                     chunk_length_adjustment: bool = True) -> tuple[ExtractedSegment, ...]:
    """Greedily select contiguous evidence spans from aligned passages and scores.

    Scoring defaults follow dsRAG's balanced preset; budgets use tokens.
    None scores penalize supplied but unscored passages. Missing byte ranges are
    hard boundaries. Budgets use the supplied tokenizer, including a header and
    two separating newlines per returned span. No storage or model calls occur.
    """
    if not callable(count_tokens):
        raise ValueError("count_tokens must be callable")
    for name, value in (("max_segment_tokens", max_segment_tokens),
                        ("max_total_tokens", max_total_tokens), ("max_segments", max_segments),
                        ("max_segment_passages", max_segment_passages)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    for name, value in (("irrelevant_chunk_penalty", irrelevant_chunk_penalty),
                        ("minimum_value", minimum_value), ("decay_rate", decay_rate)):
        if type(value) not in (int, float) or not isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if type(chunk_length_adjustment) is not bool:
        raise ValueError("chunk_length_adjustment must be a boolean")
    segments, scores = tuple(segments), tuple(scores)
    if len(segments) != len(scores) or any(
        score is not None and (type(score) not in (int, float) or not 0 <= score <= 1)
        for score in scores
    ):
        raise ValueError("Expected one finite score in [0, 1], or None, per passage")
    by_source = {}
    for index, segment in enumerate(segments):
        if (not isinstance(segment, SourceSegment) or not isinstance(segment.source_id, str)
                or not segment.source_id or not isinstance(segment.text, str)
                or type(segment.start_byte) is not int or type(segment.end_byte) is not int
                or not 0 <= segment.start_byte < segment.end_byte
                or segment.end_byte - segment.start_byte != len(segment.text.encode("utf-8"))):
            raise ValueError("Expected nonempty passages with valid UTF-8 byte lengths")
        if segment.document_context is not None and (
            not isinstance(segment.document_context, DocumentContext)
            or segment.document_context.source_id != segment.source_id
        ):
            raise ValueError("Document context must belong to the passage's Source")
        by_source.setdefault(segment.source_id, []).append(index)

    # Rank once across the supplied scored candidates; ties keep input order.
    ranked = sorted((i for i, score in enumerate(scores) if score is not None), key=lambda i: -scores[i])
    values = [-irrelevant_chunk_penalty] * len(segments)
    for rank, index in enumerate(ranked):
        scale = max(1, len(segments[index].text) / 700) if chunk_length_adjustment else 1
        values[index] = (exp(-rank / decay_rate) * scores[index] - irrelevant_chunk_penalty) * scale

    # Sort by source position, but never join across unavailable text or changed context.
    blocks = []
    for indices in by_source.values():
        previous = None
        for index in sorted(indices, key=lambda i: segments[i].start_byte):
            segment = segments[index]
            if previous is not None and segment.start_byte < previous.end_byte:
                raise ValueError("Passages from one Source must not overlap or repeat")
            if (previous is None or segment.start_byte != previous.end_byte
                    or segment.document_context != previous.document_context):
                blocks.append([])
            blocks[-1].append(index)
            previous = segment

    used, results, remaining, token_counts = set(), [], max_total_tokens, {}
    while len(results) < max_segments:
        best = None
        best_value = float("-inf")
        for block_index, block in enumerate(blocks):
            context = segments[block[0]].document_context
            header = context.header + "\n\n" if context else ""
            for start, index in enumerate(block):
                if index in used or values[index] < 0:
                    continue
                value = 0.0
                for end in range(start, min(start + max_segment_passages, len(block))):
                    index = block[end]
                    if index in used:
                        break
                    value += values[index]
                    if values[index] >= 0 and value >= minimum_value and value > best_value:
                        key = (block_index, start, end)
                        if key not in token_counts:
                            text = header + "".join(segments[i].text for i in block[start:end + 1])
                            size = count_tokens(text)
                            if type(size) is not int or size < 0:
                                raise ValueError("count_tokens must return a nonnegative integer")
                            token_counts[key] = size
                        size = token_counts[key]
                        if size > min(max_segment_tokens, remaining):
                            continue  # Adding text can reduce BPE token counts; do not break.
                        best, best_value = (block, start, end + 1, size), value
        if best is None:
            break
        block, start, end, size = best
        selected = block[start:end]
        first, last = segments[selected[0]], segments[selected[-1]]
        segment = SourceSegment(first.source_id, first.start_byte, last.end_byte,
                                "".join(segments[i].text for i in selected), first.document_context)
        results.append(ExtractedSegment(segment, best_value, size))
        used.update(selected)
        remaining -= size
    return tuple(results)
