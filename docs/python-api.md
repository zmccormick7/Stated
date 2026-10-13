# Python API guide

[Back to PremiseDB](../README.md)

[Records](#try-the-record-store) · [Links](#connect-existing-records) · [Sources](#ingest-source-text) · [Citations](#cite-evidence) · [Contribute](#contribute-several-records-at-once) · [CCH](#add-document-context) · [Reranking](#rerank-document-evidence) · [RSE](#retrieve-evidence) · [Record types](#what-the-graph-remembers)

Examples below build on the same `research.sqlite` database. Reranking and integrated retrieval use a configured provider; the other examples run locally.

## Try the record store

From this checkout, use Python 3.12+ linked against SQLite 3.51.3+ with FTS5 enabled (including the [WAL-reset fix](https://sqlite.org/wal.html#walresetbug)). The library has no required third-party runtime dependencies.

```python
from premisedb import RecordStore

with RecordStore("research.sqlite") as db:
    first = db.create("Finding", text="Ramp expected in June", author_id="alice")
    second = db.update(first.id, text="Ramp delayed to September", author_id="bob")
    assert db.read(first.id) == second
    print(second.updated_at)
```

Run tests from the repository root with `python -m unittest discover -s tests -v`.

All table, index and trigger definitions live in [premisedb/schema.sql](../premisedb/schema.sql).

Each record has a stable ID and kind, current text, JSON attributes, the last editor's `author_id`, the creator's `created_by` (which edits never change), a database-assigned UTC `updated_at` timestamp, and `as_of`, the date its contents are as of: the cutoff the writer worked at, or else the date of the write. Updates replace text and attributes in full; omitted attributes become `{}`. `patch(id, author_id=..., text=None, attributes=None, remove=(), citations=None)` changes only what it names. It optionally replaces the text, sets or replaces the given top-level attributes (values are replaced whole, not merged), deletes the keys in `remove` (absent keys are ignored), and resolves `citations` as `update` does. It reads and writes the current record in one transaction, so patches to different attributes are all kept, and the last committed patch wins for the same key. A patch that names nothing, or both sets and removes the same key, raises `ValueError`. Missing references raise `KeyError`. Invalid SQL-constrained fields raise `sqlite3.IntegrityError`. Returned metadata is a detached copy; changing it does not update the database.

A change to a Variable's `value` is not an edit but a new version. When `patch` or `update` would give a Variable that has a value a different one, the record is left as it is and a new Variable is written with the new contents, stamped with the day (or the cutoff), linked `updates` the current one and carrying its outgoing links other than its own `updates` and `supersedes`; the call returns that new record, whose `id` differs. The earlier version keeps its value and its date, so the model's inputs can be read as they stood on any date and a past decision reviewed. Setting a value where there was none, and changing text or other attributes, edit in place as before. The cutoff rule does not stop a version, since nothing is edited.

## Point-in-time reads

`RecordStore(path, as_of="2023-02-24")` opens the store at a cutoff date. Every read then sees only what a reader on that date could have seen, and every write is stamped with that date. The `record_visibility` view in the schema says when each record is first seen:

- a Source from its publication date, `attributes.published`, which must start with a `YYYY-MM-DD` date; a Source without one is never seen at any cutoff;
- a disclosed Finding (`attributes.basis` is `"disclosed"`) from the latest publication date of the Sources it is `supported_by`, whenever it was written, because its figure is checked against those sources; when it cites no Source, or one without a date, from its stamp;
- an Entity always, as reference data;
- every other record from its stamp, `as_of`;
- a link from the latest of its stamp and the dates its two ends are seen from.

This is a filter on evidence dates and stamps, not a reconstruction: earlier contents are not kept. Reads of a record, link or Source the cutoff hides raise `KeyError` saying so, as does a write at a cutoff that would cite or link one; `search_records`, `search_documents`, `list_records`, `list_links`, `connections`, `expand`, `cited_by`, `relation_usage` and the model checks leave hidden rows out. `as_of` must be a `YYYY-MM-DD` date or `None`.

What a reader at a date sees is not rewritten from another date. A record or link written at a cutoff (its `as_of` is earlier than the date of its `updated_at`) can be edited only by a store opened at that cutoff, and one written without a cutoff only by a store without one; `update`, `patch`, `update_link`, `patch_link` and `merge` raise `ValueError` otherwise, saying to add a record and link it `updates` or `supersedes`. A live edit of a live record restamps it to the day, so it is seen from then on. Entities are exempt, being always seen. `recall` treats a record with an incoming `supersedes` link as retired, with or without a retired status, since a record written at another cutoff cannot have its status changed. At a cutoff, `create("Source", ...)`, `ingest_source` and edits of a Source require `attributes.published`, a date on or before the cutoff. `read_document_context` returns `None` and `search_documents` segments carry no `document_context` at a cutoff: a summary is written on the day it is generated, whatever its document's date.

## Layers

Every record and link belongs to a **layer**, reported as `layer` on `Record` and `Link`. A new database has one layer, named by `RecordStore(path, layer="workspace")` when the file is created and ignored afterwards; `layers()` lists the layers as `Layer(name, version, writable)` objects, the workspace's own first. Writes always go to the workspace's own layer, the only writable one. Records and links in any other layer are copies of a graph published elsewhere: editing them raises `sqlite3.IntegrityError`, and rows never move between layers. Build on them instead, with your own records and links, which may point at records in any layer in either direction. Your records and links may point at attached records in either direction.

`export_layer(path, version=...)` publishes the workspace's own layer: it copies its records, links, source text, passages and document context verbatim into a new PremiseDB database at `path`, whose single layer is sealed (`writable` false) and carries `version`, `content_sha256` and `exported_at`. The hash covers the copied rows and each source text's hash, so exports of unchanged contents agree. A Source whose attributes hold `"redistributable": false` is published without its text, passages or context; the file's `withheld_sources` table keeps its text hash so a workspace holding the document can verify it. The returned `LayerExport` counts records, links, sources with text and withheld sources. Existing files are never overwritten, and a layer whose links reach records in attached layers raises `ValueError` for now.

`attach_layer(path)` attaches a published file as a read-only layer, or brings an already attached layer to the file's version. It verifies the file holds exactly one sealed layer whose contents match its manifest hash, refuses a file whose layer name is the workspace's own or whose records exist in another layer, then applies the file in one transaction. Records and links are matched by ID: existing rows are updated in place, so record keys and therefore the workspace's links and citations survive versions; new rows are added; rows the new version lacks are deleted, except records the workspace still links to or cites, which stay and are reported. Source text is fixed: a version may add or withdraw a Source's text (withdrawal also removes its passages, context and indexed citations) but a different text for the same Source raises `ValueError` and nothing changes. The returned `LayerAttachment` counts records (`added`, `updated`, `removed`, `kept`) and links, and its `review` dict names `changed_linked_records`, attached records the workspace links to whose contents changed, `retired_linked_records`, those whose new contents carry status `superseded` or `retracted`, and `removed_upstream_but_linked`. Attaching the same version again is a no-op.

One table stores current records and another stores current links. Writes are atomic, and the last committed update wins. There are no revision counters, stale-edit checks or historical reads; a [cutoff](#point-in-time-reads) filters current rows by date. Schema format numbers only identify file layouts. During early development, incompatible formats raise `ValueError`; create a new database to use the current schema. No migrations are provided. Use one store per thread on a local filesystem; the API and schema are experimental.

## Connect existing records

```python
with RecordStore("research.sqlite") as db:
    finding = db.create("Finding", text="Ramp requires utility approval", author_id="alice")
    company = db.create("Entity", text="Supplier A", author_id="alice")
    link = db.create_link(finding.id, company.id, "about", author_id="alice")
    incoming = db.connections(company.id, direction="in", relation="about")
    assert incoming.links == (link,) and not incoming.has_more
    assert incoming.records[finding.id] == finding
```

`resolve_id(value, link=False)` returns the full record (or link) ID for an exact ID or a unique prefix of at least 8 characters. It raises `KeyError` when nothing matches and `ValueError` when a prefix matches more than one. Operations that take record or Source IDs, including link ends and citation `source_id`s, accept the same prefixes and store full IDs. Links also carry `created_by`. `read_link(id)` returns current contents. `update_link` replaces its endpoints, relation and explanation in place, records the last editor and refreshes `updated_at`. `patch_link(id, author_id=..., from_id=None, to_id=None, relation=None, explanation=None)` changes only the fields given, in one transaction, and keeps the rest. Links always refer to current records. Agents reassess support when underlying understanding changes; the database does not preserve earlier assumptions or automatically determine whether a relationship is still valid.

`connections` returns links touching a record in creation order, with an optional exact relation filter. `direction` is `"both"` by default, or `"out"` or `"in"`; links keep their stored direction, so check `from_id` and `to_id`. `records` maps the ID at the other end of each returned link to that record's current contents, in first-seen order; a self-link maps the record to itself. The default limit is 100 links, the maximum is 1,000, and `has_more` reports omitted links in the chosen direction. Missing IDs raise `KeyError`; custom relation labels are allowed. Link deletion is not implemented.

One link connects two records with a given relation. `create_link`, `contribute`, `update_link` and `patch_link` raise `ValueError` naming the existing link when a write would add or produce a second link with the same `from_id`, `to_id` and `relation`; patch the existing link instead. Links in the opposite direction or with another relation are distinct.

`expand(seed_ids, depth=1, direction="both", relations=None, limit=100, per_record_limit=50)` walks current links breadth-first from up to 100 seed records, one or two hops, and returns an `Expansion`:
- `reached` lists each record once as a `Reached(record, depth, path)`, where `path` is the chain of links from a seed. Seeds come first at depth 0, then records in the order reached, each at its shortest depth with the first path found.
- `relations` restricts every hop to the named relations. `direction` applies at every hop.
- From any one record, at most `per_record_limit` links to records not yet reached are followed; records with more appear in `capped`, so hubs such as a layer Entity cannot flood the walk.
- `has_more` reports that `limit` (records beyond the seeds, 1–1,000) stopped the walk.

The walk reads one consistent snapshot. Unbounded recursion and path finding are not provided.

`relation_usage()` returns a `RelationUsage` for each combination of relation, source record kind and target record kind among current links, with its link count, ordered by relation, then by count. Agents use it to reuse the relation names a workspace already has. The database does not restrict relation labels; the [research skill](../premisedb/skills/premisedb-research/SKILL.md#relations) defines the standard vocabulary.

## List records

```python
with RecordStore("research.sqlite") as db:
    page = db.list_records(kind="Question", author_id="alice", limit=100)
    while page.has_more:
        page = db.list_records(kind="Question", author_id="alice", after=page.records[-1].id)
```

`list_records` returns a `RecordPage` of current records in creation order, optionally filtered by exact `kind`, `author_id` (the last editor), `created_by`, `since` (a UTC ISO date or time; keeps records created or edited then or later) and `where`, a dict of top-level attribute values that must all match. `where` values are strings, finite numbers, booleans or `None`, and match only the same JSON type: `1`, `1.0`, `True` and `"1"` are distinct, and `None` matches an explicit null, not a missing key. Pass the last returned ID as `after` to continue; `has_more` reports whether more matching records follow. The default limit is 100 and the maximum is 1,000. Invalid kinds, limits or author types raise `ValueError`, and an unknown `after` ID raises `KeyError`. Records created after a page is read appear on later pages.

`list_links(relation=None, author_id=None, created_by=None, since=None, after=None, limit=100)` pages through current links the same way and returns a `LinkPage` of `links` and `has_more`. `since` keeps links created or edited at or after the given UTC time, so an agent can see the connections others have added to the graph; `after` takes a link ID or unique prefix.

## Check a model's formulas

A Variable with `role: output` may carry a `formula` attribute computing its `value` from the `key`s of Variables in the same Model (connected to it by `in_model`). Several Variables of a Model may share a `key` when they are versions of one quantity written as of different dates; the model operations read the latest version the store sees, by `as_of` and then creation, so a store opened at a cutoff reads the model as it stood then. `evaluate(formula, values)` evaluates one formula over a dict of key → value; `RecordStore.check_model(model_id, tolerance=1e-4)` evaluates every output formula of a Model from the stored values of its Variables and returns a `ModelCheck`: `inputs` and `outputs` counts, `checked` as `VariableCheck(key, stored, computed, matches, exact, problems)` per output with a formula, and `without_formula` keys: `matches` is within the tolerance and `exact` is equal number for number, false when the stored value is rounded. Each formula is checked on its own against stored values, so one wrong output does not fail its dependents, and a formula reads only the Variables it names, so a null value elsewhere in the model stops nothing. A Variable whose `value` is null is unknown: a formula that reads it yields null, which matches a stored null. A formula must name only Variables of the model that the output also links `depends_on`; naming others, its own Variable, an unknown key, or failing to evaluate is reported in `problems` rather than raised. Two Variables sharing a key, a non-Model ID, or a tolerance outside `[0, 1)` raise `ValueError`.

The language: numbers, keys, `+ - * / **`, unary minus, comparisons (chained allowed), `a if cond else b`, and the functions `min`, `max`, `sum`, `abs`, `prev`, `next`, `at` and `fill`. A value is a number or a row: a list labelled by the Variable's `periods`. A row keeps its own periods through `prev`, `next`, `at` and single-row aggregates; when rows combine, each is read at the output's periods, with `null` where a label is missing, or at identical labels when the output has none. Unlabelled lists combine by position and must have equal lengths. Scalars broadcast, and a `null` operand yields `null`. `min`, `max` and `sum` of one row aggregate across its periods, ignoring `null`; with several arguments they apply per period. `prev(x)` shifts a row one period later with `null` first and `prev(x, start)` fills that first period; `next(x[, end])` shifts the other way. `at(x, "2027")` returns one period's value from the row, and `fill(x, v)` replaces `null` by `v` over the output's periods. A formula may name its own Variable, only inside `prev(...)`, to define a recurrence; `evaluate(..., self_key=key, target=periods)` then computes it one period at a time. Anything else, including attribute access, indexing, other strings, booleans and other calls, is refused by `parse` with `ValueError`. `evaluate` takes `periods` (key → labels), `target` (the output's labels) and `self_key` as keyword arguments; `check_model` supplies them from the Variables.

`RecordStore.evaluate_model(model_id, overrides=None)` answers "what if": it recomputes the Model's formulas with some Variables held at other values and returns a `ModelEvaluation` of `model_id`, `overrides`, `variables` and `without_formula`. Each `VariableValue` has `key`, `role`, `stored` (the Variable's stored value), `value` (the override, the formula's result, or the stored value when there is no formula) and `changed` (whether the overrides moved it, compared with evaluating without any). Unlike `check_model`, results flow through: a formula reads the recomputed values of the Variables it names, in dependency order, so `variables` lists Variables without a formula first and then formulas in the order they were computed. An override must name a Variable of the model and have its stored value's shape, a number or a list of the same length; overriding an output holds it there and skips its formula. Nothing is written: to keep a scenario, contribute your own Variables. A formula that cannot be parsed or evaluated, formulas that depend on each other in a cycle, an unknown override key or a mis-shaped override raise `ValueError`; `depends_on` declarations are `check_model`'s concern and are not required here.

`RecordStore.fill_model(model_id, author_id=...)` stores what `evaluate_model` computes: each output with a formula whose stored value is not exactly the formula's result, in dependency order, returned in `ModelFill.written` as `FilledVariable(key, stored, value, id)`. An output the store may edit is patched in place. One written at another cutoff cannot be edited, so it gets a new version: a Variable with the same text and attributes and the new value, linked `in_model`, `updates` the old version and `depends_on` the current version of each Variable the formula names; `id` is that new Variable. The old value stays true of its own date. Inputs and outputs without a formula are untouched, and a formula that cannot be evaluated raises `ValueError` before anything is written.

`RecordStore.state_rows(schema=None, entity_id=None, start=None, end=None, limit=1000)` exports the states written under a schema as change points. For each Model whose attributes name a `schema`, and each Entity it is `about`, it returns a `StateRow` at `start` holding the latest version of every Variable key stamped by then, and one at each later date up to `end` on which some key changed, each with `entity_id`, `entity`, `schema`, `date`, `model_id`, `values` (key → value), `periods` (labels of the keys holding rows) and `stamps` (key → the `as_of` of the version in use, which is when that key last changed). Between rows the state is unchanged, so a daily series is a forward fill of these rows. `end` defaults to the store's cutoff, or today, and `start` to `end`, so with neither there is one row per Model, its current state; `end` may not pass the cutoff. A Model about no Entity has `None` for both Entity fields. This is the feature table of the point-in-time mode, read from the graph rather than from any session.

## Check an answer document

`check_answer(db, answer)` checks the structured answer an agent returns for a question and gives an `AnswerCheck` of `ok`, `problems` and `answer`. The document is a dict with `title` (one line, at most 120 characters), `body`, and optional `sources` and `blocks`; the [answer skill](../premisedb/skills/premisedb-answer/SKILL.md) describes it for agents. `[n]` in the body or a text block cites `sources[n - 1]`. A source names a `record` and may carry a `quote`, which must be text of the record or of a passage it cites. Blocks are `text`, `quote` (shows a source's quote), `chart` (rows of labelled parts with a `unit`, and optionally `whole`, `rest_label`, `caption` and `lead`) `flow` (a `from` node, proportional `parts` as in a chart with optional `note`s, and an optional `to` node with `items`) and `lever` (a `model` with an `input` key to drag and an `output` key to watch, both single-valued Variables of that model, with optional `input_label`, `output_label`, `range`, `caption` and `lead`; the resolved block carries each end's record, value and units and the slider's `low` and `high`, from `range`, else the Variable's own `low` and `high`, else half to one and a half times the value); at most six blocks and one lead.

Every charted figure names a record. A Variable supplies its stored `value` (with `period` for a row), and the document must not restate it. For any other record the part gives `value`, `as_written` and optionally `scale`: `as_written` must appear in the record's text or a passage it cites, standing alone and not inside a longer number, and `as_written × scale` must equal `value` to within 0.5%. This stops invented or mistyped figures; it cannot tell whether a real figure is used with the right meaning.

Nothing is raised for a bad document: unknown keys at any level, missing or retired (`superseded`, `retracted`) records, uncited sources, misplaced `[n]` markers, quotes or figures that are not in the record, and parts that exceed `whole` are all listed in `problems`, and `answer` is `None`. When `ok`, `answer` is the document with full record IDs, each source's `kind`, and each part reduced to `label`, `record` and `value`.

## Ask through the user's own agent

`merge(keep_id, duplicate_id, author_id=...)` folds a duplicate record into the one kept and returns a `Merge` with the links `moved` and `dropped` (ones the kept record already had): the duplicate must be in the writable layer and of the kept record's kind, not a Source; the kept record's `aliases` gain the duplicate's name and aliases when it is editable; a `supersedes` link is added and the duplicate's `status` becomes `superseded`. Each step is its own transaction.

`premisedb.fetch.fetch_page(url, contact=None)` downloads an HTML or plain-text page or a PDF and returns a `Page` with its final `url`, its `text` (`html_text(html)` for HTML, `pdf_text(data)` for a PDF, which needs `pypdf`) and `retrieved_at`; it raises `ValueError` for other content types, failed requests, SEC.gov addresses without a contact, and when `PREMISEDB_FETCH` is `0`. `contact(db_path)` reads `PREMISEDB_CONTACT` or `contact.txt` beside the database.

`premisedb.ui` is the answer UI. `premisedb ui` serves it; `premisedb.ui.server.make_server(db_path, harness=..., port=0)` builds the same `ThreadingHTTPServer` for tests, with `GET /api/threads`, `GET /api/threads/ID`, `POST /api/threads` and `POST /api/threads/ID/turns` (JSON `{"question": ..., "effort": 1 | 5 | 20 | 60 | 180 | 480, "agents": 1 | 2 | 3 | 5 | 10}`; agents applies to a run and is 3 when omitted, effort in minutes and 5 when omitted, one running turn per thread) `POST /api/models/ID/evaluate` with `{"overrides": {key: value}}` returns the model's `values` by key and the `changed` keys, which the lever block calls as it moves; and the page under `/`. `ThreadStore(path)` keeps conversation threads in their own SQLite file, apart from the graph: `create_thread(question, harness=name)`, `threads()`, `thread(thread_id)` (with its turns), `start_turn(thread_id, question)`, `add_progress(turn_id, text)` and `finish_turn(...)`. A file with another schema version is refused.

`run_turn(store, turn_id, db_path=..., harness=...)` answers a started turn. The turn's effort, given to `start_turn(thread_id, question, effort=5, agents=1)`, is a key of `premisedb.ui.threads.EFFORTS` or one of `RUN_EFFORTS`. An effort in `RUN_EFFORTS` is carried out by `premisedb.ui.runs.run_research`: a plan, pieces of work queued with `add_items(turn_id, items)` (each a `(title, text)` pair: a one-line title for the page and the lists agents see, and the detail the worker acts on), claimed with `next_item(turn_id)` and closed with `finish_item(item_id, result=... | error=...)`, each done in a fresh session, `agents` of them at a time on a thread pool, then a report checked like any answer. A harness raises `UsageLimit` (a `HarnessError` with `resets_at`, a Unix time or `None`) when the subscription's limit is reached (the reset time is read from a `|<unix time>` suffix or a `resets 3:10pm (America/Denver)` clause); a run puts the piece back with `requeue_item(item_id)` and waits, a single-session turn fails with the message; `thread()` returns each turn's `items` (with each one's latest `progress` line) and `notes`; `control(turn_id, action)` takes `pause`, `resume` or `finish` for a running run, `add_note(turn_id, text)` adds a note the run's later sessions see, and `notes(turn_id)` lists them. The server exposes these as `POST /api/turns/ID/control` (`{"action": ...}`) and `POST /api/turns/ID/notes` (`{"text": ...}`), and `GET /api/threads/ID` gives a running run a live `written` count. `unfinished()` lists turns still marked running and `requeue_running(turn_id)` puts a run's claimed pieces back; `App` calls both on start (`resume()`), restarting each run in the background and failing each single-session turn. A run's clock is the turn's `started_at`, so a restarted run has only the time it had left. Otherwise the effort sets the time budget the prompt states, whether the harness is run with `web=True`, and the `timeout` after which the harness is killed and the turn fails (`time_limit(effort)` seconds for the whole turn, repair included), and whether an agent that finishes with less than `USE_OF_TIME` of its effort spent is resumed with the time used and asked what would improve the answer. It replies with an updated document, which is checked like the first, or with `DONE`; the last answer that passed the check stands. It runs the question through the harness in the workspace directory with `PREMISEDB_DB` set, parses the JSON answer document from the agent's final message, checks it with `check_answer`, and gives the agent one chance to fix what the check reports. The turn ends `done` with the resolved `answer`, or `failed` with an `error`; either way it records `progress` (one line per tool call), `written` (records the agent added or edited in the workspace's own layer during the turn, by kind) and `denied` (tools the harness refused). The first prompt of a thread names the research and answer skills, which `premisedb ui` installs in the harness; later turns resume the harness's session.

`ClaudeCode(executable="claude")` is the first harness: `run(prompt, cwd=..., env=..., session_id=..., on_progress=...)` starts Claude Code headless (`-p --output-format stream-json`, plus `--resume` for a follow-up) and returns a `Reply` of `text`, `session_id` and `denied`. It adds `--allowedTools "Bash(premisedb:*)" Read Write`, the least the product needs, on top of the user's own settings. A harness that cannot start, ends without a result or reports an error raises `HarnessError`. `Codex(executable="codex", model=None, reasoning_effort=None)` runs `codex exec --json --skip-git-repo-check -s workspace-write` (with `resume SESSION_ID` for a follow-up), reads the final message from the `-o` file, turns each command execution into a progress line, and reports no refusals: in exec mode Codex asks no one, and a command its sandbox refuses simply fails. `premisedb.ui.server.install_skills(harness)` copies the bundled skills into the harness's `skills_directories()`. Any object with a `name`, `skills_directories()` and the same `run` works as a harness.

## Recall connected research

```python
from premisedb import RecordStore, recall

with RecordStore("research.sqlite") as db:
    result = recall(db, "utility approval construction", seeds=[company.id])
    for item in result.records:
        print(item.score, item.record.text, [link.relation for link in item.path])
    for evidence in result.evidence:
        print(evidence.quote, evidence.record_ids)
```

`recall(db, query, seeds=(), max_records=40, entry_limit=10)` assembles connected research in one call, without provider calls.

1. **Entry points.** Record-search hits (up to `entry_limit`) and `seeds` are combined by reciprocal rank fusion, with seeds weighted double.
2. **Walk.** From each entry point it walks two hops along reasoning relations and one hop along `about`. It does not walk `in_layer` or `supported_by`.
3. **Scoring.** A reached record scores its entry point's score times `RELATION_WEIGHTS[relation] × HOP_DISCOUNT` per hop, keeping the best path. Records whose `status` attribute is `superseded` or `retracted` are walked through but never selected or listed on the frontier; `coverage["retired"]` counts them. The top `max_records` (1–100) become `records`, as `Recalled(record, score, entry_id, channels, path)`.

The result is a `Recall`:

| Field | Contents |
| --- | --- |
| `records` | The selected records, best first. |
| `evidence` | The deduplicated cited ranges of selected records with their quotes. Older citations without quotes are read from the source by byte range. |
| `issue_links` | `contradicts`, `qualifies`, `updates` and `supersedes` links touching selected records. |
| `open_questions` | Questions with status `open` or `partly_answered`, linked to selected records through reasoning relations, or selected themselves. |
| `frontier` | Up to 50 records reached but not selected, then records whose links were capped. |
| `coverage` | Counts and whether the result is truncated. |

Each step reads the current graph separately, so concurrent writes may appear partially.

## Search for a starting point

```python
with RecordStore("research.sqlite") as db:
    results = db.search_records("utility approval", kind="Finding", limit=10)
    for record in results:
        print(record.text, db.connections(record.id).links)
```

`search_records` returns a tuple of current `Record` objects, searching only their `text`. The query is plain text: punctuation separates words, any query word can match, and quotes or boolean words have no query-language meaning. Empty or punctuation-only queries return an empty tuple. FTS5 supplies case-insensitive token matching and [BM25 ranking](https://sqlite.org/fts5.html#the_bm25_function); ties use record creation order.

The optional `kind` filter applies before the result limit. The default limit is 10 and the maximum is 1,000; results have no pagination or truncation flag yet. Invalid kinds, limits or non-string queries raise `ValueError`. SQLite triggers keep the index synchronized in the same transaction as each record write, so updated text replaces earlier searchable text immediately after commit.

This uses schema format 11. Databases created by earlier implementations must be recreated; no migration is performed.

## Ingest source text

```python
with RecordStore("research.sqlite") as db:
    source = db.ingest_source(
        description="Supplier A — Q2 filing",
        document_text="Utility approval is required before construction.",
        author_id="alice",
        attributes={"origin": "supplier-a-q2.txt"},
    )
    assert source.kind == "Source"
    assert db.read_source_text(source.id, start_byte=0, end_byte=7) == "Utility"
```

`ingest_source` accepts already extracted text and returns a new Source record. Its `text` is the short description searched by `search_records`; the document body stays in a separate `source_content` table. The Source, its search entry and its UTF-8 body commit together. Text identical to an existing Source's raises `ValueError` naming that Source, unless `allow_duplicate=True`, which creates a new Source identity. Caller-supplied provenance belongs in `attributes`.

The body and its SHA-256 are stored in SQLite for this first ingestion slice. Text is preserved exactly, including whitespace, line endings and Unicode normalization. `read_source_text(id)` returns the full text; optional offsets select a half-open UTF-8 byte range `[start_byte, end_byte)`, fetching only that range. Invalid ranges or offsets inside a character raise `ValueError`. Missing IDs and records without ingested text raise `KeyError`.

`locate_source_text(id, quote, limit=10)` finds exact, nonoverlapping occurrences of a quotation and returns them in source order as `SourceSegment` objects with byte ranges suitable for citations and no document context. Matching compares UTF-8 bytes without whitespace, quotation-mark or Unicode normalization; no match returns an empty tuple. Blank quotes and limits outside 1–1,000 raise `ValueError`. The search reads the whole document into memory.

Source descriptions and metadata remain editable. Captured text stays fixed so future citation offsets keep their meaning; ingest a separate Source for different text. This is evidence identity, not record edit history. Parsing, original-file storage and section context will be added in later increments. Ingestion currently holds one document's text in memory and makes no model calls.

## Cite evidence

```python
with RecordStore("research.sqlite") as db:
    finding = db.create(
        "Finding", text="Utility approval gates construction.", author_id="alice",
        attributes={"basis": "disclosed"},
        citations=[{"source_id": source.id, "quote": "Utility approval is required"}],
    )
    assert finding.attributes["citations"] == [{"source_id": source.id, "start_byte": 0,
                                                "end_byte": 28, "quote": "Utility approval is required"}]
    assert db.connections(finding.id, relation="supported_by").links[0].to_id == source.id
```

`create` and `update` accept optional `citations`: an iterable of objects with a `source_id` and either an exact `quote` or a `start_byte`/`end_byte` range. A quote must occur exactly once, compared as with `locate_source_text`; add `start_byte` (or `end_byte`) to choose among repeated occurrences, and the error lists their offsets. Errors name the failing citation (`citations[1]: ...`, prefixed in `contribute` with the record's index and ref). When no exact match exists but the source holds the quote with different whitespace, dashes or quotation marks, the error shows that exact text and its `start_byte`. A range without a quote is read from the source and must contain non-whitespace text. Citations are stored in order as `attributes["citations"]`, each with `source_id`, `start_byte`, `end_byte` and the exact `quote`.

The record write and one `supported_by` link to each distinct cited Source, authored by the writer, commit in one transaction. Invalid or ambiguous citations raise `ValueError` and missing sources `KeyError`, before anything is written. Supplying `citations` together with a `citations` attribute raises `ValueError`. On `update`, citations replace the attribute and add any missing `supported_by` links; existing links are kept, because link deletion is not implemented. Without the argument, `attributes` behave as before and nothing is validated or linked.

`cited_by(source_id, start_byte=0, end_byte=None, limit=100)` answers the reverse question: which records cite any part of a half-open byte range of a Source. It returns a `CitationPage` of `Citation` objects, each with the citing `record`, `source_id` and the cited `start_byte` and `end_byte`, in source order. One record appears once per overlapping citation. `has_more` reports truncation; the limit is 1–1,000. The whole document is the default range, and an `end_byte` past the text means its end. Other ranges outside the text raise `ValueError`, and IDs without ingested text raise `KeyError`.

The `citations` table behind it is derived from each record's `citations` attribute. Triggers rewrite it in the same transaction as every record write, including `update`, `patch` and attributes supplied directly. Entries that do not name an ingested Source and a valid integer range within its text are kept in the attribute but not indexed.

## Contribute several records at once

```python
with RecordStore("research.sqlite") as db:
    result = db.contribute(
        author_id="alice",
        records=[
            {"ref": "f1", "kind": "Finding", "text": "Construction waits on utility approval.",
             "attributes": {"basis": "disclosed"},
             "citations": [{"source_id": source.id, "quote": "Utility approval is required"}]},
            {"ref": "q1", "kind": "Question", "text": "How long does utility approval take?"},
        ],
        links=[{"from_id": "f1", "to_id": company.id, "relation": "about"},
               {"from_id": "f1", "to_id": "q1", "relation": "raises"}],
    )
    finding = result.records["f1"]
```

`contribute` creates records and links in **one transaction** and returns a `Contribution` with `records`, a dict from each caller-chosen `ref` to its new `Record`, and `links`, the created links in input order. Records take `ref`, `kind`, `text` and optional `attributes` and `citations`, with the same meaning as in `create`. Links take `from_id`, `to_id`, `relation` and optional `explanation`; either end can name a new record by its `ref` or an existing record by ID. Record `text` and link `explanation` can name a new record as `{ref:NAME}`, which is replaced by its full ID; IDs are assigned before anything is written, so any record in the call can be named, including the record itself. A placeholder that names no record in the call raises `ValueError`, and attributes and citations are stored verbatim. `create`, `update`, `patch`, `create_link`, `update_link` and `patch_link` raise `ValueError` for `{ref:...}` text, since they have no refs to resolve. One `author_id` applies to everything written. The `supported_by` links created for citations are not repeated in `links`.

Any invalid item, missing endpoint, failed citation or constraint violation rolls back the whole contribution, so a failed call can be retried without leaving partial writes. A call that committed but whose result was lost is not deduplicated; check the graph before retrying. Refs must be unique nonblank strings that are not existing record IDs; unknown keys and batches over 1,000 records or 1,000 links raise `ValueError`.

## Split a source into passages

```python
with RecordStore("research.sqlite") as db:
    count = db.chunk_source(source.id, max_chars=2000)
    for passage in db.read_passages(source.id, limit=100):
        text = db.read_source_text(passage.source_id,
                                   start_byte=passage.start_byte, end_byte=passage.end_byte)
        print(passage.ordinal, text)
```

The dependency-free splitter caps each passage at `max_chars` Unicode characters (default 2,000), with zero overlap. Before a cut, it prefers the last paragraph break in the latter half of the window, then a line break, then whitespace; otherwise it cuts at the character limit. The final remainder stays intact. Whitespace and original text are preserved exactly. This is a character budget, not a token budget, and the splitter does not detect semantic sections or understand tables.

`chunk_source` replaces the current passage set atomically and returns its count. It processes one document in memory. `read_passages` returns `Passage` locators containing `source_id`, zero-based `ordinal`, `start_byte` and `end_byte`, in source order. Its default limit is 100 and maximum is 1,000; use `start_ordinal=last.ordinal + 1` to continue. These reads use the current set, so concurrent rechunking can change later pages. An ingested but unchunked source returns an empty tuple; missing ingested text raises `KeyError`.

Alternative chunkers can call `set_passages(source_id, ranges)` with UTF-8 byte ranges covering the entire document, in order, without gaps, overlap or empty passages. Boundaries must fall between UTF-8 characters. The built-in `premisedb.chunking.split_ranges` is a pure function independent of SQLite. Passage rows store only locators; document text stays in `source_content`. Rechunking changes passage positions, so evidence references must use Source IDs and byte ranges rather than ordinals. Section context remains a later increment.

## Search document evidence

```python
with RecordStore("research.sqlite") as db:
    segments = db.search_documents("utility approval", source_ids=[source.id], limit=5)
    for segment in segments:
        print(segment.source_id, segment.start_byte, segment.end_byte, segment.text)
    assert segments[0].text == db.read_source_text(
        source.id, start_byte=segments[0].start_byte, end_byte=segments[0].end_byte,
    )
```

Call `chunk_source` or `set_passages` first: ingestion alone does not make the document body searchable. `search_documents` returns a tuple of `SourceSegment` objects, each with a Source ID, half-open UTF-8 byte range, exact original text and optional `document_context`. Each search result covers one matching passage; the separate RSE selector can combine supplied adjacent passages into longer ranges with the same locator contract. Read the Source record separately for its description and metadata.

Query words follow the same plain-text rules as `search_records`. BM25 ranks separate header and body fields, with initial weights of 0.5 and 1.0 respectively; these are starting defaults, not tuned relevance guarantees. Ties use Source creation order, then passage ordinal. Source descriptions, attributes and research records are not included in this search. Omit `source_ids` to search all indexed documents, or supply an iterable of IDs to restrict matches before applying `limit`. An empty iterable matches nothing; duplicates have no effect. BM25 statistics still use the full passage corpus. Multiple results can come from one Source.

The default result limit is 10 and the maximum is 1,000. Invalid query, limit or filter types raise `ValueError`; filter IDs without ingested source text raise `KeyError`. Empty queries and scopes containing only unchunked documents return an empty tuple. Results have no pagination, truncation flag or total text budget yet.

SQLite triggers synchronize the passage index within the passage write transaction, including rollback on failure. The [contentless-delete FTS5 index](https://sqlite.org/fts5.html#contentless_delete_tables) stores search terms without another copy of the source text. Search selects locators and document context together, then reads those ranges from fixed canonical text; the ranges remain valid if another connection rechunks the document meanwhile. Reranking and RSE are optional subsequent steps; automatic connected research remains planned.

## Add document context

```python
with RecordStore("research.sqlite") as db:
    context = db.set_document_context(
        source.id,
        title="Supplier A — Q2 filing",
        summary="Filing excerpt describing utility approval required before construction.",
        author_id="research-agent",
        provenance={"method": "agent-authored"},
    )
    segment = db.search_documents("Supplier", source_ids=[source.id])[0]
    assert segment.document_context == context
    assert segment.text == db.read_source_text(source.id)
    print(context.header)
```

`set_document_context` stores a caller-supplied **document** title and summary once per ingested Source, separately from its record description and original text. It returns a `DocumentContext` with those fields, the Source ID, last editor, UTC `updated_at` and optional JSON provenance. Its `header` is the title, a newline and the summary. A title must be nonblank; an empty summary represents title-only context. This operation makes no model calls.

Context can be supplied before or after chunking. Writes replace current context and provenance in full; omitted provenance becomes `{}`. Triggers refresh every affected passage's search entry in the same transaction. Source text, citation ranges, passage boundaries and the Source record's timestamp stay unchanged. No earlier context is retained. Later chunks automatically use the current context.

### Generate document context with a model

```python
from premisedb import OpenRouterContextWriter, write_document_context

with RecordStore("research.sqlite") as db:
    context = write_document_context(db, source.id, writer=OpenRouterContextWriter(), author_id="research-agent")
```

`write_document_context(db, source_id, writer=..., author_id=...)` follows [dsRAG's AutoContext](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/auto_context.py). It asks for the document's title from its first ~3,000 words, then for a one-sentence summary of the form "This document is about: …" from its first ~6,000 words and the title. When the document is longer, each prompt says it holds only the start of the document. dsRAG truncates at 4,000 and 8,000 tokens; counting words avoids a tokenizer dependency. The prompts ask for a short answer and to treat the document as data, not instructions. The model calls finish before the context is stored with `set_document_context`, outside any transaction. The CLI's `ingest` calls it for every new Source when `OPENROUTER_API_KEY` is set; `ingest_source` itself makes no model calls. Provenance records `method: "model"`, the writer's model and reasoning effort, and whether the text was truncated. An empty title raises `ValueError`, and a failed call raises; neither writes anything.

`OpenRouterContextWriter(model="openai/gpt-6-luna", reasoning_effort="low", api_key=None, timeout=60)` calls OpenRouter's chat completions endpoint with `OPENROUTER_API_KEY`. `reasoning_effort=None` omits the reasoning setting. Subclass `ContextWriter` and implement `complete(prompt)` to use another provider; `describe()` supplies its provenance.

`read_document_context(source_id)` returns the current context, or `None` for an ingested Source without context. Missing IDs and records without ingested text raise `KeyError`. Non-string titles/summaries or invalid provenance raise `ValueError`; blank titles/authors violate SQL constraints and raise `sqlite3.IntegrityError`.

Search returns context as a separate field, captured with the matching index entries. A concurrent edit cannot replace it midway through a result. Header-only queries can match every passage in a document; the header provides document context and is not a quotation from the returned passage. Section summaries and embeddings remain future increments.

## Rerank document evidence

```python
from premisedb import OpenRouterReranker, RecordStore

# Reads OPENROUTER_API_KEY from the environment; alternatively pass api_key=.
reranker = OpenRouterReranker(model="voyageai/rerank-2.5")
query = "What could delay construction?"
with RecordStore("research.sqlite") as db:
    candidates = db.search_documents(query, limit=50)

results = reranker.rerank(query, candidates)
for result in results:
    print(result.score, result.segment.source_id, result.segment.text)
```

Supported `model` values are `typesafe/jev-1.13`, `cohere/rerank-4-pro` and `voyageai/rerank-2.5`. All three use your OpenRouter account. Set the environment variable in your calling process or supply the key explicitly; the library does not load `.env` files. Construction makes no requests; scoring sends the query, candidate text and document headers to the configured model.

`score(query, segments)` returns a tuple of raw 0–1 scores in input order. `rerank(query, segments)` returns every candidate as a `ScoredSegment(segment, score)`, sorted by descending score with stable ties. Both accept a sequence of `SourceSegment` objects. Text, document context and byte locators remain attached to the original objects. Empty candidates return `()` without requests; blank queries are rejected. Neither method filters low scores, truncates results, writes scores to SQLite or holds a database transaction across model calls.

Cohere and Voyage use one [native rerank request](https://openrouter.ai/docs/cookbook/evaluate-and-optimize/rag) per query, with separate document-context and passage fields encoded as JSON strings. Jev uses one [Decisions request](https://openrouter.ai/docs/guides/community/jev) per candidate, asking whether the passage supplies relevant evidence, including partial or contradictory evidence. Its rubric treats the document header as context, not evidence. Jev concurrency is bounded by `max_workers` (default 4); `timeout` defaults to 30 seconds **per request**, not for the entire reranking operation. Provider input limits apply; this implementation does not truncate or split oversized requests. Failed calls and incomplete or invalid scores raise errors; there are no automatic retries or fallback providers.

These scores share a numerical range, **not a calibrated probability scale**. Before RSE, `retrieve` applies the adapter's `transform(score)`: dsRAG's beta CDF for Cohere and Voyage, identity for Jev. Raw scores remain accessible. The adapter preserves the scoring inputs from our [Jev experiment](https://github.com/Implied-Capital/PremiseDB/pull/11) and the plain-query configuration of our [Cohere/Voyage comparison](https://github.com/Implied-Capital/PremiseDB/pull/12).

To use a direct provider, a local model or caller-supplied scores, subclass `Reranker` and implement `_score(query, segments)`. It receives a nonblank query and a nonempty tuple of segments and must return one finite 0–1 score per segment in input order. The base class supplies validation and sorting:

```python
from premisedb import Reranker

class MyReranker(Reranker):
    def _score(self, query, segments):
        return my_model.score(query, segments)  # Your implementation; same order.
```

This follows [dsRAG's abstract reranker interface](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/reranker.py), with raw scoring, score transformation and sorting kept explicit. Custom adapters inherit an identity `transform(score)` and can override it to return a finite relevance value in [0, 1]. No provider registry, serialization or database schema change is needed.

## Retrieve evidence

```python
import tiktoken  # Optional: pip install tiktoken; any token counter can be supplied.
from premisedb import OpenRouterReranker, RecordStore, retrieve

encoding = tiktoken.get_encoding("cl100k_base")
count_tokens = lambda text: len(encoding.encode(text, disallowed_special=()))
reranker = OpenRouterReranker(model="voyageai/rerank-2.5")
with RecordStore("research.sqlite") as db:
    result = retrieve(
        db, "What could delay construction?", reranker=reranker,
        count_tokens=count_tokens, candidate_limit=50,
        max_segment_tokens=2000, max_total_tokens=6000,
    )
for excerpt in result.excerpts:
    print(excerpt.segment.source_id, excerpt.segment.start_byte, excerpt.segment.end_byte)
    print(excerpt.token_count, excerpt.render())
print(result.transformed_scores)  # RSE relevance inputs, aligned with scored_passages.
```

`retrieve` searches with BM25, scores **only those search hits**, transforms their scores through the reranker adapter, and runs RSE. Optional `source_ids` constrain search. The result exposes `excerpts`, `scored_passages` with raw scores in search order, and aligned `transformed_scores` used by RSE. Provider failures and incomplete candidate scores remain errors. The candidate limit bounds retrieval recall.

As in dsRAG, an unreturned passage between hits receives the negative `irrelevant_chunk_penalty`. RSE can bridge it when the summed utility justifies the span. For example, hits at positions 10 and 13 can form one excerpt by paying two penalties for positions 11 and 12. Those intervening passages are never sent to the reranker.

The store reads intervening text for exact token accounting and excerpt assembly, only between hits close enough to fit `max_segment_passages`. No exterior neighbors are read. Gap locators are captured together before body reads and model calls; search-time document context is preserved. Rechunking that removes a hit's exact passage anchor before that capture raises `ValueError` asking the caller to repeat search. Captured byte ranges survive later rechunking. No database transaction spans a model call.

### Token budgets

Supply a deterministic `count_tokens(text)` for the model that will **consume** the excerpts; it need not be the reranker's tokenizer. There is no character-based estimate or required tokenizer dependency. The counter sees each complete rendered excerpt: document header, two separating newlines, then canonical text. `excerpt.render()` returns exactly that string, and `excerpt.token_count` records its cost. Each excerpt must fit `max_segment_tokens`; the sum of those counts must fit `max_total_tokens`. Repeated headers are charged each time. Reserve additional tokens for your own prompt, citation wrappers and separators between rendered excerpts.

The selector counts complete spans instead of adding per-passage counts, since tokenization can merge across boundaries. It also continues past an oversized prefix because appending text can reduce token counts. Counts are cached within an extraction call. Excerpts contain at most `max_segment_passages=32` passages, bounding interval work. Individual oversized passages are never cut, though a longer span can fit if its combined encoding is smaller. Empty input or insufficient evidence/budget returns no excerpts.

### Score transforms and selection

The OpenRouter adapter uses [dsRAG's score transformations](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/reranker.py):

| Adapter model | Transform before RSE |
| --- | --- |
| `cohere/rerank-4-pro` | `beta.cdf(score, 0.4, 0.4)` |
| `voyageai/rerank-2.5` | `beta.cdf(score, 0.5, 1.8)` |
| `typesafe/jev-1.13` | Identity: scores are unchanged. |

These monotone transformations reshape scores for RSE; they do not estimate benchmark-support probabilities. A small standard-library implementation evaluates the beta CDF using the [incomplete-beta continued fraction](https://dlmf.nist.gov/8.17.v); SciPy is only an optional test reference. dsRAG's adapters default to older Cohere v3 and Voyage rerank-2 models. We retain their parameters as a starting point for the newer models and report the observed results in [retrieval validation](rse-validation.md). These parameters are inherited from dsRAG rather than fitted to the validation set.

The [dsRAG-derived selector](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/rse.py) gives each passage `exp(-rank / decay_rate) * score - irrelevant_chunk_penalty`, with zero-based relevance ranks and stable input-order ties. By default, it multiplies this utility by `max(1, passage_characters / 700)`; `chunk_length_adjustment=False` disables this dsRAG heuristic. This utility weighting is separate from token accounting. It greedily selects the best nonoverlapping intervals, allowing weak intervening material when nearby evidence outweighs its cost. Endpoints must have nonnegative utility. Equal values favor the first encountered Source, then the earliest start and end. `excerpt.value` is summed utility and can exceed 1; it is not a probability.

Both `retrieve` and `extract_segments` use [dsRAG's balanced scoring defaults](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/rse.py#L152): `irrelevant_chunk_penalty=0.18`, `minimum_value=0.5`, `decay_rate=30` and `chunk_length_adjustment=True`. Pass explicit keyword overrides through `retrieve` to change selection. Score transforms never change these defaults. PremiseDB retains its token budgets, 32-passage span limit and five-excerpt limit; these are adaptations, not dsRAG's chunk-count budgets.

### Use the selector directly

```python
from premisedb import extract_segments

scores = tuple(reranker.transform(score) for score in reranker.score(query, candidates))
excerpts = extract_segments(
    candidates, scores, count_tokens=count_tokens,
)
```

`extract_segments` remains a pure, single-query function accepting aligned `SourceSegment` passages and relevance scores. It performs no database reads or score transformations. Apply `reranker.transform` once before calling it directly, as above. Supply intervening passages with `None` scores to allow bridging; each receives only the negative penalty. `retrieve` supplies these automatically. The example above can combine only contiguous candidates because no intervening passages were supplied. Returned `ExtractedSegment` objects contain `segment`, `value` and `token_count`.

Passages from one Source must not overlap or repeat, and text must match declared UTF-8 byte lengths. Missing ranges and different context snapshots are hard boundaries; separate excerpts are never stitched into a purported continuous quotation. Multiple-query allocation and automatic connected-research retrieval remain planned.

## What the graph remembers

The store supports six record types:

| Record type | What it represents | Examples |
| --- | --- | --- |
| **Entity** | Something research refers to repeatedly, with a stable identity across investigations. | A company, product, facility, commodity or regulation. |
| **Source** | Material used as evidence, with enough provenance to identify the document and locate the relevant passage. | A filing, transcript, analyst note or interview record. |
| **Finding** | A statement or conclusion from an investigation, with its supporting evidence or reasoning. | A supply relationship, business mechanism, estimate or scenario assumption. |
| **Question** | An unresolved issue worth investigating, connected to the understanding it could change. | Whether a customer can postpone contractual deliveries. |
| **Model** | A financial model whose implementation can live in an external tool. | An Excel forecast or a Python valuation model. |
| **Variable** | An identifiable input, intermediate value or output within a model, connected to its research basis. | A capacity assumption, utilization estimate or revenue forecast for a specified period. |

These are lightweight labels on a common record structure: a stable ID, a type, readable text and flexible metadata. Records connect through named links. Company and facility labels, sector-specific details and new relationship labels can develop through use. The initial vocabulary is extensible; separate tables or elaborate schemas for each type are not required.

**Findings are independently addressable.** A finding about a supply relationship connects to the supplier, customer and product, as well as its evidence. Other findings can depend on it, challenge it or revise it. Disclosed facts, agent interpretations and scenario assumptions remain distinguishable.

**Questions preserve useful uncertainty.** Identifying which unknown could change a conclusion is valuable research. A question gives later agents somewhere to contribute partial answers, attach evidence and connect a finding that resolves it. Several investigations can share the same question.

**Models connect numbers to understanding.** A Variable can link a modeled assumption to the Findings supporting it and the Questions that could change it. Calculations remain in the user's modeling tool. Links point to current records; agents reassess their meaning when assumptions change. Model runs and past assumption values are outside the current ontology.

For example, in a hypothetical investigation:

- **Entities:** Supplier A, Customer B and Product P.
- **Source S1:** A filing disclosing their supply relationship, with a reference to the relevant passage.
- **Finding F1:** Supplier A supplies Product P to Customer B. It connects to those entities and is supported by S1.
- **Finding F2:** Customer B's project delay could affect Supplier A's revenue. This inference depends on F1 and the customer's ability to postpone deliveries.
- **Question Q1:** Can Customer B postpone contractual deliveries? It concerns the supply relationship and matters to F2.

A later agent researching the contracts can answer Q1 and revisit F2. A source that challenges F1 can prompt a review of the findings that depend on it.

Records store current understanding, its last editor and a last-edited timestamp. Updates replace previous contents. Disagreements can remain explicit as separate Findings connected to one another. Later investigations build on the current graph.
