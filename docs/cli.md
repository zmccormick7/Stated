# Command-line interface

Install from the checkout with `python -m pip install -e .`. This provides `premisedb`; `python -m premisedb` is equivalent. The [runtime requirements](../README.md#quickstart) apply to both.

```bash
premisedb --db research.sqlite init
premisedb --db research.sqlite create Finding --input - <<'JSON'
{"text":"PowerFrame's backlog may shift into later quarters.","author_id":"agent-a","attributes":{"basis":"inference"}}
JSON
premisedb --db research.sqlite search "PowerFrame backlog" --kind Finding
```

`--db` is a global option placed **before** the command; it defaults to `research.sqlite` in the current directory. Only `init` creates a missing database, and it safely reopens an existing one. Parent directories must exist. Use a persistent local file and the same path across agent sessions.

Successful operations print one JSON value to stdout: record/link/context objects, arrays of search hits, a connection object with `links` and `has_more`, a passage count from `chunk`, a JSON string from `source-text`, or an absolute database path object from `init`. Missing context returns `null`. Operational failures emit `{"error": "..."}` to stderr with exit code 1; argument errors exit 2. Help is plain text. No model calls are made by these commands.

## Commands

Run `premisedb COMMAND --help` for flags and the expected payload fields. JSON keys are the existing [Python API](python-api.md) argument names; unknown keys and keys that duplicate positional arguments are errors.

| Command | Purpose |
| --- | --- |
| `init` | Create or open the database. |
| `create KIND --input FILE` | Create one of the six record types. |
| `get ID` | Read current record contents. |
| `update ID --input FILE` | Replace text and attributes, retaining identity and kind. |
| `search QUERY [--kind KIND] [--limit N]` | Search record text. |
| `link FROM TO RELATION --input FILE` | Connect two existing records. |
| `get-link ID` | Read a link. |
| `update-link ID --input FILE` | Replace a link's endpoints, relation and explanation. |
| `connections ID [--direction in\|out] [--relation NAME] [--limit N]` | Inspect one-hop links. |
| `ingest TEXT_FILE --input FILE` | Capture extracted UTF-8 source text and its metadata. |
| `source-text SOURCE_ID [--start-byte N] [--end-byte N]` | Read exact source text as a JSON string. |
| `chunk SOURCE_ID [--max-chars N]` | Split source text into searchable passages. |
| `search-documents QUERY [--source-id ID] [--limit N]` | Search passages; repeat `--source-id` to scope to several sources. |
| `set-context SOURCE_ID --input FILE` | Set the document title, summary and provenance. |
| `get-context SOURCE_ID` | Read the document context. |

## Write payloads

`--input` accepts a UTF-8 JSON file or `-` to read stdin. For records, supply `text`, `author_id` and optional `attributes`. For `link`, supply `author_id` and optional `explanation`. For `update-link`, also supply `from_id`, `to_id` and `relation`.

For `ingest`, supply `description`, `author_id` and optional `attributes`; the separate text file is read without newline or Unicode normalization. Ingestion creates a new Source on every call and does not chunk it automatically. For `set-context`, supply `title`, `summary`, `author_id` and optional `provenance`.

Updates replace complete contents: omitted attributes/provenance become `{}`, and an omitted link explanation becomes empty. Preserve metadata you intend to keep. Each command uses the existing store operation and its transaction semantics; a series of commands is not one transaction. After an interrupted create or ingestion, check whether it committed before retrying.

Searches return at most 10 hits by default. Connections default to 100 links and report `has_more`; both limits have a maximum of 1,000. The [research skill](../skills/premisedb-research/SKILL.md) explains how to use these operations during an investigation. Configurable reranking and token-budgeted retrieval remain available through the [Python API](python-api.md#retrieve-evidence).
