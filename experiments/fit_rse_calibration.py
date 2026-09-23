"""Fit sigmoid score mappings on training data; report validation and test separately."""

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean

import numpy as np
from sklearn.linear_model import LogisticRegression
import tiktoken

from premisedb import DocumentContext, SourceSegment, ScoreCalibration, extract_segments
from premisedb.calibration import score_logit
from experiments.collect_rse_scores import DATA_SHA


def segments(case):
    return tuple(SourceSegment(**{**s, 'document_context': DocumentContext(**s['document_context']) if s['document_context'] else None}) for s in case['segments'])

def evaluate(cases, count_tokens, profile=None):
    complete, covered, size, empty = 0, 0, 0, 0
    predictions, labels = [], []
    for case in cases:
        pool = segments(case)
        scores = profile.apply(case['scores']) if profile else case['scores']
        options = {} if not profile else dict(irrelevant_chunk_penalty=profile.irrelevant_chunk_penalty, minimum_value=profile.minimum_value)
        excerpts = extract_segments(pool, scores, count_tokens=count_tokens, **options)
        found = sum(label and any(item.segment.source_id == chunk.source_id and item.segment.start_byte <= chunk.start_byte and chunk.end_byte <= item.segment.end_byte for item in excerpts)
                    for chunk, label in zip(pool, case['labels']))
        complete += found == case['required_support']
        covered += found
        size += sum(item.token_count for item in excerpts)
        empty += not excerpts
        predictions.extend(scores)
        labels.extend(case['labels'])
    ece = 0
    for bucket in range(5):
        indices = [i for i, p in enumerate(predictions) if min(4, int(p * 5)) == bucket]
        if indices:
            ece += len(indices) / len(predictions) * abs(mean(predictions[i] for i in indices) - mean(labels[i] for i in indices))
    return {'all_support': complete, 'support': covered, 'mean_tokens': size / len(cases), 'empty': empty,
            'brier': mean((p - y) ** 2 for p, y in zip(predictions, labels)), 'ece5': ece,
            'pairs': len(labels), 'positive': sum(labels)}



def fit_and_evaluate(training, validation, test, count_tokens):
    profiles, report = {}, {}
    for model, cases in training.items():
        x = np.array([[score_logit(score)] for case in cases for score in case["scores"]])
        y = np.array([label for case in cases for label in case["labels"]], dtype=int)
        fitted = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000).fit(x, y)
        # Selection settings were chosen on validation cases, then frozen for the test set.
        profile = ScoreCalibration("finqa-support-v1", model, float(fitted.coef_[0, 0]),
                                   float(fitted.intercept_[0]), 0.001, 0.01)
        profiles[model] = asdict(profile)
        report[model] = {split: {"raw": evaluate(rows, count_tokens),
                                "calibrated": evaluate(rows, count_tokens, profile)}
                         for split, rows in (("train", cases), ("validation", validation[model]), ("test", test[model]))}
        print(model, json.dumps(report[model]), flush=True)
    return profiles, report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("training", type=Path)
    parser.add_argument("validation", type=Path)
    parser.add_argument("test", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    paths = (args.training, args.validation, args.test)
    runs = [json.loads(path.read_text()) for path in paths]
    if any(run["dataset_sha256"] != DATA_SHA for run in runs):
        parser.error("Expected the pinned FinQA dataset hash")
    seen = set()
    for run in runs:
        if set(run["models"]) != {"typesafe/jev-1.13", "cohere/rerank-4-pro", "voyageai/rerank-2.5"}:
            parser.error("Each run must contain all three models")
        orders = []
        for cases in run["models"].values():
            if len(cases) != 20 or any(not len(c["labels"]) == len(c["scores"]) == len(c["segments"]) for c in cases):
                parser.error("Expected 20 complete cases with aligned scores, segments and labels")
            orders.append([c["id"] for c in cases])
        if any(order != orders[0] for order in orders):
            parser.error("Models must score the same questions in each run")
        ids = {case["id"].split(".pdf")[0] for cases in run["models"].values() for case in cases}
        if seen & ids:
            parser.error("Pages overlap between training, validation and test")
        seen.update(ids)
    encoding = tiktoken.get_encoding("cl100k_base")
    count = lambda text: len(encoding.encode(text, disallowed_special=()))
    profiles, metrics = fit_and_evaluate(*(run["models"] for run in runs), count)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "calibrations.json").write_text(json.dumps(profiles, indent=2) + "\n")
    report = {"inputs": {path.name: sha256(path.read_bytes()).hexdigest() for path in paths}, "metrics": metrics}
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
