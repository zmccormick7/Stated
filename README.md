# PremiseDB

**A shared knowledge graph that grows with every investment research investigation.**

PremiseDB gives your agents a persistent understanding of the companies, markets and economic relationships they research.

Agents contribute findings, connect them to existing knowledge, and use those connections in subsequent investigations. Evidence, interpretations and unanswered questions accumulate in a graph that any connected agent can explore and extend.

The goal is to support one agent you guide carefully or a fleet researching independently.

**Every investigation becomes a starting point for the next.**

> **Status: early implementation.** The Python store supports current records and links, lexical record and document search, caller-supplied document-level CCH, configurable reranking through OpenRouter, bounded neighbor fetching, model-specific score calibration, token-budgeted RSE, bounded connection lookups, text-only source ingestion, exact source-text reads and ordered passage storage with a basic splitter. Parsing, section context, automatic context generation, vector search, automatic graph context, citation links, the installer and agent integrations below remain planned.

The [database design specification](docs/database-spec.md) selects SQLite with FTS5 for records, graph storage and lexical search, and Faiss for vector search. It covers retrieval and concurrent contributions, building on a [source review of dsRAG and minDB](docs/dsrag-mindb-review.md). Detailed implementation choices remain proposals to validate.

Document retrieval incorporates dsRAG's **Contextual Chunk Headers** through caller-supplied document context, and **Relevant Segment Extraction** through an explicit segment-selection step. The retrieval pipeline combines search, neighbor expansion, calibrated reranking and segment extraction; section context remains planned. The graph connects evidence to accumulated research and model assumptions.

## Try the record store

