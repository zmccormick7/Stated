# Connected recall: first-increment design

[Back to PremiseDB](../README.md)

**Status: proposed, for review.** This scopes a first increment of the connected `search` described in [database-spec.md §7–8](database-spec.md#7-retrieval-primitives). The CLI already has a `search` command for record text, so the operation is called `recall`, after the **Recall → Contribute → Revise** workflow. It builds only on existing primitives and leaves the document-retrieval configuration unchanged: rerankers, score transforms and RSE settings.

## Why

Round 1's synthesis question has been rerun three times on the AI infrastructure workspace as tools were added. The agent now answers well, but it still makes **16–17 tool calls and 54–71 CLI commands** per answer, in the same loop every time:

1. `search` for entry points;
2. `expand` or `connections` from them;
3. `get` on almost every record reached (24–34 calls), to read citations and to follow record IDs written in prose;
4. `source-text` to check the cited passages.

Much of what matters is reachable only through links, and the last run found evidence nobody had cited only through `cited-by`. Smaller CLI changes have stopped reducing the count. The remaining cost is that the agent assembles, by hand, a bundle the database could assemble: related records, the path to each, their cited text, and the disagreements and open Questions around them.

## What one call returns

```bash
premisedb recall "What limits AI deployment through 2028?" [--seed ID]... [--documents] [--max-records N]
```

The call returns one JSON object:

| Field | Contents |
| --- | --- |
| `records` | Selected records as previews (ID, kind, text preview, `basis`, `status`, `key`, `value`), each with `why`: the channel that found its entry point and the `path` of links from it. |
| `evidence` | For each selected record with citations: the cited quotes and their locators, read from the attribute or, for older citations without quotes, from the source by byte range. Deduplicated across records. |
| `issues` | Links touching selected records with relation `contradicts`, `qualifies` or `updates`, and linked Questions whose `status` is `open` or `partly_answered`. |
| `excerpts` | With `--documents`: the `search-documents` excerpts used as entry points, with locators and token counts. |
| `frontier` | Records that were capped, or reached but not selected, so the agent can continue with `expand`. |
| `coverage` | Channels that ran, limits reached, and whether the result is truncated. |

The caller still decides what the evidence means. `recall` returns navigation and evidence, not a conclusion.

## How it works

1. **Entry points.**
   - `search_records(query)` supplies ranked record hits, and `--seed` IDs come first.
   - With `--documents`, the existing `search-documents` pipeline runs **exactly as configured today**. Each excerpt's range goes through `cited_by`, so records citing that passage become entry points.
   - Channels are combined by reciprocal rank fusion, with stable tie-breaking. Raw scores from different channels are never added together.
2. **Walk.** `expand` runs to depth 2 from the top entry points, with a per-record cap. The walk follows the reasoning relations (`depends_on`, `qualifies`, `contradicts`, `corroborates`, `updates`, `raises`, `answered_by`, `partly_answered_by`) and `in_model`. `about` gets a single hop, so company hubs do not flood the result. `in_layer` is not followed.
3. **Select.** Records are ranked by their entry point's fused rank, discounted per hop, with a documented weight per relation. Reasoning relations outrank `about`, and `supported_by` is used only to fetch evidence. Selection stops at `--max-records` (default 25). The ranking is a documented, deterministic baseline; a learned scorer can come later if measurement shows a need.
4. **Evidence, issues and frontier.** These are assembled from the selected records with direct reads. There are no model calls beyond the optional document channel.

**Not in this increment:** query rewriting and subqueries, embeddings and Faiss, filters such as `about_any` and dates, novelty from `research_context`, and token-budget selection across records and excerpts. All remain as described in the spec.

## Increments

1. **Graph recall** (about 300 lines): record search and seeds, the walk, selection, evidence quotes, issues, frontier and coverage. No provider calls.
2. **Document channel:** `--documents` adds `search-documents` excerpts and `cited-by` entry points, and bundles excerpts as evidence.
3. **Measure and adjust the graph-selection weights only.** The document-retrieval configuration stays as it is.

## How we measure it

- **Benchmark.** Rerun the synthesis question with `recall` available. Compare tool calls, CLI commands, tokens and time with the three earlier runs, which took 16–22 tool calls and 54–71 commands.
- **Key-record coverage.** Check how many of the 15 records cited in all three earlier answers a single `recall` call returns: `012c9c96`, `296cd8dc`, `2efe0496`, `31de4563`, `35221136`, `3ee9f7a0`, `433c1083`, `46edde7a`, `803bab78`, `8b93e885`, `b75aa242`, `cbbd9443`, `d0c0c5ef`, `fc4ba6e5` and `fe723b13`. These records are agents' judgments of what matters, not ground truth, so the measure is a proxy.
- **Evidence traces.** Recorded paths from earlier tests should appear with their `path`:
  - CoreWeave's company-wide power backlog (`873c7b65`) to NVIDIA's `acie_growth_fy28` input;
  - GE Vernova's release to the workbook's `gw_capacity`.
- **Unit tests** on small synthetic graphs: fusion order, hop discounting, hub capping, `issues` and quote resolution.

## Decisions for review

1. **Name:** `recall`, rather than overloading the existing `search` command.
2. **Document channel:** off by default in increment 1, and an explicit `--documents` flag in increment 2, because it makes paid provider calls. The alternative is on whenever a provider key is configured.
3. **Default size:** 25 records, with no token budget in the first increment.
4. **Relation weights:** the order above, reasoning relations before `about`, to be documented in the skill and checked against the benchmark.
