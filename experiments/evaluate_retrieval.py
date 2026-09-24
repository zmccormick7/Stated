"""Measure whether retrieval finds the evidence that research Findings cite; never writes to the graph.

Each disclosed Finding with citations becomes a case: a question an analyst would ask, answered by
the cited source ranges. `questions` writes those questions once with a model (paid calls);
`evaluate` scores BM25 candidates, the full rerank + RSE pipeline and record search against them.
"""

import argparse
from hashlib import sha256
import json
from statistics import median
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from premisedb import OpenRouterReranker, RecordStore, retrieve

PROMPT = (
    "Below is a finding from an investment research graph. Write the one question a research analyst "
    "would type into a document search to find the evidence for it. Name the company or organization "
    "and the topic, but do not copy the finding's distinctive wording and do not state its numbers. "
    "Reply with the question only.\n\nFinding: {finding}"
)
RANKS = (1, 5, 10, 50)


def overlaps(segment, citation):
    return (segment["source_id"] == citation["source_id"]
            and segment["start_byte"] < citation["end_byte"] and citation["start_byte"] < segment["end_byte"])


def spans(segments):
    return [{"source_id": s.source_id, "start_byte": s.start_byte, "end_byte": s.end_byte} for s in segments]


def first_hit(segments, citations):
    """1-based rank of the first segment overlapping any citation, or None."""
    return next((rank for rank, segment in enumerate(segments, 1)
                 if any(overlaps(segment, citation) for citation in citations)), None)


def rate(ranks, k):
    return sum(rank is not None and rank <= k for rank in ranks) / len(ranks)


def summarize(results):
    summary = {"cases": len(results)}
    for name in ("candidates", "scoped_candidates", "reranked", "scoped_reranked", "finding"):
        ranks = [result[name] for result in results if name in result]
        if not ranks:
            continue
        summary[name] = {f"hit@{k}": round(rate(ranks, k), 3) for k in RANKS}
    for name in ("pipeline", "scoped_pipeline"):
        runs = [result[name] for result in results if result.get(name)]
        if runs:
            hits = [run["tokens_to_hit"] for run in runs if run["hit_rank"]]
            summary[name] = {"hit": round(len(hits) / len(runs), 3),
                             "first_excerpt_hit": round(rate([run["hit_rank"] for run in runs], 1), 3),
                             "empty": sum(not run["excerpts"] for run in runs),
                             "mean_tokens": round(sum(run["tokens"] for run in runs) / len(runs)),
                             "median_tokens_to_hit": median(hits) if hits else None}
    return summary


def with_retries(call, attempts=4):
    """Retry rate limits and provider errors, which the library deliberately surfaces."""
    for attempt in range(attempts):
        try:
            return call()
        except (HTTPError, RuntimeError) as error:
            code = getattr(error, "code", None) or next((int(w) for w in str(error).split() if w.isdigit()), None)
            if attempt == attempts - 1 or code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(5 * 2 ** attempt)


def cited_findings(db):
    """Disclosed Findings whose citations all point at chunked sources."""
    page, after = None, None
    while page is None or page.has_more:
        page = db.list_records(kind="Finding", after=after, limit=1000)
        after = page.records[-1].id if page.records else after
        for record in page.records:
            citations = [{key: c[key] for key in ("source_id", "start_byte", "end_byte")}
                         for c in record.attributes.get("citations") or []]
            if (record.attributes.get("basis") == "disclosed" and citations
                    and all(db.read_passages(c["source_id"], limit=1) for c in citations)):
                yield record, citations


def write_questions(db, output, model, key):
    cases = []
    for record, citations in cited_findings(db):
        request = Request("https://openrouter.ai/api/v1/chat/completions", json.dumps({
            "model": model, "max_tokens": 300,
            "messages": [{"role": "user", "content": PROMPT.format(finding=record.text)}],
        }).encode(), headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        def ask():
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        reply = with_retries(ask)
        question = reply["choices"][0]["message"].get("content")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"No question returned for Finding {record.id}")
        cases.append({"finding_id": record.id, "question": question.strip(),
                      "citations": citations, "cost_usd": reply.get("usage", {}).get("cost")})
    output.write_text(json.dumps({"model": model, "prompt": PROMPT, "cases": cases}, indent=2) + "\n")
    return len(cases)


def evaluate(db, cases, reranker=None, count_tokens=None, max_tokens=6000):
    results = []
    for case in cases:
        question, citations = case["question"], case["citations"]
        sources = list(dict.fromkeys(c["source_id"] for c in citations))
        result = {"finding_id": case["finding_id"],
                  "candidates": first_hit(spans(db.search_documents(question, limit=50)), citations),
                  "scoped_candidates": first_hit(spans(db.search_documents(
                      question, source_ids=sources, limit=50)), citations),
                  "finding": next((rank for rank, record in enumerate(
                      db.search_records(question, kind="Finding", limit=50), 1)
                      if record.id == case["finding_id"]), None)}
        if reranker is not None:
            for name, scope in (("pipeline", None), ("scoped_pipeline", sources)):
                retrieved = with_retries(lambda: retrieve(
                    db, question, reranker=reranker, count_tokens=count_tokens, source_ids=scope,
                    candidate_limit=50, max_total_tokens=max_tokens, max_segment_tokens=min(2000, max_tokens)))
                reranked = sorted(retrieved.scored_passages, key=lambda scored: -scored.score)
                result[name.replace("pipeline", "reranked")] = first_hit(
                    spans(scored.segment for scored in reranked), citations)
                excerpts = retrieved.excerpts
                rank = first_hit(spans(e.segment for e in excerpts), citations)  # Excerpts are best-first.
                result[name] = {"hit_rank": rank, "excerpts": len(excerpts),
                                "tokens": sum(e.token_count for e in excerpts),
                                "tokens_to_hit": sum(e.token_count for e in excerpts[:rank]) if rank else None}
        results.append(result)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("questions", help="Write one question per cited Finding (paid model calls).")
    make.add_argument("db", type=Path)
    make.add_argument("output", type=Path)
    make.add_argument("--model", default="anthropic/claude-sonnet-5")
    run = commands.add_parser("evaluate", help="Score retrieval against saved questions.")
    run.add_argument("db", type=Path)
    run.add_argument("questions", type=Path)
    run.add_argument("output", type=Path)
    run.add_argument("--reranker", default="voyageai/rerank-2.5", choices=OpenRouterReranker.MODELS)
    run.add_argument("--max-tokens", type=int, default=6000, help="Total excerpt budget (default 6000).")
    run.add_argument("--skip-rerank", action="store_true", help="Only BM25 and record search; no API calls.")
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"Refusing to overwrite {args.output}")
    if not args.db.is_file():
        parser.error(f"Database does not exist: {args.db}")
    with RecordStore(args.db) as db:
        if args.command == "questions":
            print(f"Wrote {write_questions(db, args.output, args.model, os.environ['OPENROUTER_API_KEY'])} questions")
        else:
            saved = json.loads(args.questions.read_text())
            reranker = count = None
            if not args.skip_rerank:
                import tiktoken
                encoding = tiktoken.get_encoding("cl100k_base")
                count = lambda text: len(encoding.encode(text, disallowed_special=()))
                reranker = OpenRouterReranker(args.reranker)
            results = evaluate(db, saved["cases"], reranker, count, args.max_tokens)
            report = {"questions_sha256": sha256(args.questions.read_bytes()).hexdigest(),
                      "question_model": saved["model"], "reranker": None if args.skip_rerank else args.reranker,
                      "max_tokens": args.max_tokens,
                      "summary": summarize(results), "results": results}
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report["summary"], indent=2))
