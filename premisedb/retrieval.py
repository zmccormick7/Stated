"""A bounded search → neighbors → calibrated scoring → RSE pipeline."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .calibration import ScoreCalibration, calibration_for
from .reranking import Reranker, ScoredSegment
from .rse import ExtractedSegment, extract_segments
from .store import RecordStore


@dataclass(frozen=True)
class RetrievalResult:
    excerpts: tuple[ExtractedSegment, ...]
    scored_passages: tuple[ScoredSegment, ...]  # Raw provider scores, in expansion order.
    calibration: ScoreCalibration
    neighbors_truncated: bool


def retrieve(db: RecordStore, query: str, *, reranker: Reranker,
             count_tokens: Callable[[str], int], source_ids: Iterable[str] | None = None,
             candidate_limit: int = 50, neighbor_radius: int = 2, max_passages: int = 250,
             calibration: ScoreCalibration | None = None, **selection) -> RetrievalResult:
    """Score every fetched passage; no SQLite transaction spans a provider call."""
    model = getattr(reranker, "model", None)
    calibration = calibration or calibration_for(model)
    if calibration.model != model:
        raise ValueError("Calibration model must match the reranker model")
    options = {"irrelevant_chunk_penalty": calibration.irrelevant_chunk_penalty,
               "minimum_value": calibration.minimum_value, **selection}
    # Reject invalid budgets before performing paid calls, including on empty searches.
    extract_segments((), (), count_tokens=count_tokens, **options)
    candidates = db.search_documents(query, source_ids=source_ids, limit=candidate_limit)
    pool = db.expand_passages(candidates, radius=neighbor_radius, limit=max_passages)
    raw = reranker.score(query, pool.segments) if pool.segments else ()
    excerpts = extract_segments(pool.segments, calibration.apply(raw), count_tokens=count_tokens, **options)
    return RetrievalResult(excerpts, tuple(ScoredSegment(s, score) for s, score in zip(pool.segments, raw)),
                           calibration, pool.has_more)
