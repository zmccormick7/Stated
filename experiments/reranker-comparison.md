# Cohere, Voyage and Jev comparison — 2026-09-23

Voyage was the strongest candidate on this small financial retrieval test: it
matched Jev's first-support ranking while retaining the denominator Jev missed.
Jev was substantially better at rejecting irrelevant passages with relevant
document context. Neither observation establishes broad superiority or calibration.

## Results

All models used the same 300 candidate passages for the same 20 FinQA questions.
These are source-scoped page excerpts, with whole tables as single passages.
There are 21 annotated supporting passages; only the Adobe question requires two.
Jev scores are reused from the completed experiment, not newly generated.

| Model/configuration | Support ranked first | All support in top 3 | MRR | Financial calls' reported cost |
| --- | ---: | ---: | ---: | ---: |
| BM25 | 8/20 | 16/20 | 0.648 | — |
| Jev, original rubric | 20/20 | 19/20 | 1.000 | $0.006131 |
| Cohere 4 Pro, shared rubric | 17/20 | 20/20 | 0.925 | $0.050000 |
| Voyage 2.5, shared rubric | 20/20 | 20/20 | 1.000 | $0.002997 |
| Cohere 4 Pro, plain query | 17/20 | 20/20 | 0.925 | $0.050000 |
| Voyage 2.5, plain query | 20/20 | 20/20 | 1.000 | $0.001497 |

Cohere's three first-result misses were introductory text above the supporting
tables (MAS, GS and MRO). That text may be useful context, but lacks the annotated
numeric support. FinQA annotations are not exhaustive relevance judgments.

The Adobe percentage question needs a numerator passage and a denominator table.
Jev scored the table 0.10. Cohere scored it 0.906 with the rubric and 0.899 without;
Voyage scored it 0.582 and 0.555 respectively. Both put it second.

## Score behavior matters for RSE

Scores below are medians of three calls with the shared rubric; no score
normalization or calibration transform was applied.

| Synthetic case | Jev | Cohere 4 Pro | Voyage 2.5 |
| --- | ---: | ---: | ---: |
| Direct evidence | 0.970 | 0.981 | 0.965 |
| Contrary evidence | 0.980 | 0.948 | 0.906 |
| Wrong reporting period | 0.160 | 0.885 | 0.688 |
| Wrong issuer named only in header | 0.470 | 0.823 | 0.645 |
| Relevant header, irrelevant body | 0.040 | 0.959 | 0.672 |
| Accounting policy without growth evidence | 0.060 | 0.870 | 0.570 |
| Passage instructing the scorer to return 1 | 0.130 | 0.903 | 0.574 |

The plain-query control did not fix header reliance: the header-only passage
scored 0.949 with Cohere and 0.703 with Voyage. For both models it scored above
the required Adobe denominator. A single cutoff, even after a monotonic score
transform, cannot retain that denominator and reject this header-only example.
This is a discrimination issue as well as a calibration issue.

On the financial passages, a raw 0.5 cutoff retained all annotated support with
Cohere and Voyage, averaging 9.7 and 2.5 passages per query with the rubric.
Jev retained 97.5% of support on a macro-average basis and averaged 1.85 passages.
These cutoffs are illustrative, not recommendations: raw scales differ and the
synthetic counterexamples show why these financial recall numbers are insufficient.

Removing all clearly relevant synthetic candidates changed remaining scores by
less than 0.004 across both providers and both configurations in this run.
Their high irrelevant scores therefore persisted in a negative-only pool.
The unavailable-information fixture remains semantically ambiguous and was excluded
from that pool. No inference is made about universal batch invariance.

## Reproduce

First generate the saved baseline from [PR #11](https://github.com/Implied-Capital/PremiseDB/pull/11).
This comparison reads `.cache/jev/results.json` and the pinned
`.cache/jev/finqa-test.json`; it does not regenerate candidates or rerun Jev.
Use the Python/SQLite runtime requirements in the repository README.

```sh
python -m experiments.compare_rerankers --prepare-only
python -m experiments.compare_rerankers
python -m experiments.compare_rerankers --plain-query
```

The live commands read `OPENROUTER_API_KEY` from the environment or a plain
assignment in the ignored `.env`. Each makes 24 requests per provider: 20 financial
queries, three nine-passage synthetic repetitions, and one five-passage negative
pool. Four requests run concurrently; timeout is 30 seconds, with no retries.
No labels or expected scores are sent. Each candidate is a JSON string containing
separate document context and passage fields. By default the original Jev rubric
is appended to the research query; `--plain-query` omits it. Jev's typed question
interface differs, so equal criteria do not mean identical input formatting.

Every response must contain exactly one valid score for every input index.
Incomplete runs are saved but do not produce aggregate comparisons. Recall uses
the dataset's full support counts, including supports missing from the candidate
pool; table-row labels collapse to the whole-table passage. Ties keep BM25 order.
Downloads, credentials and full outputs remain untracked. Results use timestamped
files in `.cache/reranker-comparison/`, including candidate inputs, baseline hash,
rubric, model/provider IDs, request IDs, costs, timing and all scores.

The measured runs are `20260923T203129012556Z.json` (rubric) and
`20260923T203310073853Z.json` (plain). Their baseline SHA-256 is
`afc68bfc69f7c3a8e11416e9546460455636a3eb255801da41b2539c6afa850a`.
Returned model names were `rerank-v4.0-pro` and `rerank-2.5`; the cached Jev
snapshot was `typesafe/jev-1.13-20260917`. The plain-query run was a post-hoc
formatting control; both configurations are reported without tuning individual questions.

All 96 comparison requests succeeded, costing $0.12480810 combined. Two initial
endpoint checks added $0.00264560. Median full-batch request times were 351/292 ms
(Cohere/Voyage with rubric) and 290/304 ms (plain). These include network overhead
and cannot be directly compared with Jev's previously measured per-passage latency.
This sample does not test full-corpus retrieval, production chunking, comprehensive
CCH, held-out calibration, or model training-data contamination.
