# Jev relevance experiment

This opt-in experiment runs PremiseDB BM25 retrieval followed by independent
Jev relevance decisions through [OpenRouter's Decisions API](https://openrouter.ai/docs/guides/community/jev).
It is a checkout-only experiment, not a new store API or an implementation of RSE.
It uses the standard library and the runtime requirements in the main README.

From the repository root, set `OPENROUTER_API_KEY` in your environment or put a
plain `OPENROUTER_API_KEY=...` assignment in the ignored `.env` file. Environment
values take precedence. The experiment does not implement general dotenv syntax.

```sh
python -m experiments.jev_rerank --prepare-only
python -m experiments.jev_rerank
```

The first command downloads the pinned public FinQA test dataset and prepares
candidates without model calls. The second sends those candidates to OpenRouter
and incurs API charges. Both cache the dataset under `.cache/jev/`; the live run
writes scores, inputs, request IDs, served model versions, usage and timings to
`.cache/jev/results.json`, replacing the previous run. Neither command reads your
research databases. Credentials and full downloaded documents are not committed.

## What is measured

- First 20 examples in [FinQA's public test set](https://github.com/czyssrs/FinQA),
  pinned by commit and SHA-256. No selection based on model performance.
- Each example becomes one ingested source, with a title-only document header.
  Dataset text entries are individual passages; the entire table is one passage
  with column headings. These are constructed page excerpts, not original PDFs
  or output from the default character splitter.
- BM25 retrieves up to 20 passages **within the question's supplied source**.
  Jev scores exactly that pool. No annotated supporting passage is injected.
- The model sees only the query, document header and passage. Answers, reasoning
  programs and support labels are never sent. Scores remain untransformed.
- Top-1 and MRR measure the rank of the first passage containing annotated support.
  Table-row labels collapse onto the whole-table passage. Missing support counts
  as a miss; incomplete query groups are excluded and errors are reported.
- Nine authored synthetic cases, each called three times, exercise contrary
  evidence, tables, wrong company/year, relevant headers with irrelevant bodies,
  topical overlap, unavailable information and an instruction inside a passage.
  Expected labels express our initial rubric; inspect ambiguous cases directly.

Each passage gets its own request, with four concurrent requests and a 30-second
timeout. There are no automatic retries or score normalizations across candidates.
The requested model is `typesafe/jev-1.13`; responses record the served snapshot.
Timing includes network and gateway overhead and is not model inference latency.

This small demonstration does not establish calibration, broad financial retrieval
quality, corpus-wide recall, or superiority to another reranker. FinQA annotations
are supporting facts, not exhaustive relevance judgments, and the public data may
have appeared in model training. RSE needs further work on score thresholds and
which partial or contextual evidence should be included.

See [the initial results](jev-results.md) for the observed behavior.
