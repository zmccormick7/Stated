"""Search → candidate scoring → RSE with penalties for unreturned passages."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .calibration import ScoreCalibration
from .reranking import Reranker, ScoredSegment
from .rse import ExtractedSegment, extract_segments
from .store import RecordStore


@dataclass(frozen=True)
class RetrievalResult:
    excerpts: tuple[ExtractedSegment, ...]
    scored_passages: tuple[ScoredSegment, ...]  # Raw provider scores, in search order.
    calibration: ScoreCalibration | None


def retrieve(db: RecordStore, query: str, *, reranker: Reranker,
             count_tokens: Callable[[str], int], source_ids: Iterable[str] | None = None,
             candidate_limit: int = 50,
             calibration: ScoreCalibration | None = None, **selection) -> RetrievalResult:
    """Score only search hits; unreturned intervening passages receive the RSE penalty."""
    if calibration is not None and calibration.model != getattr(reranker, "model", None):
        raise ValueError("Calibration model must match the reranker model")
    # Reject invalid budgets before performing paid calls, including on empty searches.
    extract_segments((), (), count_tokens=count_tokens, **selection)
    candidates = db.search_documents(query, source_ids=source_ids, limit=candidate_limit)
    intervening = db._rse_intervening_passages(candidates, selection.get("max_segment_passages", 32))
    raw = reranker.score(query, candidates) if candidates else ()
    scores = calibration.apply(raw) if calibration is not None else raw
    excerpts = extract_segments(candidates + intervening,
                                scores + (None,) * len(intervening),
                                count_tokens=count_tokens, **selection)
    return RetrievalResult(excerpts, tuple(ScoredSegment(s, score) for s, score in zip(candidates, raw)),
                           calibration)
