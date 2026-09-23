"""Collect a small source-scoped FinQA evaluation; API calls require --collect."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from premisedb import OpenRouterReranker, RecordStore

DATA_SHA = "831dbfb2e785dbc227f895ce3f24046433467aec67b09db2bd6ac7692a8a30dc"


def split_rows(rows):
    train = rows[:20]
    used = {row["id"].split(".pdf")[0] for row in train}
    validation = [row for row in rows[20:] if row["id"].split(".pdf")[0] not in used][:20]
    used.update(row["id"].split(".pdf")[0] for row in validation)
    test = [row for row in rows[20:] if row["id"].split(".pdf")[0] not in used][:20]
    return {"train": train, "validation": validation, "test": test}


def prepare(selected):
    prepared = []
    with RecordStore(':memory:') as db:
        for row in selected:
            pre, post = row['pre_text'], row['post_text']
            units = [(f'text_{i}', text) for i, text in enumerate(pre)]
            units += [('table', '\n'.join(' | '.join(cells) for cells in row['table']))]
            units += [(f'text_{i + len(pre)}', text) for i, text in enumerate(post)]
            parts, ranges, labels, offset = [], [], [], 0
            for label, text in units:
                text += '\n\n'
                end = offset + len(text.encode())
                parts.append(text)
                ranges.append((offset, end))
                labels.append(label)
                offset = end
            source = db.ingest_source(description=row['id'], document_text=''.join(parts), author_id='calibration-check')
            db.set_passages(source.id, ranges)
            db.set_document_context(source.id, title=row['id'], summary='', author_id='calibration-check')
            query = row['qa']['question']
            candidates = db.search_documents(query, source_ids=[source.id], limit=20)
            intervening = db._rse_intervening_passages(candidates, 32)
            gold = {'table' if label.startswith('table_') else label for label in row['qa']['gold_inds']}
            by_range = dict(zip(ranges, labels))
            prepared.append({'id': row['id'], 'query': query, 'segments': candidates,
                             'labels': [by_range[(s.start_byte, s.end_byte)] in gold for s in candidates],
                             'unscored_segments': intervening,
                             'unscored_labels': [by_range[(s.start_byte, s.end_byte)] in gold for s in intervening],
                             'required_support': len(gold)})
    return prepared


def collect(prepared, output, key):
    report = {"dataset_sha256": DATA_SHA, "models": {}}
    for model in OpenRouterReranker.MODELS:
        def run(case):
            provider = OpenRouterReranker(model, api_key=key)
            post, responses = provider._post, []
            def capture(endpoint, payload):
                response = post(endpoint, payload)
                responses.append(response)
                return response
            started = perf_counter()
            with patch.object(provider, '_post', side_effect=capture):
                scores = provider.score(case['query'], case['segments'])
            return {**case, 'segments': [asdict(s) for s in case['segments']], 'scores': scores,
                    'unscored_segments': [asdict(s) for s in case['unscored_segments']],
                    'seconds': perf_counter() - started,
                    'cost_usd': sum(r['usage']['cost'] for r in responses),
                    'responses': responses}
        results = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            for result in executor.map(run, prepared):
                results.append(result)
                report['models'][model] = results
                output.write_text(json.dumps(report, indent=2))
                print(model, len(results), '/', len(prepared), flush=True)
        print(model, 'pairs', sum(len(r['scores']) for r in results),
              'cost', sum(r['cost_usd'] for r in results), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--split", choices=("train", "validation", "test"), required=True)
    parser.add_argument("--collect", action="store_true", help="Authorize paid OpenRouter calls")
    args = parser.parse_args()
    if not args.collect:
        parser.error("--collect is required to make paid API calls")
    if args.output.exists():
        parser.error("Output already exists; choose a new path")
    if sha256(args.dataset.read_bytes()).hexdigest() != DATA_SHA:
        parser.error("Expected the pinned FinQA test.json; see docs/rse-validation.md")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    collect(prepare(split_rows(json.loads(args.dataset.read_text()))[args.split]),
            args.output, os.environ["OPENROUTER_API_KEY"])
