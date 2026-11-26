"""Search → candidate scoring → RSE with penalties for unreturned passages."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .embedding import Embedder
from .reranking import Reranker, ScoredSegment
from .rse import ExtractedSegment, extract_segments
from .store import RecordStore, SourceSegment


@dataclass(frozen=True)
class RetrievalResult:
    excerpts: tuple[ExtractedSegment, ...]
    scored_passages: tuple[ScoredSegment, ...]  # Raw provider scores, in search order.
    transformed_scores: tuple[float, ...]  # RSE inputs, aligned with scored_passages.
    stage_one: str = "keyword"  # How the candidates were chosen: "every passage", "embedding" or "keyword".


def candidates(db: RecordStore, query: str, *, source_ids: Iterable[str] | None = None,
               embedder: Embedder | None = None, limit: int = 50) -> tuple[tuple[SourceSegment, ...], str]:
    """The passages to hand the reranker, and how they were chosen.

    A search inside named Sources takes every passage when there are no more than `limit`: the reranker
    reads them all, and nothing is lost to a first cut. With more, it takes the nearest by embedding when
    every one of those passages has an embedding of the embedder's model. Otherwise, as for a search of
    everything, passages are found by the query's words. A failed embedding call falls back to words.
    """
    if source_ids is not None:
        source_ids = tuple(source_ids)
        passages, embedded = db.count_passages(source_ids)
        if passages <= limit:
            return db.every_passage(source_ids), "every passage"
        if embedder is not None and embedded == passages and db.vector_model() == embedder.model:
            try:
                return db.nearest_passages(embedder.embed([query])[0], source_ids=source_ids, limit=limit), "embedding"
            except RuntimeError:
                pass
    return db.search_documents(query, source_ids=source_ids, limit=limit), "keyword"


def retrieve(db: RecordStore, query: str, *, reranker: Reranker,
             count_tokens: Callable[[str], int], source_ids: Iterable[str] | None = None,
             about: str | None = None, embedder: Embedder | None = None,
             candidate_limit: int = 50, **selection) -> RetrievalResult:
    """Score only the candidates; unreturned intervening passages receive the RSE penalty.

    `about` searches every Source about one record, such as a company, in place of named Sources.
    """
    # Reject invalid budgets before performing paid calls, including on empty searches.
    extract_segments((), (), count_tokens=count_tokens, **selection)
    if about is not None:
        if source_ids is not None:
            raise ValueError("give source_ids or about, not both")
        source_ids = db.source_ids_about(about)
    found, stage_one = candidates(db, query, source_ids=source_ids, embedder=embedder, limit=candidate_limit)
    intervening = db._rse_intervening_passages(found, selection.get("max_segment_passages", 32))
    raw = reranker.score(query, found) if found else ()
    scores = tuple(reranker.transform(score) for score in raw)
    excerpts = extract_segments(found + intervening,
                                scores + (None,) * len(intervening),
                                count_tokens=count_tokens, **selection)
    return RetrievalResult(excerpts, tuple(ScoredSegment(s, score) for s, score in zip(found, raw)),
                           scores, stage_one)
