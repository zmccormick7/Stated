"""Compare raw scores and dsRAG transforms on saved candidate-only scores; no API calls."""

import argparse
from hashlib import sha256
import json
from pathlib import Path

import tiktoken

from premisedb import DocumentContext, OpenRouterReranker, SourceSegment, extract_segments
from experiments.collect_rse_scores import DATA_SHA


def segments(case, field):
    return tuple(SourceSegment(**{**s, "document_context": DocumentContext(**s["document_context"])
                                 if s["document_context"] else None}) for s in case[field])


def evaluate(cases, count_tokens, transform=lambda score: score):
    complete, covered, size, empty = 0, 0, 0, 0
    for case in cases:
        pool = segments(case, "segments")
        intervening = segments(case, "unscored_segments")
        scores = tuple(transform(score) for score in case["scores"])
        excerpts = extract_segments(pool + intervening, scores + (None,) * len(intervening),
                                    count_tokens=count_tokens)
        found = sum(label and any(item.segment.source_id == chunk.source_id
                                  and item.segment.start_byte <= chunk.start_byte
                                  and chunk.end_byte <= item.segment.end_byte for item in excerpts)
                    for chunk, label in zip(pool + intervening, case["labels"] + case["unscored_labels"]))
        complete += found == case["required_support"]
        covered += found
        size += sum(item.token_count for item in excerpts)
        empty += not excerpts
    return {"questions": len(cases), "all_support": complete, "support": covered,
            "required_support": sum(c["required_support"] for c in cases),
            "mean_tokens": size / len(cases), "empty": empty}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scores", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run = json.loads(args.scores.read_text())
    if run["dataset_sha256"] != DATA_SHA or set(run["models"]) != set(OpenRouterReranker.MODELS):
        parser.error("Expected the pinned dataset and all three models")
    orders = []
    for cases in run["models"].values():
        if not cases or any(len(c["labels"]) != len(c["scores"]) or len(c["scores"]) != len(c["segments"])
                            or len(c["unscored_segments"]) != len(c["unscored_labels"]) for c in cases):
            parser.error("Expected complete cases with aligned scores, segments and labels")
        orders.append([c["id"] for c in cases])
    if any(order != orders[0] for order in orders):
        parser.error("Models must score the same questions")
    encoding = tiktoken.get_encoding("cl100k_base")
    count = lambda text: len(encoding.encode(text, disallowed_special=()))
    metrics = {}
    for model, cases in run["models"].items():
        reranker = OpenRouterReranker(model, api_key="unused-offline-evaluation")
        metrics[model] = {"raw": evaluate(cases, count),
                          "dsrag_transform": evaluate(cases, count, reranker.transform)}
    report = {"scores_sha256": sha256(args.scores.read_bytes()).hexdigest(), "metrics": metrics}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(metrics, indent=2))
