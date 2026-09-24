# Retrieval evaluation from cited Findings

[Back to PremiseDB](../README.md)

A research graph can supply its own retrieval benchmark. Each disclosed Finding cites exact source ranges; a question whose answer is that Finding should retrieve one of those ranges. [`experiments/evaluate_retrieval.py`](../experiments/evaluate_retrieval.py) turns every disclosed, cited Finding in a workspace into such a case and measures each retrieval stage against it. It reads the graph and never writes to it.

- **Questions.** `questions` asks a model to write the question an analyst would search for, naming the company and topic without copying the Finding's distinctive wording or numbers. The saved file keeps each question and its cited ranges; every later run reuses it.
- **Hits.** A retrieved passage or excerpt is a hit when it overlaps any cited range of that Finding. The cited ranges were chosen by the agent that wrote the Finding; other passages can hold equally good evidence, so every hit rate is a lower bound.
- **Stages.** BM25 `search_documents` across all sources and scoped to the cited sources; the reranker's order of those 50 candidates; the excerpts `retrieve` returns after RSE, in their best-first order, with their token cost; and `search_records` for the Finding itself.

## First run: the AI infrastructure workspace

The workspace behind this run held 28 public filings, releases and regulator documents ingested by three research agents, split into 2,146 passages of at most 2,000 characters (median 342 `cl100k_base` tokens) with agent-written document headers (median 96 tokens). Its 78 disclosed Findings cite 256 ranges with a median of 25 tokens. `anthropic/claude-sonnet-5` wrote the questions for $0.086. The pipeline used `voyageai/rerank-2.5`, 50 BM25 candidates and the balanced RSE defaults; reranking cost was not recorded.

| Stage (78 questions) | Hit @1 | Hit @5 | Hit @50 |
| --- | ---: | ---: | ---: |
| BM25 passages, all sources | 52.6% | 80.8% | 98.7% |
| BM25 passages, cited sources only | 64.1% | 92.3% | 98.7% |
| Reranked passages, all sources | 85.9% | 98.7% | 98.7% |
| Record search for the Finding | 88.5% | 100% | 100% |

| `retrieve` output, all sources | Any excerpt hits | First excerpt hits | Mean tokens returned | Median tokens read to first hit |
| --- | ---: | ---: | ---: | ---: |
| 6,000-token budget (default) | 98.7% | 75.6% | 5,721 | 1,927 |
| 2,000-token budget | 75.6% | 75.6% | 1,887 | 1,805 |

Scoping `retrieve` to the cited sources gave 98.7% and 74.4% at the default budget. The one question that no stage answered was a compound question about CoreWeave's backlog, customers and financing, whose cited ranges never reached BM25's top 50.

### What it shows

- **Candidate recall is not the bottleneck here.** BM25's 50 candidates contained cited evidence for 77 of 78 questions, and the reranker put it first for 67.
- **RSE currently costs precision.** The reranker's top passage (about 440 tokens with its header) was a hit 85.9% of the time; RSE's first excerpt was a hit 75.6% of the time at about 1,900 tokens. When it hit, the first excerpt used a median 1,805 tokens, close to the 2,000-token excerpt cap, so the default budget succeeds by returning nearly 6,000 tokens per question. Cited evidence is a median 25 tokens. This matches agents' reports that `search-documents` results were large and did not always lead with the relevant passage.
- **Record search looks strong but is flattered.** Each question was written from its Finding and names the same company and topic, so shared wording makes the Finding easy to find. A synthesis agent searching in its own words never surfaced the Dominion Findings it later reached through links. This set does not measure that case.

These are measurements on one workspace, one question model, one reranker and one run, not an independent benchmark. Its questions often name the filing. The sources are public, but the workspace and its question file are not published, so others can reproduce the method rather than these exact numbers. The first report has SHA-256 `c9c5bd3d901d0d305cf513f04b88b050c3a8d12e304a1725833b6e29e520d10c` (6,000 tokens) and was computed from question file `c50f4418d77adb41fddddd94f443b5444fa9e5b706ddecd13cf7acf1ec5b3d20`.

Changes worth testing against this baseline include smaller passages, RSE without the chunk-length adjustment, and a higher irrelevant-passage penalty. Any change should keep candidate and reranked hit rates while raising first-excerpt hits and lowering tokens read.

## Reproduce

Use a workspace whose Findings cite ingested, chunked sources with `basis: disclosed`. Install the CLI or evaluation extra for the tokenizer and export `OPENROUTER_API_KEY`. `questions` and `evaluate` without `--skip-rerank` make paid calls; both refuse to overwrite an output file.

```sh
python -m experiments.evaluate_retrieval questions research.sqlite .cache/retrieval/questions.json
python -m experiments.evaluate_retrieval evaluate research.sqlite .cache/retrieval/questions.json .cache/retrieval/report.json
python -m experiments.evaluate_retrieval evaluate research.sqlite .cache/retrieval/questions.json .cache/retrieval/report-2000.json --max-tokens 2000
```

`--skip-rerank` reports BM25 and record search only, offline. `--reranker` selects another supported model. Rate limits and provider errors are retried with backoff. Keep question files and reports out of version control when they quote non-public material.
