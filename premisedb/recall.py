"""Connected recall: entry points → bounded walk → selected records with evidence and issues.

Increment 1 of docs/connected-recall.md: graph-only, deterministic and without provider calls.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from .store import Link, Record, RecordStore

# Weight per followed relation; a reached record scores entry score × (weight × HOP_DISCOUNT) per hop.
# Reasoning relations outrank `about`; `in_layer` and `supported_by` are not walked.
RELATION_WEIGHTS = {
    "depends_on": 1.0, "qualifies": 1.0, "contradicts": 1.0, "corroborates": 0.9, "updates": 0.9,
    "answered_by": 0.9, "partly_answered_by": 0.9, "raises": 0.8, "shares_driver": 0.6, "in_model": 0.6,
    "about": 0.4,
}
HOP_DISCOUNT = 0.5
RRF_K = 60  # Reciprocal rank fusion constant.
CHANNEL_WEIGHTS = {"seed": 2.0, "search": 1.0}  # Seeds the caller chose come before search hits.
FRONTIER_LIMIT = 50
ISSUE_RELATIONS = ("contradicts", "qualifies", "updates")
QUESTION_RELATIONS = ("raises", "answered_by", "partly_answered_by", "depends_on", "qualifies")
OPEN_STATUSES = ("open", "partly_answered")


@dataclass(frozen=True)
class Recalled:
    record: Record
    score: float
    entry_id: str  # The entry point the best path starts from.
    channels: tuple[str, ...]  # How that entry point was found: "seed" and/or "search".
    path: tuple[Link, ...]  # Links from the entry point; empty for an entry point itself.


@dataclass(frozen=True)
class Evidence:
    source_id: str
    start_byte: int
    end_byte: int
    quote: str
    record_ids: tuple[str, ...]  # Selected records citing this range.


@dataclass(frozen=True)
class Recall:
    records: tuple[Recalled, ...]  # Best first.
    evidence: tuple[Evidence, ...]
    issue_links: tuple[Link, ...]  # contradicts, qualifies and updates links touching selected records.
    open_questions: tuple[Record, ...]  # Linked Questions whose status is open or partly answered.
    frontier: tuple[str, ...]  # Reached but not selected, best first, then capped records.
    coverage: dict[str, object]


def recall(db: RecordStore, query: str, *, seeds: Iterable[str] = (), max_records: int = 40,
           entry_limit: int = 10) -> Recall:
    """Assemble connected research around a query from record search, seeds and a bounded walk."""
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    if isinstance(seeds, (str, bytes)):
        raise ValueError("seeds must be a list of record IDs")
    for name, value in (("max_records", max_records), ("entry_limit", entry_limit)):
        if type(value) is not int or not 1 <= value <= 100:
            raise ValueError(f"{name} must be an integer from 1 to 100")
    seed_ids = list(dict.fromkeys(db.resolve_id(seed) for seed in seeds))
    hits = [record.id for record in db.search_records(query, limit=entry_limit)]

    # Entry points: reciprocal rank fusion over the seed and search channels.
    entries: dict[str, tuple[float, tuple[str, ...]]] = {}
    for channel, ranked in (("seed", seed_ids), ("search", hits)):
        for rank, record_id in enumerate(ranked, 1):
            score, channels = entries.get(record_id, (0.0, ()))
            entries[record_id] = (score + CHANNEL_WEIGHTS[channel] / (RRF_K + rank), channels + (channel,))

    # Walk each entry point: reasoning relations to two hops, `about` for a single hop.
    best: dict[str, Recalled] = {}
    capped: list[str] = []
    walks = ((2, [name for name in RELATION_WEIGHTS if name != "about"]), (1, ["about"]))
    for entry_id, (entry_score, channels) in entries.items():
        for depth, relations in walks:
            walk = db.expand([entry_id], depth=depth, relations=relations, limit=200)
            capped += [record_id for record_id in walk.capped if record_id not in capped]
            for reached in walk.reached:
                score = entry_score
                for link in reached.path:
                    score *= RELATION_WEIGHTS[link.relation] * HOP_DISCOUNT
                current = best.get(reached.record.id)
                if current is None or score > current.score:
                    best[reached.record.id] = Recalled(reached.record, score, entry_id, channels, reached.path)

    ranked = sorted(best.values(), key=lambda r: (-r.score, len(r.path), r.record.id))
    selected, rest = ranked[:max_records], ranked[max_records:]
    chosen = {r.record.id for r in selected}

    # Evidence: cited ranges of selected records, with quotes read from the source when absent.
    evidence: dict[tuple[str, int, int], tuple[str, list[str]]] = {}
    for r in selected:
        for citation in r.record.attributes.get("citations") or []:
            if not isinstance(citation, dict):
                continue
            key = (citation.get("source_id"), citation.get("start_byte"), citation.get("end_byte"))
            if key in evidence:
                evidence[key][1].append(r.record.id)
                continue
            quote = citation.get("quote")
            if not isinstance(quote, str):
                try:
                    quote = db.read_source_text(key[0], start_byte=key[1], end_byte=key[2])
                except (KeyError, ValueError):
                    continue  # Unresolvable legacy citation; the record still shows its attribute.
            evidence[key] = (quote, [r.record.id])

    # Issues: disagreement and revision links, and unresolved linked Questions.
    issue_links: dict[str, Link] = {}
    questions: dict[str, Record] = {}
    for r in selected:
        connected = db.connections(r.record.id, limit=1000)
        linked = [r.record] + [connected.records[link.from_id if link.to_id == r.record.id else link.to_id]
                               for link in connected.links if link.relation in QUESTION_RELATIONS]
        for link in connected.links:
            if link.relation in ISSUE_RELATIONS:
                issue_links[link.id] = link
        for record in linked:  # Not via `about`, which would pull in every Question on a company.
            if record.kind == "Question" and record.attributes.get("status", "open") in OPEN_STATUSES:
                questions.setdefault(record.id, record)

    frontier = list(dict.fromkeys([r.record.id for r in rest] + [i for i in capped if i not in chosen]))
    return Recall(
        records=tuple(selected),
        evidence=tuple(Evidence(s, a, b, quote, tuple(ids)) for (s, a, b), (quote, ids) in evidence.items()),
        issue_links=tuple(issue_links.values()),
        open_questions=tuple(questions.values()),
        frontier=tuple(frontier[:FRONTIER_LIMIT]),
        coverage={"seeds": len(seed_ids), "search_hits": len(hits), "entry_points": len(entries), "reached": len(best), "selected": len(selected),
                  "frontier_omitted": max(0, len(frontier) - FRONTIER_LIMIT), "truncated": bool(rest or capped)},
    )
