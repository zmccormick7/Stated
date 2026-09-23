"""Compare hosted rerankers with the saved Jev experiment; live calls are opt-in."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from statistics import mean, median
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MODELS = ("cohere/rerank-4-pro", "voyageai/rerank-2.5")
ENDPOINT = "https://openrouter.ai/api/v1/rerank"
DATA_SHA = "831dbfb2e785dbc227f895ce3f24046433467aec67b09db2bd6ac7692a8a30dc"


def request_payload(model, cases, rubric, plain_query=False):
    query = cases[0]["query"]
    if any(case["query"] != query for case in cases):
        raise ValueError("A rerank batch must contain one research query")
    if not plain_query:
        query += ("\n\nRelevance instructions: " + rubric["instructions"]
                  + "\nRelevant: " + rubric["criteria"]["true"]
                  + "\nIrrelevant: " + rubric["criteria"]["false"])
    documents = []
    for case in cases:
        segment = case["segment"]
        context = segment["document_context"]
        documents.append(json.dumps({
            "document_context": context["title"] + "\n" + context["summary"] if context else "",
            "passage": segment["text"],
        }, ensure_ascii=False))
    return {"model": model, "query": query, "documents": documents, "top_n": len(documents)}


def ordered_scores(result, count):
    """Require one finite 0–1 score for every original candidate, regardless of returned order."""
    scores = {}
    for row in result["results"]:
        index, score = row["index"], row["relevance_score"]
        if type(index) is not int or index in scores or not 0 <= index < count:
            raise ValueError("Invalid or duplicate candidate index")
        if type(score) not in (int, float) or not 0 <= score <= 1:
            raise ValueError("Invalid relevance score")
        scores[index] = score
    if len(scores) != count:
        raise ValueError("Provider omitted candidate scores")
    return [scores[i] for i in range(count)]


def rerank(payload, api_key):
    request = Request(ENDPOINT, json.dumps(payload).encode(), headers={
        "Authorization": "Bearer " + api_key, "Content-Type": "application/json",
    })
    started = perf_counter()
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"OpenRouter HTTP {error.code}") from None
    except (URLError, TimeoutError):
        raise RuntimeError("OpenRouter connection failed or timed out") from None
    return {"scores": ordered_scores(result, len(payload["documents"])),
            "model": result["model"], "provider": result.get("provider"),
            "request_id": result["id"], "usage": result["usage"],
            "seconds": perf_counter() - started}


def metrics(cases, scores, required, *, thresholds=(0.1, 0.3, 0.5, 0.7)):
    """Counts use all annotated supports, including any missing from the BM25 pool."""
    if len(cases) != len(scores):
        raise ValueError("One score per candidate is required")
    groups = defaultdict(list)
    for case, score in zip(cases, scores):
        groups[case["group"]].append((case, score))
    totals = defaultdict(list)
    for query_id, support_count in required.items():
        ranked = sorted(groups[query_id], key=lambda pair: (-pair[1], pair[0]["bm25_rank"]))
        ranks = [rank for rank, (case, _) in enumerate(ranked, 1) if case["annotated_support"]]
        totals["top1"].append(bool(ranks and ranks[0] == 1))
        totals["mrr"].append(1 / ranks[0] if ranks else 0)
        for k in (3, 5):
            found = sum(rank <= k for rank in ranks)
            totals[f"recall@{k}"].append(found / support_count)
            totals[f"all_support@{k}"].append(found == support_count)
        for cutoff in thresholds:
            kept = [case for case, score in ranked if score >= cutoff]
            totals[f"recall>={cutoff}"].append(sum(c["annotated_support"] for c in kept) / support_count)
            totals[f"kept>={cutoff}"].append(len(kept))
    return {name: mean(values) for name, values in totals.items()}


def load_baseline(path, dataset_path):
    baseline_bytes, dataset_bytes = path.read_bytes(), dataset_path.read_bytes()
    baseline = json.loads(baseline_bytes)
    if sha256(dataset_bytes).hexdigest() != DATA_SHA or baseline["dataset_sha256"] != DATA_SHA:
        raise ValueError("Dataset does not match the pinned FinQA test set")
    required = {}
    for row in json.loads(dataset_bytes)[:20]:
        labels = {"table" if key.startswith("table_") else key for key in row["qa"]["gold_inds"]}
        required[row["id"]] = len(labels)
    cases = baseline["results"]
    if any("error" in case or "score" not in case for case in cases):
        raise ValueError("The Jev baseline must be complete")
    groups = defaultdict(list)
    for case in cases:
        group = case["group"] if case["group"] != "synthetic" else f"synthetic-{case['repeat']}"
        groups[group].append(case)
    if set(groups) != set(required) | {"synthetic-0", "synthetic-1", "synthetic-2"}:
        raise ValueError("Expected the original 20 questions and three synthetic repetitions")
    groups["negative-only"] = [c for c in groups["synthetic-0"]
                               if c["case"] in {"wrong_period", "wrong_company", "header_only", "related", "injection"}]
    return baseline, groups, required, sha256(baseline_bytes).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=Path(".cache/jev/results.json"))
    parser.add_argument("--dataset", type=Path, default=Path(".cache/jev/finqa-test.json"))
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--plain-query", action="store_true", help="Control run without appended relevance instructions")
    args = parser.parse_args()
    baseline, groups, required, digest = load_baseline(args.baseline, args.dataset)
    print(f"Prepared {len(groups)} requests per provider from the frozen Jev candidate pools.")
    if args.prepare_only:
        return
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key and Path(".env").exists():
        api_key = next((line.removeprefix("OPENROUTER_API_KEY=").strip() for line in
                        Path(".env").read_text().splitlines() if line.startswith("OPENROUTER_API_KEY=")), None)
    if not api_key:
        raise ValueError("Set OPENROUTER_API_KEY in the environment or checkout .env")
    financial = [case for case in baseline["results"] if case["group"] != "synthetic"]
    output = {"created_at": datetime.now(UTC).isoformat(), "baseline_sha256": digest,
              "query_mode": "plain" if args.plain_query else "shared-rubric",
              "dataset_sha256": DATA_SHA, "rubric": baseline["question"], "required": required,
              "candidates": dict(groups), "models": {},
              "bm25": metrics(financial, [-c["bm25_rank"] for c in financial], required, thresholds=()),
              "jev": metrics(financial, [c["score"] for c in financial], required)}
    path = Path(".cache/reranker-comparison")
    path.mkdir(parents=True, exist_ok=True)
    destination = path / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    for model in MODELS:
        def evaluate(item):
            group, cases = item
            try:
                return group, rerank(request_payload(model, cases, baseline["question"], args.plain_query), api_key)
            except Exception as error:
                return group, {"error": type(error).__name__}  # No provider bodies or credentials.

        started = perf_counter()
        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = dict(executor.map(evaluate, groups.items()))
        run = {"responses": responses, "wall_seconds": perf_counter() - started}
        output["models"][model] = run
        if any("error" in response for response in responses.values()):
            destination.write_text(json.dumps(output, indent=2) + "\n")
            raise SystemExit(f"Incomplete run saved to {destination}; no aggregate comparison reported")
        model_cases, model_scores = [], []
        for group, cases in groups.items():
            if group in required:
                model_cases.extend(cases)
                model_scores.extend(responses[group]["scores"])
        run["metrics"] = metrics(model_cases, model_scores, required)
        costs = [response["usage"].get("cost") for response in responses.values()]
        run["cost_usd"] = sum(costs) if all(cost is not None for cost in costs) else None
        run["median_batch_seconds"] = median(r["seconds"] for r in responses.values())
        destination.write_text(json.dumps(output, indent=2) + "\n")
        print(json.dumps({model: {k: v for k, v in run.items() if k != "responses"}}, indent=2))
    print(f"Full inputs, scores and request metadata saved to {destination}")


if __name__ == "__main__":
    main()