From this checkout, use Python 3.12+ linked against SQLite 3.51.3+ with FTS5 enabled (including the [WAL-reset fix](https://sqlite.org/wal.html#walresetbug)). There are no third-party runtime dependencies in this slice.

```python
from premisedb import RecordStore

with RecordStore("research.sqlite") as db:
    first = db.create("Finding", text="Ramp expected in June", author_id="alice")
    second = db.update(first.id, text="Ramp delayed to September", author_id="bob")
    assert db.read(first.id) == second
    print(second.updated_at)
```

Run tests from the repository root with `python -m unittest discover -s tests -v`.

All table, index and trigger definitions live in [premisedb/schema.sql](premisedb/schema.sql).

Each record has a stable ID and kind, current text, JSON attributes, the last editor's `author_id`, and a database-assigned UTC `updated_at` timestamp. Updates replace text and attributes in full; omitted attributes become `{}`. Missing references raise `KeyError`. Invalid SQL-constrained fields raise `sqlite3.IntegrityError`. Returned metadata is a detached copy; changing it does not update the database.

One table stores current records and another stores current links. Writes are atomic, and the last committed update wins. There are no revision counters, stale-edit checks or historical reads. Schema format numbers only identify file layouts. During early development, incompatible formats raise `ValueError`; create a new database to use the current schema. No migrations are provided. Use one store per thread on a local filesystem; the API and schema are experimental.

## Connect existing records

```python
with RecordStore("research.sqlite") as db:
    finding = db.create("Finding", text="Ramp requires utility approval", author_id="alice")
    company = db.create("Entity", text="Supplier A", author_id="alice")
    link = db.create_link(finding.id, company.id, "about", author_id="alice")
    incoming = db.connections(company.id, direction="in", relation="about")
    assert incoming.links == (link,) and not incoming.has_more
```

`read_link(id)` returns current contents. `update_link` replaces its endpoints, relation and explanation in place, records the last editor and refreshes `updated_at`. Links always refer to current records. Agents reassess support when underlying understanding changes; the database does not preserve earlier assumptions or automatically determine whether a relationship is still valid.

`connections` returns links in creation order, with an optional exact relation filter. Its default limit is 100, its maximum is 1,000, and `has_more` reports omitted links. Incoming lookups preserve the stored direction. Missing IDs raise `KeyError`; duplicate links and custom relation labels are allowed. Pagination, recursive traversal and link deletion are not implemented yet.

## Search for a starting point

```python
with RecordStore("research.sqlite") as db:
    results = db.search_records("utility approval", kind="Finding", limit=10)
    for record in results:
        print(record.text, db.connections(record.id).links)
```

`search_records` returns a tuple of current `Record` objects, searching only their `text`. The query is plain text: punctuation separates words, any query word can match, and quotes or boolean words have no query-language meaning. Empty or punctuation-only queries return an empty tuple. FTS5 supplies case-insensitive token matching and [BM25 ranking](https://sqlite.org/fts5.html#the_bm25_function); ties use record creation order.

The optional `kind` filter applies before the result limit. The default limit is 10 and the maximum is 1,000; results have no pagination or truncation flag yet. Invalid kinds, limits or non-string queries raise `ValueError`. SQLite triggers keep the index synchronized in the same transaction as each record write, so updated text replaces earlier searchable text immediately after commit.

This uses schema format 9. Databases created by earlier implementations must be recreated; no migration is performed.

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

`ingest_source` accepts already extracted text and returns a new Source record. Its `text` is the short description searched by `search_records`; the document body stays in a separate `source_content` table. The Source, its search entry and its UTF-8 body commit together. Each call creates a new Source identity, even for identical text. Caller-supplied provenance belongs in `attributes`.

The body and its SHA-256 are stored in SQLite for this first ingestion slice. Text is preserved exactly, including whitespace, line endings and Unicode normalization. `read_source_text(id)` returns the full text; optional offsets select a half-open UTF-8 byte range `[start_byte, end_byte)`, fetching only that range. Invalid ranges or offsets inside a character raise `ValueError`. Missing IDs and records without ingested text raise `KeyError`.

Source descriptions and metadata remain editable. Captured text stays fixed so future citation offsets keep their meaning; ingest a separate Source for different text. This is evidence identity, not record edit history. Parsing, original-file storage and section context will be added in later increments. Ingestion currently holds one document's text in memory and makes no model calls.

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

These scores share a numerical range, **not a calibrated probability scale**. Do not assume that a threshold such as 0.5 means the same thing across models. The integrated retrieval path below applies a model-specific calibration fitted on financial support labels; these starting profiles remain specific to that workload. The initial adapter preserves the scoring inputs from our [Jev experiment](https://github.com/Implied-Capital/PremiseDB/pull/11) and the plain-query configuration of our [Cohere/Voyage comparison](https://github.com/Implied-Capital/PremiseDB/pull/12).

To use a direct provider, a local model or caller-supplied scores, subclass `Reranker` and implement `_score(query, segments)`. It receives a nonblank query and a nonempty tuple of segments and must return one finite 0–1 score per segment in input order. The base class supplies validation and sorting:

```python
from premisedb import Reranker

class MyReranker(Reranker):
    def _score(self, query, segments):
        return my_model.score(query, segments)  # Your implementation; same order.
```

This follows [dsRAG's abstract reranker interface](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/reranker.py), with scoring separated from sorting so segment extraction can consume all candidate scores. Configuration lives in ordinary Python objects for now; no provider registry, serialization or database schema change is needed.

## Retrieve calibrated evidence

```python
import tiktoken  # Optional: pip install tiktoken; any token counter can be supplied.
from premisedb import OpenRouterReranker, RecordStore, retrieve

encoding = tiktoken.get_encoding("cl100k_base")
count_tokens = lambda text: len(encoding.encode(text, disallowed_special=()))
reranker = OpenRouterReranker(model="voyageai/rerank-2.5")
with RecordStore("research.sqlite") as db:
    result = retrieve(
        db, "What could delay construction?", reranker=reranker,
        count_tokens=count_tokens, candidate_limit=50, neighbor_radius=2,
        max_passages=250, max_segment_tokens=2000, max_total_tokens=6000,
    )
for excerpt in result.excerpts:
    print(excerpt.segment.source_id, excerpt.segment.start_byte, excerpt.segment.end_byte)
    print(excerpt.token_count, excerpt.render())
print(result.calibration.name, result.neighbors_truncated)
```

`retrieve` searches with BM25, expands around the matching passages, scores **every fetched passage**, applies the selected model's calibration, and runs RSE. Optional `source_ids` constrain search and expansion stays within those Sources. Model calls occur after the database reads, outside SQLite transactions. The result exposes `excerpts`, `scored_passages` with raw scores in expansion order, the actual `calibration` object, and `neighbors_truncated`. Provider failures remain errors; missing scores are not silently replaced. The search limit bounds seed recall, and a false truncation flag only means the requested neighbor windows fit.

`expand_passages(seeds, radius=2, limit=250)` is also available on the store. It retains all distinct seeds first, then the nearest neighbors, breaking ties by seed order and source position. Overlapping windows are deduplicated before text reads. Radius must be 0–10 and the passage limit 1–1,000; a limit below the number of distinct seeds is rejected. The return value contains `segments` and `has_more`. A single SQL query captures current locators and document context; it refreshes seed context before scoring. Rechunking that removes an exact seed anchor raises `ValueError` asking the caller to repeat search. Once captured, byte ranges and context remain valid snapshots through later model calls and database edits.

### Token budgets

Supply a deterministic `count_tokens(text)` for the model that will **consume** the excerpts; it need not be the reranker's tokenizer. There is no character-based estimate or required tokenizer dependency. The counter sees each complete rendered excerpt: document header, two separating newlines, then canonical text. `excerpt.render()` returns exactly that string, and `excerpt.token_count` records its cost. Each excerpt must fit `max_segment_tokens`; the sum of those counts must fit `max_total_tokens`. Repeated headers are charged each time. Reserve additional tokens for your own prompt, citation wrappers and separators between rendered excerpts.

The selector counts complete spans instead of adding per-passage counts, since tokenization can merge across boundaries. It also continues past an oversized prefix because appending text can reduce token counts. Counts are cached within an extraction call. Excerpts contain at most `max_segment_passages=32` passages, bounding interval work. Individual oversized passages are never cut, though a longer span can fit if its combined encoding is smaller. Empty input or insufficient evidence/budget returns no excerpts.

### Calibration and selection

`calibration_for(model)` supplies a named `ScoreCalibration` for Jev, Cohere or Voyage. Each applies `sigmoid(slope * logit(raw_score) + intercept)` using separately fitted coefficients; scores are clipped to `[1e-6, 1 - 1e-6]` before taking logits. Raw provider scores remain accessible. Calibration and selection parameters live in ordinary Python objects, and `calibration=` can override the default. Its `model` must match `reranker.model`; custom rerankers must identify their model and supply a matching calibration. Unknown models never silently receive another model's profile.

The bundled `finqa-support-v1` profiles were fitted on 20 financial questions, with selection settings chosen on 20 validation questions and checked on 20 additional test questions. They estimate the dataset's **annotated supporting-passage** label, not truth or universal financial relevance. Sparse labels may omit useful context. Score error improved for all three models on the test set, but excerpt coverage and length still trade off; Cohere lost support on one test question while returning less text. See [calibration results and reproduction instructions](docs/rse-validation.md) before applying these starting profiles to a different corpus, candidate mix, document context or provider version.

The [dsRAG-derived selector](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/rse.py) gives each passage `exp(-rank / decay_rate) * score - irrelevant_chunk_penalty`, with zero-based relevance ranks and stable input-order ties. By default, it multiplies this utility by `max(1, passage_characters / 700)`; `chunk_length_adjustment=False` disables this dsRAG heuristic. This utility weighting is separate from token accounting. It greedily selects the best nonoverlapping intervals, allowing weak intervening material when nearby evidence outweighs its cost. Endpoints must have nonnegative utility. Equal values favor the first encountered Source, then the earliest start and end. `excerpt.value` is summed utility and can exceed 1; it is not a probability.

The integrated path uses the profile's penalty and minimum utility (currently 0.001 and 0.01), with `decay_rate=30` and `max_segments=5`. These recall-oriented settings were fixed before the final test. Pass keyword overrides through `retrieve` to change selection without changing the fitted score mapping. An explicit caller-supplied `minimum_value` or `irrelevant_chunk_penalty` takes precedence.

### Use the selector directly

```python
from premisedb import calibration_for, extract_segments

scores = reranker.score(query, candidates)
profile = calibration_for(reranker.model)
excerpts = extract_segments(
    candidates, profile.apply(scores), count_tokens=count_tokens,
    irrelevant_chunk_penalty=profile.irrelevant_chunk_penalty,
    minimum_value=profile.minimum_value,
)
```

`extract_segments` remains a pure, single-query function accepting aligned `SourceSegment` passages and scores. Unlike `retrieve`, it does not fetch neighbors or calibrate scores itself. A supplied but unscored passage takes `None` and receives only the negative penalty. Its standalone penalty/minimum defaults remain 0.2/0.5 for the original reference configuration; use the profile's values with calibrated scores. Returned `ExtractedSegment` objects contain `segment`, `value` and `token_count`.

Passages from one Source must not overlap or repeat, and text must match declared UTF-8 byte lengths. Missing ranges and different context snapshots are hard boundaries; separate excerpts are never stitched into a purported continuous quotation. Multiple-query allocation and automatic connected-research retrieval remain planned.

## What can you do with PremiseDB?

The experience we are building lets you ask your agent questions like:

- **“Get me up to speed on this company.”** Bring together earlier research, business drivers, important relationships and unresolved issues.
- **“Who else could this affect?”** Follow connections through customers, suppliers, products and constraints.
- **“What supports this assumption?”** Trace a conclusion back through the reasoning and evidence behind it.
- **“What does this disclosure change?”** Identify earlier claims and dependent conclusions that warrant revisiting.
- **“What should we investigate next?”** Find gaps that matter to the question you’re trying to answer.

As the graph develops, agents can connect findings across investigations—even when different agents performed the original research.

## Intended developer experience

Create a research workspace:

```text
npx premisedb init my-research
```

**This is a proposed command, not an implemented or published installer.** It is intended to prepare the workspace and add research skills for Codex and Claude Code.

Open the workspace in your preferred agent and start researching:

> Research Micron’s exposure to AI infrastructure spending. Use the filings and notes in this folder. Build on any existing knowledge, preserve the supporting evidence, and record the important unresolved questions.

Your agent consults the graph, performs the research using its available tools, and contributes what it learns.

Later, open a fresh session and ask:

> A major customer is delaying deployment. Use our existing research to investigate what that could mean for Micron. Identify which relationships and assumptions matter before estimating an impact.

The new session can retrieve the earlier findings, follow their connections and extend the investigation.

PremiseDB works through the agent you already use. Your harness conducts the investigation, while PremiseDB can use configured models to prepare documents, generate contextual headers, embed content and rank evidence. Model providers are replaceable, and existing pipelines can supply their own outputs.

## Research together, remember together

Suppose three agents work on separate questions:

- One investigates a supplier’s production capacity.
- Another studies its customers’ expansion plans.
- A third researches infrastructure constraints affecting those customers.

Their findings enter the same graph, connected through shared entities and evidence.

A subsequent agent investigating a production delay can draw on all three contributions. It can inspect where the findings agree, where they conflict and which connections remain uncertain.

We are designing the same workflow for investigations performed sequentially over several months or concurrently across many agents. Concurrent operation and performance at that scale have not yet been implemented or demonstrated.

## What the graph remembers

The proposed model has six record types:

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

## Bring your own agents

The common interface we are building lets agents:

- **Recall** relevant knowledge and follow its connections.
- **Contribute** findings, evidence and questions.
- **Revise** existing understanding in place.

The planned skills will guide interactive research in Codex and Claude Code. Custom agent workflows will use the same operations against a shared workspace.

Choose your own models, research tools and orchestration. The research graph should remain available across sessions and agent harnesses.

## Make it yours

Our intended starting point is an example workspace you can explore immediately and a way to build a graph from your own research.

Extend the skills for your sector. Add workflows for company coverage, meeting preparation or thesis review. Connect the graph to your existing research process.

Repository contributors and agents should use this README as the product direction and [AGENTS.md](AGENTS.md) for implementation guidance. Python is the implementation language; SQLite and Faiss are the selected storage/search engines. Model providers and integration details remain open.

**Your agents do the research. PremiseDB makes their accumulated knowledge available to whatever comes next.**
