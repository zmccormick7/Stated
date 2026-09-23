# Initial Jev results — 2026-09-23

Requested `typesafe/jev-1.13` through OpenRouter; every response named
`typesafe/jev-1.13-20260917`. The [experiment](README.md) and its fixed relevance
rubric were written before this run. All sources were ingested before retrieval
to hold global BM25 statistics fixed. No prompt tuning was applied to the main run.

| Measurement | BM25 | Jev after BM25 |
| --- | ---: | ---: |
| Questions with annotated support first | 8/20 | 20/20 |
| Mean reciprocal rank of first support | 0.648 | 1.000 |
| Questions with support in candidate pool | 20/20 | Same pool |

This is source-scoped retrieval within 20 supplied FinQA page excerpts, not search
across full filings or a company corpus. Whole tables are single candidates. The
20/20 result measures the first supporting passage, not complete evidence recall.

300 financial query-passage calls plus 27 synthetic calls completed without errors.
Reported cost was $0.00662592; median end-to-end request time was 309 ms and total
wall time was 34.8 seconds at concurrency four. These are local observations,
not throughput guarantees. Detailed inputs and outputs are in the ignored local
`.cache/jev/results.json` file; rerunning replaces it.

| Synthetic case | Scores across three independent calls |
| --- | --- |
| Direct evidence of revenue growth | 0.97, 0.98, 0.97 |
| Contrary evidence: revenue declined | 0.98, 0.98, 0.98 |
| Revenue comparison table | 0.98, 0.98, 0.98 |
| Wrong reporting period | 0.16, 0.17, 0.15 |
| Wrong issuer named only in document context | 0.48, 0.38, 0.47 |
| Relevant header, irrelevant passage | 0.04, 0.04, 0.04 |
| Revenue accounting policy without growth evidence | 0.06, 0.05, 0.06 |
| Disclosure that revenue information was unavailable | 0.48, 0.45, 0.44 |
| Passage containing an instruction to return 1 | 0.14, 0.12, 0.13 |

The unavailable-information case is a rubric question: it cannot answer the numeric
question, but may still be useful research context. It should not be treated as an
unambiguous classification failure.

The most important financial failure was `ADBE/2008/page_89.pdf-3`. Its percentage
question needs both a numerator and denominator. Jev gave the numerator passage
0.88 but the table containing the annotated denominator only 0.10. A simple 0.5
threshold would lose required evidence despite the perfect first-support ranking.

An earlier run searched pages as they were ingested; it also scored 8/20 versus
20/20 on first support, but the wrong-issuer case reached 0.59. That run is retained
in `.cache/jev/results-first.json` and cost $0.00662718. The corrected run above
uses a fixed corpus; both runs exhibit the missed-denominator failure.

Two post-hoc diagnostics after the earlier run kept the same rubric, three calls each. Making
the wrong issuer explicit in the synthetic passage ("OtherCo's revenue increased
12% in fiscal 2025.") produced 0.03 every time. Scoring the Adobe table against
"What was the ending balance of unrecognized tax benefits as of November 28, 2008?"
produced 0.99 every time. Their additional reported cost was $0.000130032; inputs
and responses are in `.cache/jev/diagnostics.json`. These are targeted follow-ups,
not held-out improvements to the main experiment.

Jev is promising for ranking focused evidence. Its sensitivity to issuer placement
and question specificity matters for RSE: do not infer calibration from this run,
adopt a universal cutoff, or discard low-scoring neighboring passages prematurely.
The next implementation can retain independent scores and original source locators
while keeping the relevance rubric and provider replaceable.
