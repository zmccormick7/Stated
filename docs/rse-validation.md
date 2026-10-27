# RSE and score-transform validation

The document path scores only BM25 hits, applies dsRAG's provider-specific transforms, then selects evidence with RSE. Cohere uses beta CDF parameters `(0.4, 0.4)`; Voyage uses `(0.5, 1.8)`; Jev scores stay unchanged. The fitted sigmoid profiles and fitting procedure have been removed.

RSE scoring defaults follow dsRAG's `balanced` preset: penalty 0.18, minimum segment value 0.5, rank decay 30 and length adjustment enabled. Stated's token budgets and passage/excerpt caps remain adaptations. The reference is [dsRAG at commit 5215e97](https://github.com/D-Star-AI/dsRAG/tree/5215e9791133fc1ad383af043fb2fde0e7d4e4d4).

## Numerical and behavioral checks

The 39-line `stated/_beta.py` evaluates the regularized incomplete beta function with the [DLMF continued fraction and symmetry relation](https://dlmf.nist.gov/8.17.v). Runtime uses only the standard library. Against `scipy.stats.beta.cdf`, a dense grid, logarithmically spaced tails, adjacent floating-point values around the symmetry threshold, and exact endpoints give maximum absolute errors of 1.67e-15 for Cohere (10,335 inputs) and 1.45e-15 for Voyage (10,336 inputs). Separate power-law checks cover extremely small subnormal inputs. SciPy is an optional test reference, never a runtime import.

All 74 tests pass with optional evaluation dependencies installed. Without them, 72 pass and the SciPy/BPE reference checks skip. Integration tests verify that only search hits are scored, each score is transformed once, raw scores remain inspectable, and intervening passages retain their negative penalty. They also cover source scope, stale anchors, context/rechunk changes, exact citation ranges and token budgets.

Earlier checks matched the pinned dsRAG selector across 500 randomized pure-selection cases and 500 integrated sparse-hit cases under equivalent budgets. Those one-character fixtures isolate selection behavior from tokenization; separate tests cover real BPE, repeated headers and token merges across passage boundaries.

## Fresh provider check: 20 financial questions

We recollected scores for the same 20 FinQA questions used in earlier experiments, using the corrected candidate-only pipeline. Each provided page is ingested and queried sequentially in a scratch store; BM25 selects up to 20 hits within that page. Text sentences and the whole table are individual passages. Headers contain the FinQA ID with an empty summary. The resulting pool has 353 scored passages and 24 annotated supports per model. Intervening passages are read for selection but never scored.

Both configurations below use the same raw provider responses and balanced RSE settings, at most five excerpts and 32 passages per excerpt, 2,000 tokens per excerpt and 6,000 total tokens (`cl100k_base`, headers included). Only the score transformation changes.

| Provider | Questions retaining all support, raw → transformed | Support passages retained, out of 24 | Mean output tokens |
| --- | ---: | ---: | ---: |
| Jev 1.13 (identity) | 19 → 19 | 22 → 22 | 258 → 258 |
| Cohere Rerank 4 Pro | 20 → 20 | 24 → 24 | 717 → 735 |
| Voyage rerank-2.5 | 20 → 20 | 24 → 24 | 581 → 808 |

The beta transforms preserved annotated support coverage while returning more text, especially for Voyage. They did not improve coverage on this fixture. This is a small source-scoped compatibility check on previously examined questions, not an independent benchmark or evidence of equal-budget superiority. dsRAG's adapters default to older Cohere v3 and Voyage rerank-2 models; we have not tuned the parameters for these newer versions. Production chunking, richer headers and full-corpus retrieval remain untested by this check.

Fresh scoring cost $0.058849176: 353 Jev requests and 20 batch requests each for Cohere and Voyage. Responses identified `typesafe/jev-1.13-20260917`, `rerank-v4.0-pro` and `rerank-2.5`. The saved score file has SHA-256 `6190c9742a202815359a09c59a35a716090fdac1249d7b1ffed540446865dbb2`. Evaluating it with SciPy and the standard-library CDF produced identical coverage and token-count reports. No credentials or downloaded source material are committed.

## Reproduce

The collection and evaluation scripts for this check were removed from the repository. They remain at commit [`4930cfe`](https://github.com/zmccormick7/Stated/tree/4930cfe6aa6efaaef59af8b33efade0b9aec5dfb/experiments). They used the [pinned FinQA test.json](https://raw.githubusercontent.com/czyssrs/FinQA/0f16e2867befa6840783e58be38c9efb9229d742/dataset/test.json), SHA-256 `831dbfb2e785dbc227f895ce3f24046433467aec67b09db2bd6ac7692a8a30dc`, and made paid OpenRouter calls to collect scores. The numerical and behavioral checks above still run in the test suite:

```sh
python -m pip install -e '.[evaluation]'
python -m unittest discover -s tests -v
```
