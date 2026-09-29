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

Each record has a stable ID and kind, current text, JSON attributes, the last editor's `author_id`, the creator's `created_by` (which edits never change), and a database-assigned UTC `updated_at` timestamp. Updates replace text and attributes in full; omitted attributes become `{}`. `patch(id, author_id=..., text=None, attributes=None, remove=(), citations=None)` changes only what it names. It optionally replaces the text, sets or replaces the given top-level attributes (values are replaced whole, not merged), deletes the keys in `remove` (absent keys are ignored), and resolves `citations` as `update` does. It reads and writes the current record in one transaction, so patches to different attributes are all kept, and the last committed patch wins for the same key. A patch that names nothing, or both sets and removes the same key, raises `ValueError`. Missing references raise `KeyError`. Invalid SQL-constrained fields raise `sqlite3.IntegrityError`. Returned metadata is a detached copy; changing it does not update the database.

One table stores current records and another stores current links. Writes are atomic, and the last committed update wins. There are no revision counters, stale-edit checks or historical reads. Schema format numbers only identify file layouts. During early development, incompatible formats raise `ValueError`; create a new database to use the current schema. No migrations are provided. Use one store per thread on a local filesystem; the API and schema are experimental.

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

`relation_usage()` returns a `RelationUsage` for each combination of relation, source record kind and target record kind among current links, with its link count, ordered by relation, then by count. Agents use it to reuse the relation names a workspace already has. The database does not restrict relation labels; the [research skill](../skills/premisedb-research/SKILL.md#relations) defines the standard vocabulary.

## List records

```python
with RecordStore("research.sqlite") as db:
    page = db.list_records(kind="Question", author_id="alice", limit=100)
    while page.has_more:
        page = db.list_records(kind="Question", author_id="alice", after=page.records[-1].id)
```

`list_records` returns a `RecordPage` of current records in creation order, optionally filtered by exact `kind`, `author_id` (the last editor), `created_by`, `since` (a UTC ISO date or time; keeps records created or edited then or later) and `where`, a dict of top-level attribute values that must all match. `where` values are strings, finite numbers, booleans or `None`, and match only the same JSON type: `1`, `1.0`, `True` and `"1"` are distinct, and `None` matches an explicit null, not a missing key. Pass the last returned ID as `after` to continue; `has_more` reports whether more matching records follow. The default limit is 100 and the maximum is 1,000. Invalid kinds, limits or author types raise `ValueError`, and an unknown `after` ID raises `KeyError`. Records created after a page is read appear on later pages.

`list_links(relation=None, author_id=None, created_by=None, since=None, after=None, limit=100)` pages through current links the same way and returns a `LinkPage` of `links` and `has_more`. `since` keeps links created or edited at or after the given UTC time, so an agent can see the connections others have added to the graph; `after` takes a link ID or unique prefix.

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
3. **Scoring.** A reached record scores its entry point's score times `RELATION_WEIGHTS[relation] × HOP_DISCOUNT` per hop, keeping the best path. The top `max_records` (1–100) become `records`, as `Recalled(record, score, entry_id, channels, path)`.

The result is a `Recall`:

| Field | Contents |
| --- | --- |
| `records` | The selected records, best first. |
| `evidence` | The deduplicated cited ranges of selected records with their quotes. Older citations without quotes are read from the source by byte range. |
| `issue_links` | `contradicts`, `qualifies` and `updates` links touching selected records. |
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

`read_document_context(source_id)` returns the current context, or `None` for an ingested Source without context. Missing IDs and records without ingested text raise `KeyError`. Non-string titles/summaries or invalid provenance raise `ValueError`; blank titles/authors violate SQL constraints and raise `sqlite3.IntegrityError`.

Search returns context as a separate field, captured with the matching index entries. A concurrent edit cannot replace it midway through a result. Header-only queries can match every passage in a document; the header provides document context and is not a quotation from the returned passage. Section summaries, automatic generation and embeddings remain future increments.

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
