# PremiseDB

**A shared knowledge graph that grows with every investment research investigation.**

PremiseDB gives your agents a persistent understanding of the companies, markets and economic relationships they research.

Agents contribute findings, connect them to existing knowledge, and use those connections in subsequent investigations. Evidence, interpretations and unanswered questions accumulate in a graph that any connected agent can explore and extend.

The goal is to support one agent you guide carefully or a fleet researching independently.

**Every investigation becomes a starting point for the next.**

> **Status: early implementation.** The Python store supports current records and links, lexical record search, bounded connection lookups, text-only source ingestion, exact source-text reads and ordered passage storage with a basic splitter. Parsing, passage search, semantic search, automatic graph context, citation links, the installer and agent integrations below remain planned.

The [database design specification](docs/database-spec.md) selects SQLite with FTS5 for records, graph storage and lexical search, and Faiss for vector search. It covers retrieval and concurrent contributions, building on a [source review of dsRAG and minDB](docs/dsrag-mindb-review.md). Detailed implementation choices remain proposals to validate.

Document retrieval will incorporate dsRAG's **Contextual Chunk Headers** to give each chunk its document and section context, and **Relevant Segment Extraction** to return useful variable-length evidence spans. The graph connects that evidence to accumulated research and model assumptions.

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

This uses schema format 7. Databases created by earlier implementations must be recreated; no migration is performed.

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

Source descriptions and metadata remain editable. Captured text stays fixed so future citation offsets keep their meaning; ingest a separate Source for different text. This is evidence identity, not record edit history. Parsing, original-file storage, CCH, RSE and passage search will be added in later increments. Ingestion currently holds one document's text in memory and makes no model calls.

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

Alternative chunkers can call `set_passages(source_id, ranges)` with UTF-8 byte ranges covering the entire document, in order, without gaps, overlap or empty passages. Boundaries must fall between UTF-8 characters. The built-in `premisedb.chunking.split_ranges` is a pure function independent of SQLite. Passage rows store only locators; document text stays in `source_content`. Rechunking changes passage positions, so evidence references must use Source IDs and byte ranges rather than ordinals. Passage indexing and document/section context for CCH remain later increments.

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
