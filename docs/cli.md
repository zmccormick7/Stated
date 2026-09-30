# Command-line interface

Install from the checkout with `python -m pip install -e '.[cli]'`. This provides `premisedb` and its document-search tokenizer; `python -m premisedb` is equivalent. The [runtime requirements](../README.md#quickstart) apply to both. The base Python library and graph commands work without the optional tokenizer.

```bash
premisedb --db research.sqlite init
premisedb --db research.sqlite create Finding --input - <<'JSON'
{"text":"PowerFrame's backlog may shift into later quarters.","author_id":"agent-a","attributes":{"basis":"inference"}}
JSON
premisedb --db research.sqlite search "PowerFrame backlog" --kind Finding
```

`--db` is a global option placed **before** the command. Without it, the CLI uses `PREMISEDB_DB` when set, otherwise `research.sqlite` in the current directory. Set `PREMISEDB_DB` in the agent's environment so every session uses the shared workspace regardless of its working directory. Only `init` creates a missing database, and it safely reopens an existing one. Parent directories must exist. Use a persistent local file and the same path across agent sessions.

`recall` output is also compact unless given `--full`:
- entry points show `entry_channels`;
- records reached by walking show a shorter text, `via` (the 8-character prefix of their entry point in the same result) and `path` as a list of relations;
- `evidence` keeps one quote per record, up to 240 characters;
- the `frontier` shows its first 10 records as short previews.

The counts left out are reported in `coverage`.

`search`, `list`, `expand`, `connections` and `links` print previews unless given `--full`. A preview holds the record's `id`, `kind`, the first 200 characters of `text`, any of the attributes `basis`, `status`, `key` and `value`, and `citations` as a count when the record cites sources. Previews from `search`, `list` and `expand` end with the last editor's `author_id`, `created_by` when the creator differs, `updated_at` and `layer`. `connections` adds to each link an `other` preview of the record at its other end, and `links` adds `from` and `to` previews; `--full` returns links alone, with `connections` also listing the complete records. Use `get` for a complete record. Successful operations print one JSON object to stdout. Lists are under a named key: `records` from `search` and `list`, `links` from `connections` and `links`, `excerpts` from `search-documents`, `matches` from `locate` and `relations` from `relations`. `source-text` prints a JSON string, `chunk` a passage count, and missing context `null`. Operational failures emit `{"error": "..."}` to stderr with exit code 1; argument errors exit 2. Help is plain text. Document search uses the configured provider; other commands run locally.

## Commands

Run `premisedb COMMAND --help` for flags and the expected payload fields. Positional record and link IDs can be full IDs or unique prefixes of at least 8 characters; The same prefixes work for IDs inside JSON payloads, including link ends and citation `source_id`s, which are stored as full IDs. A missing ID reports `Not found: ID`, and an ambiguous prefix asks for more characters. JSON keys are the existing [Python API](python-api.md) argument names; unknown keys and keys that duplicate positional arguments are errors.

| Command | Purpose |
| --- | --- |
| `init [--layer NAME]` | Create or open the database. `--layer` names the workspace's own layer when creating it (default `workspace`) and is ignored for an existing database. |
| `layers` | List layers: the workspace's own writable layer first, then any attached read-only layers, each with its `version`, `content_sha256` and `exported_at` when published. |
| `export-layer PATH --version LABEL` | Publish the workspace's own layer as a new database file at PATH (never overwritten). |
| `attach-layer PATH` | Attach a published layer file as a read-only layer, or bring an attached layer to the file's version. |
| `create KIND --input FILE` | Create one of the six record types. |
| `get ID` | Read current record contents. |
| `update ID --input FILE` | Replace text and attributes, retaining identity and kind. |
| `patch ID --input FILE` | Change text or some top-level attributes, keeping the rest. |
| `search QUERY [--kind KIND] [--limit N] [--full]` | Search record text. Previews by default; `--full` returns complete records. |
| `contribute --input FILE` | Create several records and links in one transaction. |
| `link FROM TO RELATION --input FILE` | Connect two existing records. |
| `get-link ID` | Read a link. |
| `update-link ID --input FILE` | Replace a link's endpoints, relation and explanation. |
| `patch-link ID --input FILE` | Change some of a link's endpoints, relation or explanation, keeping the rest. |
| `connections ID [--direction both\|in\|out] [--relation NAME] [--limit N] [--full]` | Inspect one-hop links, each with a preview of the record it reaches. |
| `links [--relation NAME] [--author ID] [--created-by ID] [--since UTC_TIME] [--after LINK_ID] [--limit N] [--full]` | List links in creation order with previews of both ends. `--since` keeps links created or edited at or after a UTC date or time, so it shows the connections others have added. |
| `list [--kind KIND] [--author ID] [--created-by ID] [--since UTC_TIME] [--where KEY=VALUE]... [--after ID] [--limit N] [--full]` | List records in creation order, optionally matching top-level attribute values. `--author` matches the last editor and `--created-by` the creator. `--since` keeps records created or edited at or after a UTC date or time, such as `2026-09-28T18:00`. Previews by default. |
| `expand SEED_ID... [--depth 1\|2] [--direction both\|in\|out] [--relation NAME]... [--limit N] [--per-record N] [--full]` | Walk links from seed records, reporting each record reached and its path. Previews by default; `--full` returns complete records. |
| `recall QUERY [--seed ID]... [--max-records N] [--full]` | Assemble connected research: records with paths, cited evidence, disagreements, open Questions and a frontier. Compact by default and capped at `--max-chars` (default 24,000, minimum 2,000) by dropping the lowest-ranked records with their evidence and issue links; `coverage.dropped_for_size` counts them. `--full` is uncapped. |
| `relations` | Count links by relation and the record kinds they connect. |
| `ingest TEXT_FILE --input FILE` | Capture extracted UTF-8 source text and its metadata. |
| `source-text SOURCE_ID [--start-byte N] [--end-byte N]` | Read exact source text as a JSON string. |
| `cited-by SOURCE_ID [--start-byte N] [--end-byte N] [--limit N]` | List records citing any part of a source range. An end past the text means the end. |
| `locate SOURCE_ID QUOTE\|- [--limit N]` | Find exact byte ranges of a quotation for citations. |
| `chunk SOURCE_ID [--max-chars N]` | Split source text into searchable passages. |
| `search-documents QUERY [--source-id ID] [--limit N] [--max-tokens N]` | Return relevant excerpts with citations; repeat `--source-id` to scope to several sources. |
| `set-context SOURCE_ID --input FILE` | Set the document title, summary and provenance. |
| `get-context SOURCE_ID` | Read the document context. |

## Write payloads

`--input` accepts a UTF-8 JSON file or `-` to read stdin. For records, supply `text`, `author_id` and optional `attributes` and `citations`. For `link`, supply `author_id` and optional `explanation`. For `update-link`, also supply `from_id`, `to_id` and `relation`. A link with the same ends and relation as an existing link is refused, and the error names that link; patch it instead.

For `contribute`, supply `author_id`, a `records` list and a `links` list. Each record has a caller-chosen `ref` plus `kind`, `text` and optional `attributes` and `citations`; each link has `from_id`, `to_id`, `relation` and optional `explanation`, where either end can be a new record's `ref` or an existing record ID. In record `text` and link `explanation`, `{ref:NAME}` is replaced by the ID of the record contributed as `NAME`; a placeholder naming no record in the call is an error, and attributes are stored verbatim. Other writes refuse `{ref:...}` placeholders. The output maps each `ref` to its created record and lists the created links.

For `ingest`, supply `description`, `author_id` and optional `attributes`; the separate text file is read without newline or Unicode normalization. Ingestion refuses text identical to an existing Source's and names that Source; pass `"allow_duplicate": true` to create another. It does not chunk automatically. When `OPENROUTER_API_KEY` is set, `ingest` also gives the new Source a model-written document title and one-sentence summary; there is nothing to request. If that model call fails, the Source is still ingested, the command succeeds, and a `{"warning": "..."}` line on stderr names the Source without context. For `set-context`, supply `title`, `summary`, `author_id` and optional `provenance`.

Updates replace complete contents: omitted attributes/provenance become `{}`, and an omitted link explanation becomes empty. Preserve metadata you intend to keep. For `patch`, supply `author_id` and any of `text`, `attributes` (keys to set), `remove` (keys to delete) and `citations`; unnamed attributes are kept. Each command uses the existing store operation and its transaction semantics; a series of commands is not one transaction. After an interrupted create or ingestion, check whether it committed before retrying.

Records and links report their `layer`. Writes go to the workspace's own layer; a record or link in an attached read-only layer cannot be edited, and the error says so: add your own record or link that points at it instead. Your records and links may point at attached records in either direction.

`attach-layer` verifies the file's manifest hash, then copies its rows into the workspace in one transaction, or updates the attached layer in place when a newer version of it is attached: records keep their identity across versions, so your links and citations to them stay valid. The output counts records and links added, updated and removed, and `review` lists what your own layer should reassess: `changed_linked_records`, attached records you link to whose contents changed, and `removed_upstream_but_linked`, records the new version dropped that stay in the workspace because you still link to or cite them. A version that withdraws a Source's text removes the text and everything derived from it; one that changes a Source's text is refused, since source text is fixed. Attaching the same version again changes nothing.

`export-layer` copies the workspace's own records, links, source text, passages and document context verbatim into a new PremiseDB database whose single layer is sealed and carries the version label, a content hash and the export time, so every read command works on the published file. A Source whose attributes hold `"redistributable": false` is published without its text, passages or context; the file records its text hash instead. A layer whose links reach records in attached layers cannot be exported yet.

Record searches return at most 10 hits by default. Connections cover both directions by default, return up to 100 links, and report `has_more`. `list` and `links` return up to 100 records or links with `has_more`; continue with `--after` and the last ID. These limits have a maximum of 1,000. The [research skill](../skills/premisedb-research/SKILL.md) explains how to use these operations during an investigation.

## Citations

`locate SOURCE_ID QUOTE` returns the exact, nonoverlapping occurrences of a quotation in source order, each with `source_id`, `start_byte`, `end_byte` and `text`. Matching is exact: whitespace, line endings, quotation marks and Unicode forms must match the source, so copy the quote from `source-text` or `search-documents` output. Pass `-` as QUOTE to read it from stdin, which avoids shell quoting; one final newline, such as the one a heredoc adds, is removed. An empty array means no exact match. At most 10 matches are returned by default (maximum 1,000); lengthen the quote when several occurrences are ambiguous. A write whose citation fails names it, for example `records[1] (ref 'f2'): citations[0]: ...`. When the source holds the quote with different spacing, line breaks, dashes or quotation marks, the error shows that exact source text and its `start_byte`; copy it into the quote.

To cite while writing, add `citations` to a `create` or `update` payload, each `{"source_id": ..., "quote": ...}` or `{"source_id": ..., "start_byte": ..., "end_byte": ...}`. The [Python API guide](python-api.md#cite-evidence) describes how quotes are resolved and linked.

## Document search

Configure `OPENROUTER_API_KEY` in the calling environment once; the CLI does not load `.env` files. The agent supplies a research question and optional source scope:

```bash
premisedb search-documents "Could customer project delays affect revenue timing?"
```

This runs the existing BM25 → reranking → score transformation → RSE pipeline automatically. Only selected excerpts are returned, each with `source_id`, `start_byte`, `end_byte`, original `text`, optional `document_context` and `token_count`. Candidate passages and internal scores are not dumped into the agent's context. Empty results mean no excerpts met the search and selection constraints; provider errors remain errors.

Defaults select up to five excerpts within a total 6,000-token budget (2,000 per excerpt), using Voyage rerank-2.5 and the existing RSE defaults. Optional `--limit` and `--max-tokens` control output size. The budget counts source text plus document headers in the configured encoding; JSON metadata and the rest of the agent's prompt are additional.

Deployment settings can override `PREMISEDB_RERANKER_MODEL` with `typesafe/jev-1.13`, `cohere/rerank-4-pro` or `voyageai/rerank-2.5`, and `PREMISEDB_TOKEN_ENCODING` with a tiktoken encoding (default `cl100k_base`). These are setup choices, not decisions the research agent needs to make for each query. Token counts refer to that encoding; select one appropriate to the consuming model. Tokenizer data may download on first use. Searches send the query, candidate text and document context to OpenRouter. `ingest` sends the first ~6,000 words of each new document to OpenRouter's chat model for its document context: `openai/gpt-6-luna` with `low` reasoning effort by default. Deployment settings can override these with `PREMISEDB_CONTEXT_MODEL` and `PREMISEDB_CONTEXT_REASONING`. The [Python API guide](python-api.md#retrieve-evidence) explains the underlying retrieval contract.
