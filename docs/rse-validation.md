# RSE and calibration validation

The selector follows dsRAG at commit `5215e9791133fc1ad383af043fb2fde0e7d4e4d4`. Across 500 randomized single-query cases (seed 2314), intervals matched its original `get_best_segments` function exactly and utilities agreed within 1e-12. After removing neighbor expansion, a further 500 randomized integrated retrieval cases matched the reference: only supplied search hits were scored, and intervening passages received the negative penalty. Fixtures use one-character passages and a one-character-per-token counter so budgets equal dsRAG's passage counts. Separate tests cover real BPE tokenization, non-additive and decreasing token counts, repeated headers, gaps beyond the former neighbor radius, passage/token limits, source scope, stale anchors and preserved canonical citations through context changes/rechunking.

## Current pipeline: candidate-only replay

The runtime now reranks only BM25 hits. To check the change without more API calls, we regenerated the original 20 final-test searches (up to 20 hits per question), kept their previously collected raw scores, discarded all neighbor scores, and supplied intervening passages as unscored. The bundled calibration coefficients and selection settings were unchanged. This replay has 353 scored passages and 24 annotated supports; it is not a fresh provider run or an independent test set.

| Provider | Questions retaining all support, raw → calibrated | Support passages retained, out of 24 | Mean output tokens |
| --- | ---: | ---: | ---: |
| Jev 1.13 | 18 → 19 | 21 → 22 | 249 → 368 |
| Cohere Rerank 4 Pro | 20 → 19 | 24 → 22 | 677 → 594 |
| Voyage rerank-2.5 | 20 → 20 | 24 → 24 | 512 → 696 |

This verifies selection using cached scores; it does not prove that a provider would return identical scores for a smaller batch. The original calibration was fitted on a different candidate distribution. No coefficients or thresholds were retuned for this correction.

## Original calibration experiment (expanded candidates)

We collected raw scores using the production OpenRouter adapters on three page-disjoint groups of 20 FinQA questions each: training, validation and final test. Each text sentence and the whole table are individual passages; headers contain the FinQA ID with an empty summary. For each question, the former pipeline selected up to 20 BM25 hits within its provided page, expanded two passages in each direction (capped at 100), and scored every fetched passage. That expansion has been removed. Preparation ingested and queried pages sequentially in a scratch store; all providers received the same saved pools. The results below describe this historical experiment, not the current pipeline.

The training pool contains 411 passages, including 21 annotated supports. Validation has 482 passages/30 supports; final test has 436 passages/24 supports. FinQA's `gold_inds` define binary labels; table-row labels collapse to one table passage. Unannotated passages are treated as negatives for fitting, although they may contain useful context. These are **support-annotation calibrations**, not universal relevance or factual-confidence probabilities. Other pages from the same company/filing can appear across groups.

For each provider independently, L2-regularized logistic regression (`C=1`, `lbfgs`, maximum 1,000 iterations) fits `sigmoid(slope * logit(score) + intercept)`. Raw scores are clipped to `[1e-6, 1-1e-6]` before logits. Fitting uses only training labels; production inference uses the saved two coefficients and the standard library. Observed tools: scikit-learn 1.9.1 and tiktoken 0.14.0.

Selection uses penalty 0.001 and minimum utility 0.01 on calibrated scores. These recall-oriented settings were chosen using validation results and frozen before collecting the final test set. Other settings are the same in both comparisons: rank decay 30, length adjustment enabled, at most five excerpts, 32 passages per excerpt, 2,000 tokens per excerpt and 6,000 total tokens using `cl100k_base`. Headers are included. The raw baseline uses the original 0.2 penalty and 0.5 minimum. This compares complete scoring/selection configurations, not an isolated effect of the sigmoid.

### Original final test: 20 additional questions

Brier score is mean squared error against the support label; lower is better. The table compares raw → calibrated configurations on identical expanded passage pools.

| Provider | Brier score | Questions retaining all support | Support passages retained, out of 24 | Mean output tokens |
| --- | ---: | ---: | ---: | ---: |
| Jev 1.13 | 0.0431 → 0.0197 | 18 → 19 | 21 → 22 | 251 → 386 |
| Cohere Rerank 4 Pro | 0.3173 → 0.0260 | 20 → 19 | 24 → 22 | 767 → 644 |
| Voyage rerank-2.5 | 0.1320 → 0.0226 | 20 → 20 | 24 → 24 | 595 → 738 |

Calibration improves score error for all three models here. Retrieval still has a coverage/length tradeoff: Jev retains more support with more text; Cohere returns less text but drops support on one question; Voyage preserves support while returning more text. These small, source-scoped checks do not establish equal-budget superiority or validate full-corpus retrieval, the production splitter, richer document summaries or other evidence-labeling policies. The bundled profiles are starting points to replace or refit for another workload.

All 60 questions across three providers cost $0.183054262: 1,329 Jev requests and 60 batch requests each for Cohere and Voyage. Responses identified `typesafe/jev-1.13-20260917`, `rerank-v4.0-pro` and `rerank-2.5`. No credentials or downloaded source material are committed.

## Collect and evaluate the current pipeline

Use the [pinned FinQA test.json](https://raw.githubusercontent.com/czyssrs/FinQA/0f16e2867befa6840783e58be38c9efb9229d742/dataset/test.json), SHA-256 `831dbfb2e785dbc227f895ce3f24046433467aec67b09db2bd6ac7692a8a30dc`. Install the optional evaluation dependencies and export `OPENROUTER_API_KEY` in your shell. Collecting scores makes paid calls; fitting and evaluating saved files is offline after the tokenizer vocabulary is cached.

```sh
python -m pip install -e '.[evaluation]'
python -m experiments.collect_rse_scores .cache/finqa-test.json .cache/train.json --split train --collect
python -m experiments.collect_rse_scores .cache/finqa-test.json .cache/validation.json --split validation --collect
python -m experiments.collect_rse_scores .cache/finqa-test.json .cache/test.json --split test --collect
python -m experiments.fit_rse_calibration .cache/train.json .cache/validation.json .cache/test.json .cache/fitted
```

The collector now scores only search hits and records intervening passages separately with no scores. The evaluator penalizes those passages during selection and includes them when measuring returned support. The commands above produce a new candidate-only experiment, not the original calibration data. The collector refuses to overwrite a saved run. Fit output includes `calibrations.json` and `report.json`; inspect them before replacing bundled profiles. Partial collections are rejected by the fitter. API responses may vary between runs.

For the historical collection procedure, use [the experiment at commit e3c08b5](https://github.com/Implied-Capital/PremiseDB/blob/e3c08b5e3af869ccb8b70f7b44a5d53fadcd7f92/experiments/collect_rse_scores.py). The local runs used for the unchanged committed coefficients have these SHA-256 digests:

- Training: `cfd8b126554213e19194cc2ec4ffd7cb4e73110dcc6259b9fb31796b8962dad0`
- Validation: `d0c9851afc339a3f8509105cce4a91918be2468a0e24ce9b65e8889d68c7f568`
- Final test: `000d9e5f8d8def93fe5da02722383655e68f337772f8b046cc8c4b0d1acb439f`
