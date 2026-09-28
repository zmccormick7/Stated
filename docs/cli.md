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

Successful operations print one JSON value to stdout: record/link/context objects, arrays of search results, a connection object with `links` and `has_more`, a passage count from `chunk`, a JSON string from `source-text`, or an absolute database path object from `init`. Missing context returns `null`. Operational failures emit `{"error": "..."}` to stderr with exit code 1; argument errors exit 2. Help is plain text. Document search uses the configured provider; other commands run locally.

## Commands

Run `premisedb COMMAND --help` for flags and the expected payload fields. JSON keys are the existing [Python API](python-api.md) argument names; unknown keys and keys that duplicate positional arguments are errors.

| Command | Purpose |
| --- | --- |
| `init` | Create or open the database. |
| `create KIND --input FILE` | Create one of the six record types. |
| `get ID` | Read current record contents. |
| `update ID --input FILE` | Replace text and attributes, retaining identity and kind. |
| `search QUERY [--kind KIND] [--limit N]` | Search record text. |
| `contribute --input FILE` | Create several records and links in one transaction. |
| `link FROM TO RELATION --input FILE` | Connect two existing records. |
| `get-link ID` | Read a link. |
| `update-link ID --input FILE` | Replace a link's endpoints, relation and explanation. |
| `connections ID [--direction both\|in\|out] [--relation NAME] [--limit N]` | Inspect one-hop links and the records they reach. |
| `list [--kind KIND] [--author ID] [--after ID] [--limit N]` | List records in creation order. |
| `relations` | Count links by relation and the record kinds they connect. |
| `ingest TEXT_FILE --input FILE` | Capture extracted UTF-8 source text and its metadata. |
| `source-text SOURCE_ID [--start-byte N] [--end-byte N]` | Read exact source text as a JSON string. |
| `locate SOURCE_ID QUOTE\|- [--limit N]` | Find exact byte ranges of a quotation for citations. |
| `chunk SOURCE_ID [--max-chars N]` | Split source text into searchable passages. |
| `search-documents QUERY [--source-id ID] [--limit N] [--max-tokens N]` | Return relevant excerpts with citations; repeat `--source-id` to scope to several sources. |
| `set-context SOURCE_ID --input FILE` | Set the document title, summary and provenance. |
| `get-context SOURCE_ID` | Read the document context. |

## Write payloads

`--input` accepts a UTF-8 JSON file or `-` to read stdin. For records, supply `text`, `author_id` and optional `attributes` and `citations`. For `link`, supply `author_id` and optional `explanation`. For `update-link`, also supply `from_id`, `to_id` and `relation`.

For `contribute`, supply `author_id`, a `records` list and a `links` list. Each record has a caller-chosen `ref` plus `kind`, `text` and optional `attributes` and `citations`; each link has `from_id`, `to_id`, `relation` and optional `explanation`, where either end can be a new record's `ref` or an existing record ID. The output maps each `ref` to its created record and lists the created links.

For `ingest`, supply `description`, `author_id` and optional `attributes`; the separate text file is read without newline or Unicode normalization. Ingestion creates a new Source on every call and does not chunk it automatically. For `set-context`, supply `title`, `summary`, `author_id` and optional `provenance`.

Updates replace complete contents: omitted attributes/provenance become `{}`, and an omitted link explanation becomes empty. Preserve metadata you intend to keep. Each command uses the existing store operation and its transaction semantics; a series of commands is not one transaction. After an interrupted create or ingestion, check whether it committed before retrying.

Record searches return at most 10 hits by default. Connections cover both directions by default, return up to 100 links with the records at their other ends, and report `has_more`. `list` returns up to 100 records with `has_more`; continue with `--after` and the last ID. All three limits have a maximum of 1,000. The [research skill](../skills/premisedb-research/SKILL.md) explains how to use these operations during an investigation.

## Citations

`locate SOURCE_ID QUOTE` returns the exact, nonoverlapping occurrences of a quotation in source order, each with `source_id`, `start_byte`, `end_byte` and `text`. Matching is exact: whitespace, line endings, quotation marks and Unicode forms must match the source, so copy the quote from `source-text` or `search-documents` output. Pass `-` as QUOTE to read it from stdin, which avoids shell quoting; one final newline, such as the one a heredoc adds, is removed. An empty array means no exact match. At most 10 matches are returned by default (maximum 1,000); lengthen the quote when several occurrences are ambiguous.

To cite while writing, add `citations` to a `create` or `update` payload, each `{"source_id": ..., "quote": ...}` or `{"source_id": ..., "start_byte": ..., "end_byte": ...}`. The [Python API guide](python-api.md#cite-evidence) describes how quotes are resolved and linked.

## Document search

Configure `OPENROUTER_API_KEY` in the calling environment once; the CLI does not load `.env` files. The agent supplies a research question and optional source scope:

```bash
premisedb search-documents "Could customer project delays affect revenue timing?"
```

This runs the existing BM25 → reranking → score transformation → RSE pipeline automatically. Only selected excerpts are returned, each with `source_id`, `start_byte`, `end_byte`, original `text`, optional `document_context` and `token_count`. Candidate passages and internal scores are not dumped into the agent's context. Empty results mean no excerpts met the search and selection constraints; provider errors remain errors.

Defaults select up to five excerpts within a total 6,000-token budget (2,000 per excerpt), using Voyage rerank-2.5 and the existing RSE defaults. Optional `--limit` and `--max-tokens` control output size. The budget counts source text plus document headers in the configured encoding; JSON metadata and the rest of the agent's prompt are additional.

Deployment settings can override `PREMISEDB_RERANKER_MODEL` with `typesafe/jev-1.13`, `cohere/rerank-4-pro` or `voyageai/rerank-2.5`, and `PREMISEDB_TOKEN_ENCODING` with a tiktoken encoding (default `cl100k_base`). These are setup choices, not decisions the research agent needs to make for each query. Token counts refer to that encoding; select one appropriate to the consuming model. Tokenizer data may download on first use. Searches send the query, candidate text and document context to OpenRouter. The [Python API guide](python-api.md#retrieve-evidence) explains the underlying retrieval contract.
