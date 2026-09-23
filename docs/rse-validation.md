# Initial RSE validation

The selector was checked against dsRAG at commit `5215e9791133fc1ad383af043fb2fde0e7d4e4d4`, using its original `get_chunk_value` and `get_best_segments` functions. Across 500 randomized single-query cases (seed 2314), the selected intervals matched exactly and values agreed within 1e-12. Fixtures used one-character passages so character budgets equaled dsRAG's passage-count budgets, no headers, one to four sources, missing scores, and varied penalties, decay rates, minimum values and budgets. This checks the shared algorithm, not equivalence of character and passage budgets for real documents. Unit tests separately cover length adjustment, header costs, gaps, context changes, Unicode and unchanged canonical citations after rechunking.

An offline replay used the first 20 public FinQA test questions and the saved scores from the [Jev experiment](https://github.com/Implied-Capital/PremiseDB/pull/11) and [plain-query Cohere/Voyage comparison](https://github.com/Implied-Capital/PremiseDB/pull/12). No new model calls were made. Dataset: [pinned FinQA test.json](https://github.com/czyssrs/FinQA/blob/0f16e2867befa6840783e58be38c9efb9229d742/dataset/test.json), SHA-256 `831dbfb2e785dbc227f895ce3f24046433467aec67b09db2bd6ac7692a8a30dc`.

Each text sentence and the complete table formed separate passages, preserving the previous experiment's boundaries. Sources were reconstructed from the dataset; cached candidate text and byte ranges were checked against it. Unretrieved passages were supplied with `None` scores, allowing penalized bridges. Titles were the FinQA document IDs with empty summaries. Extraction used the implementation defaults, including character budgets and length adjustment. Every selected range matched the canonical text and respected the budget.

| Cached scorer | Questions retaining all annotated support | Annotated support passages retained | Mean output characters, including headers |
| --- | ---: | ---: | ---: |
| Jev 1.13 | 19/20 | 20/21 | 821 |
| Cohere Rerank 4 Pro | 20/20 | 21/21 | 2,867 |
| Voyage rerank-2.5 | 19/20 | 20/21 | 1,919 |

These are small, source-scoped integration checks, not a new retrieval benchmark or a fair quality comparison at equal output length. There are only 21 support passages across 20 questions because tables are single passages. The replay does not test whole-corpus retrieval, the production splitter, full document summaries or held-out score calibration.

Two failures matter for the next increment:

- Jev still omits Adobe's required denominator (`ADBE/2008/page_89.pdf-3`), which received a raw score of 0.10. The selector cannot recover every weakly scored fact merely by merging passages.
- Voyage returns no excerpt for `RCL/2012/page_75.pdf-1`: its highest score is 0.6640625, giving utility 0.4640625 after the penalty, below the default 0.5 minimum. Shared numerical ranges do not make shared selection thresholds appropriate. Cohere's greater retained coverage also comes with substantially more text here.

Provider-specific selection settings or score calibration need evaluation before RSE becomes a default integrated retrieval path. The present increment supplies the inspectable selection primitive and leaves those controls explicit.
