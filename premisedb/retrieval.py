"""Search → calibrated candidate scoring → RSE with penalties for unreturned passages."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .calibration import ScoreCalibration, calibration_for
from .reranking import Reranker, ScoredSegment
from .rse import ExtractedSegment, extract_segments
from .store import RecordStore


@dataclass(frozen=True)
class RetrievalResult:
    excerpts: tuple[ExtractedSegment, ...]
    scored_passages: tuple[ScoredSegment, ...]  # Raw provider scores, in search order.
    calibration: ScoreCalibration


def retrieve(db: RecordStore, query: str, *, reranker: Reranker,
             count_tokens: Callable[[str], int], source_ids: Iterable[str] | None = None,
             candidate_limit: int = 50,
             calibration: ScoreCalibration | None = None, **selection) -> RetrievalResult:
    """Score only search hits; unreturned intervening passages receive the RSE penalty."""
    model = getattr(reranker, "model", None)
    calibration = calibration or calibration_for(model)
    if calibration.model != model:
        raise ValueError("Calibration model must match the reranker model")
    options = {"irrelevant_chunk_penalty": calibration.irrelevant_chunk_penalty,
               "minimum_value": calibration.minimum_value, **selection}
    # Reject invalid budgets before performing paid calls, including on empty searches.
    extract_segments((), (), count_tokens=count_tokens, **options)
    candidates = db.search_documents(query, source_ids=source_ids, limit=candidate_limit)
    intervening = db._rse_intervening_passages(candidates, options.get("max_segment_passages", 32))
    raw = reranker.score(query, candidates) if candidates else ()
    excerpts = extract_segments(candidates + intervening,
                                calibration.apply(raw) + (None,) * len(intervening),
                                count_tokens=count_tokens, **options)
    return RetrievalResult(excerpts, tuple(ScoredSegment(s, score) for s, score in zip(candidates, raw)),
                           calibration)
