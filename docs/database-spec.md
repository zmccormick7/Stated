# PremiseDB database: proposed v0 design

**Status: design specification, not an implementation or a performance claim.** SQLite and Faiss are the selected storage/search engines. This draft incorporates the agreed research ontology and workload; detailed schema, retrieval defaults and operating parameters below remain proposals to validate. The [dsRAG and minDB review](dsrag-mindb-review.md) explains the source ideas.

## 1. What the database owns

PremiseDB stores evolving research and retrieves useful connected context. It owns record identity, revisions, evidence locators, relationships, atomic contributions and the query engine that combines document search with graph exploration.

**Contextual Chunk Headers (CCH) and Relevant Segment Extraction (RSE) from dsRAG are required parts of document retrieval in v0.** Headers provide document and section context when ranking chunks; RSE turns scored chunks into variable-length evidence segments. The project author's experience with these techniques on financial documents, including FinanceBench, motivates starting with both. Graph retrieval builds on this document-retrieval foundation. PremiseDB's implementation and performance still need to be demonstrated.

Agents conduct investigations through their existing harnesses. PremiseDB owns the document-processing and retrieval pipeline and can call configured local or hosted models for semantic sectioning, contextual descriptions, embeddings and relevance scoring. Caller-supplied outputs remain supported for existing pipelines and testing. Financial calculations remain in Excel, Python or another modeling tool.

The standard document-retrieval profile uses contextualized embeddings, lexical retrieval, semantic reranking and RSE. Provider choice is configuration. A text query can be embedded and reranked by PremiseDB without requiring the caller to perform those steps. Lexical-only retrieval is an explicit alternative profile, not a restriction on the standard pipeline; exact reads and graph operations do not need to invoke a model.

The six research record types are **Entity, Source, Finding, Question, Model and Variable**. Relationships are addressable graph infrastructure. Document chunks, database transactions and index generations are storage infrastructure. There is no financial **Run** record in v0.

The main product contract is: retrieve relevant prior understanding, expose useful connections, and preserve the changes contributed by the next investigation.

## 2. Workload and sizing assumptions

One shared research workspace for a long-short desk covering approximately 100 companies in depth:

| Item | Working size | Basis |
| --- | ---: | --- |
| Documents / Source records | 1 million | Agreed representative corpus; one Source per document initially. |
| Pages | 10 million | Assumed average of 10 pages/document. |
| Findings | 500,000 | Agreed reduction from the earlier estimate. |
| Connections | 2.5 million | Agreed working size; measure separately if citations push this higher. |
| Entities, Questions, Models, Variables | Unspecified | Include related companies, products and facilities beyond the 100 covered companies. |
| Search passages | 15 million | Illustrative 750 tokens/page and 500 tokens/passage, without overlap. |
| Extracted text | About 30 GB | 7.5 billion tokens at an illustrative 4 bytes/token. |
| Raw passage vectors | 46.08 GB at 768 dimensions; 92.16 GB at 1,536 | Float32 payload only. |

These are decimal GB. Chunking, tables, document versions, research-record embeddings, indexes, history and storage overhead add to these figures. Twenty-percent overlap would raise the passage estimate to approximately 18.75 million. Original artifacts need separate sizing: an assumed 1 MB/document would mean 1 TB, but the actual corpus must determine that estimate.

Use a shared graph and search corpus. Company membership is many-to-many; it is a filter and discovery signal, not a physical database boundary. Benchmark skewed company hubs and shared suppliers rather than evenly isolated company graphs.

Proposed concurrency scenario: 100 active agents each retrieving once per 10 seconds and contributing one batch/minute. That is 10 retrieval requests/second and approximately 1.7 contribution transactions/second, with a separate 10x burst test. These are test inputs, not known desk behavior or proven capacity. Bulk ingestion is a distinct workload.

## 3. Storage stack and external dependencies

**One authoritative SQLite database, immutable source artifacts on disk, and derived Faiss indexes.** PremiseDB's custom layer supplies the graph operations and retrieval planner.

SQLite and Faiss are embedded libraries, not hosted services. They are the two core storage/search dependencies. Require a SQLite build with FTS5 enabled for lexical search; FTS5 supplies BM25 ranking within SQLite. Model providers are a separate configuration concern. [SQLite FTS5](https://sqlite.org/fts5.html).

| Component | Contents and responsibility |
| --- | --- |
| `premisedb.sqlite` | Record and relationship revisions, current projections, source manifests, passages, embedding payloads, exact indexes and durable change sequence. |
| SQLite FTS5 tables | Rebuildable lexical indexes for research records and passages; updated in the same transaction as their indexed content. |
| `objects/` | Immutable, content-addressed originals and canonical extracted-text artifacts. SQLite references published objects. |
| `indexes/` | Faiss generations and their manifests, rebuilt from retained embedding payloads. Separate document and research-record candidate pools. |

Proposed per-workspace layout:

```text
workspace/
  premisedb.sqlite
  objects/<content-hash>
  indexes/<embedding-space>/<research-or-passages>/<generation>.faiss
```

SQLite manages its WAL/SHM sidecars. Temporary ingestion and index-build files are unpublished until their SQLite manifests are committed. The local filesystem is the initial artifact store; a hosted object store is not required.

| Other capability | Boundary |
| --- | --- |
| Context generation and query rewriting | Configurable generative-model adapter; can use a hosted API or local model. |
| Embeddings | Configurable embedding adapter, with explicit model/configuration identity and dimensions. |
| Text reranking | Configurable relevance-scoring adapter, separate from Faiss vector refinement. |
| Document parsing and tokenization | Ordinary libraries/adapters; concrete packages remain to be selected. OCR or multimodal providers can be added for documents needing them. |
| Graph traversal, CCH assembly, RSE, query planning and context selection | PremiseDB code, incorporating dsRAG's CCH/RSE components and minDB's retrieval approach. |
| Work queues, index manifests and invalidation tracking | SQLite tables plus PremiseDB workers in the same deployment. |

These are capability boundaries, not a requirement for a separate vendor per capability. The generative-model adapter can serve context generation and query rewriting. No additional graph database, vector database, search server, cache service or message broker is required by this design. Runtime libraries, model clients and parsers mean that SQLite and Faiss will not literally be the only software dependencies.

This keeps graph changes and their exact indexes in one transaction and gives us an initial lexical engine without building an inverted index. FTS5 can index an external content table without maintaining another text copy inside the FTS table; the application must maintain consistency. [FTS5 documentation](https://sqlite.org/fts5.html#external_content_tables).

SQLite WAL permits concurrent readers with one writer. A shared desk uses a single owner service, a short write queue and bounded transactions; agents do not open the file over a network filesystem. Readers use short snapshots. Index training must not hold a database read transaction open for hours. Pin a supported SQLite build containing the documented WAL-reset fix. [SQLite WAL behavior and fix](https://sqlite.org/wal.html).

**SQLite is selected**, including full vectors in a separate BLOB table. Measure batched candidate-vector fetches, ingestion and graph-write interference before loading the full corpus. Revisit another store only if these measurements expose a specific bottleneck. Do not add LMDB speculatively or introduce two independent authoritative stores with best-effort dual writes.

This proposal does not commit to a runtime, transport, general backend abstraction or distributed database. These can be chosen after the vertical slice establishes the contracts.

## 4. Logical data model

### Records and revisions

All records have a stable opaque ID and a kind. Revisions are immutable and monotonically numbered within a record. Each revision contains:

```text
id, revision, kind, title, text, attributes
author_id, recorded_at, commit_seq
applies_from?, applies_to?, status
```

`recorded_at` is assigned by PremiseDB; agents cannot backdate when the workspace learned something. Effective dates describe the subject matter and may be unknown. Additional observation dates belong in provenance. Times use UTC; application periods retain their explicit financial/calendar meaning. Status values are validated by kind, rather than one universal list. Flexible attributes use validated JSON, with a small typed core:

| Kind | Core attributes |
| --- | --- |
| Entity | Entity category, aliases and scoped external identifiers. A ticker alone is not permanent identity. |
| Source | Origin, publisher, publication time if known, and content-version references. A later capture never rewrites earlier captured content. |
| Finding | Claim text and basis: disclosed fact, interpretation, estimate or scenario assumption. Evidence and dependencies are links. |
| Question | Question text and state such as open/resolved; answering evidence remains linked and inspectable. |
| Model | Purpose, implementation reference and covered entities. The reference may identify a workbook or code artifact. |
| Variable | Owning Model, definition, input/intermediate/output role, units, period, optional scenario, current value, external binding and last synchronization time. |

Start with one Variable per model/metric/period/scenario coordinate. Use decimal strings for numeric values, explicit currencies and scales, and `null` for unknown values. An absent value is not zero. A binding such as a workbook named range is an external location, not the Variable's identity. Each synchronized change creates a revision. The model engine remains responsible for coherent recalculation; PremiseDB records the synchronization provenance and can flag stale values.

Entity identifiers with a known unique namespace receive uniqueness constraints. Names and aliases can have multiple matches. Merges are explicit revisions/redirects with history, never silent fuzzy-name deduplication.

### Relationships

A relationship has its own stable ID and revision, plus:

```text
from_id, to_id, relation
from_revision?, to_revision?
explanation?, provenance, author_id, recorded_at, commit_seq
applies_from?, applies_to?, status
source_locator?  # only when the target is a Source
```

The stored direction always has one meaning. `A depends_on B` means A relies on B; reverse traversal does not reverse that meaning. Relation labels are extensible. A small built-in vocabulary supplies direction descriptions and retrieval behavior; unknown labels remain traversable without acquiring invented semantics.

| Example | Meaning |
| --- | --- |
| Finding → `about` → Entity | Subject of the claim; useful for discovery. |
| Finding → `supported_by` → Source + locator | Evidence for the stated claim. |
| Finding → `depends_on` → Finding | A conclusion relies on another conclusion or premise. |
| Finding → `contradicts` / `qualifies` → Finding | An explicit disagreement or limitation. |
| Question → `matters_for` → Finding or Variable | Resolving the question could change this understanding or assumption. |
| Question → `answered_by` → Finding | A recorded answer, without deleting the question's history. |
| Variable → `supported_by` → Finding | Research supporting a particular modeled value. |
| Variable → `calculated_from` → Variable | A declared computational dependency in the external model. |

Supporting links pin the claim or Variable revision being supported and the evidence revision/content being relied on. A stable subject link such as `about` may follow current identity. If a pinned endpoint changes, retrieval reports that the connection concerns an earlier revision; it never silently transfers support to the new value.

Business assertions such as “Supplier A supplies Customer B” are Findings linked to Entities, so questions and counterevidence attach to the assertion. Shared entities or shared documents are discovery routes, not proof of an economic mechanism.

Variable ownership lives in the Variable's typed attributes and an indexed projection; avoid a second independently editable `belongs_to` edge. Other substantive links use the common relationship mechanism.

### Source versions and citations

A content version identifies a Source's captured artifact and a specific canonical text rendition. Record original and text hashes, extraction method/version and when this version entered the workspace. Re-extraction creates a new content version even if the original PDF bytes are unchanged.

A text citation is `(source_id, content_version_id, start_byte, end_byte)`, using a half-open UTF-8 range in the immutable canonical text. Validate boundaries and optionally retain a quoted-text hash. Pages, section paths and table/cell references help navigation; they supplement the canonical locator. Non-text evidence uses an explicit artifact/page/region locator rather than fabricated text offsets.

Search passages are derived partitions of a content version, identified by `(chunk_set_id, ordinal)`. Rechunking creates a new chunk set. Citations do not depend on chunk ordinals, so changing chunk sizes does not break evidence links. Materialized passage text may duplicate canonical text for efficient search; include that copy in disk sizing.

### Contextual Chunk Headers

Document and section context are stored explicitly and used to construct each chunk's search representation, following dsRAG's [contextual header implementation](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/auto_context.py#L136):

```text
Document context: document title + document summary
Section context: section title + section summary

Original chunk text
```

For example, a chunk containing a debt-maturity table can carry the issuer, filing period and debt-note context even when those details occur elsewhere in the document. CCH summarizes the containing document and section, not the chunk. Source descriptions and additional background from the research graph are separate fields with separate provenance.

Store document title/summary once per document-context revision and section title/summary once per section-context revision, with author and provenance. Sections have ordered source ranges and may have parent sections. Each passage references its section and the applicable context revisions. These are source-processing structures, not additional research record types.

PremiseDB prepares descriptive titles and summaries during ingestion using its configured context model, or accepts them from an agent or another ingestion pipeline. Semantic sectioning can use a configured model when source structure is insufficient. Record the producer, input content hash and model/prompt configuration with generated context; PremiseDB renders the resulting header deterministically. A document can be ingested with only known titles while enrichment is pending; mark its context as incomplete rather than treating missing summaries as equivalent to a fully enriched document.

Embed **header plus chunk text**, and use the same contextual representation for text reranking. Lexical search indexes header and body as separate fields so their weighting can be tuned. A search representation identifies the content version, chunk set, document/section context revisions and header-template version. Changing the rendered header creates affected search representations without changing the underlying source text or breaking citations. Reuse an embedding only when the exact model input and embedding configuration are unchanged. Publish a new representation generation after bounded staging; expose missing embeddings explicitly.

Returned evidence separates original text from contextual descriptions. It includes document context and applicable section references without inserting a repeated header between every chunk. A summary is enrichment, not a quotable passage from the source. Header storage and indexed-text duplication must be included in capacity measurements.

### Graph-enriched context and dependency tracking

Context construction may consult existing Entities and Findings to explain principal products, segments, facilities and business roles discussed in a document. Store that background separately from the document/section summary, and construct it at the document or section level. It does not become an assertion made by the source or independent confirmation of its supporting Finding. A short Finding can be embedded whole with relevant business background; it does not need an invented parent document.

Persist two kinds of input dependency for each immutable context revision:

- **Exact inputs:** the record/link IDs and revisions supplied to context generation, conservatively including all supplied inputs. Optional statement-level attribution can be more selective.
- **Research scopes:** the entity IDs, relation filters and applicable-time filters whose research was consulted, plus the graph sequence observed. These catch new relevant contributions that did not exist among the exact inputs.

A record revision change, a new qualifying/challenging link, or a change within a watched scope marks affected contexts for reconsideration. Scope matching uses indexed entities and relation labels; it does not claim to understand semantic disagreement. Writes append change events atomically; a worker coalesces affected contexts into refresh jobs. Unlinked information outside recorded scopes cannot trigger this mechanism automatically.

Refresh jobs compare the newly rendered context with the old context and update provenance even when the text is unchanged. Re-embed only when the exact embedding input changes. Mark old representations as pending refresh while they remain usable; an explicit strict-freshness profile can exclude them. Capture consulted scope sequence tokens before generation and check them at publication so intervening changes enqueue reconsideration rather than silently publishing context as current. This is dependency tracking and invalidation, not another research record type.

## 5. Physical tables and exact indexes

This is a logical table layout, not final migration SQL. Public IDs map to compact integer keys used in joins and Faiss; internal keys are never recycled.

| Table/group | Key and principal contents |
| --- | --- |
| `records`, `record_revisions` | Stable identity/current revision; immutable `(record_key, revision)` payloads and commit sequence. |
| `links`, `link_revisions` | Stable relationship/current revision; immutable endpoints, pins, relation, locator and provenance. |
| `record_current`, `link_current` | Transactionally maintained current projections for indexed filters and traversal. Rebuildable from revisions. |
| `entity_identifiers`, `entity_aliases` | Exact normalized identity lookup; ambiguous aliases return multiple candidates. |
| `record_entities`, `model_variables` | Exact, rebuildable membership projections from explicit subject links and Variable ownership. |
| `source_content`, `chunk_sets`, `passages` | Immutable content manifests, published chunk-set selection and ordered passage locators/text. |
| `document_context`, `sections`, `section_context` | Versioned document/section titles and summaries, source-aligned section ranges and authorship; shared by the passages whose contextual headers use them. |
| `record_context` | Optional versioned business background for searchable research records, separate from the record's own claim text. |
| `context_inputs`, `context_scopes` | Exact graph-input revisions and watched research scopes for each context revision, with reverse dependency lookups. |
| `citation_ranges` | Projection of source-evidence links, indexed by content version and range start. |
| `search_items` | Stable integer search IDs, target revision or passage, separate header/body search fields, context-revision dependencies, representation version and availability state. |
| `embedding_spaces`, `embeddings` | Model/configuration identity, dimension, normalization and metric; `(space_id, search_item_id)` full vector payload. |
| `commits`, `changes`, `requests` | Monotonic commit sequence, durable index-update events and idempotency receipts. |
| `processing_jobs` | Durable ingestion/enrichment work, keyed by input version and processing configuration, with status and result references. |
| `index_generations` | Published generation manifests, embedding space, represented sequence, file checksums and processing state. |

Essential indexes:

- Outgoing and incoming adjacency: `(from_key, relation, link_key)` and `(to_key, relation, link_key)`, plus endpoint-only scans for arbitrary relation filters.
- Record kind/status and subject membership in both directions. Index dates used in actual filters rather than every possible JSON field.
- Source passage order: `(content_version, chunk_set, ordinal)`, and range starts for locating overlapping passages.
- Context dependencies: document/section context revision to affected passage search items, for batched header reconstruction and invalidation.
- Graph input dependencies: `(input_kind, input_key, context_key)`; watched scopes: `(entity_key, relation, context_key)`. Context keys also identify the context owner/type and revision. Broad scopes explicitly match all relations rather than silently missing new labels.
- Reverse citations: `(content_version, start_byte)` followed by exact overlap checking with `citation_start < query_end AND citation_end > query_start`.
- Revision history: `(record_key, commit_seq)` and `(link_key, commit_seq)` for exact historical reads.
- Search-item revision identity and embeddings by integer search ID, allowing batched fetches without carrying full metadata in Faiss.

Do not expand millions of passages into semantic graph nodes. The reverse-citation index bridges source search and the research graph. A hit on a source range can reveal the Findings citing that range, then their dependent Variables or Questions. Merely citing unrelated passages in the same report does not create that bridge.

### Column and representation rules

Use typed columns for stable identity, kind, revision, endpoints, dates, source ranges, state and fields used in frequent filters. Keep extensible domain attributes and provenance in validated JSON `TEXT`. Do not put adjacency, citation ranges or identity lookup exclusively inside JSON. Enable foreign-key enforcement on every connection, and validate kind-specific references in the writer.

Use signed 64-bit integer keys internally and stable public IDs externally. Each revision has a composite unique key such as `(record_key, revision)`; endpoint pins reference those keys. Current projections are derived and maintained in the same transaction. Keys used as Faiss labels are never recycled, including after deletion.

The main physical access patterns are:

```text
link_current:       from_key + relation → outgoing links
                    to_key + relation → incoming links
passages:           content_version + chunk_set + ordinal → ordered ranges
citation_ranges:    content_version + start_byte → candidate overlaps
context_inputs:     input_kind + input_key → dependent context revisions
context_scopes:     entity_key + relation → contexts watching that research
embeddings:         space_id + search_item_id → full vector payload
```

The citation index narrows by source version and range start; the query checks range ends to establish actual overlap. It is not a general interval-tree index. Source/passage lookup supplies eligible IDs and neighbors; the retrieval executor must separately enforce those restrictions in vector search. For reads at a captured sequence, resolve the latest applicable record/link revision at or before that sequence before interpreting its endpoints, pins or status. Historical adjacency lookup indexes must cover both endpoint directions; current projections alone cannot answer those reads.

Keep research and passage lexical candidate pools separate, each with an external-content FTS5 table indexing rendered header and body fields. Its `rowid` is the corresponding integer search-item ID. Derived active-content projections, FTS updates and their visibility changes commit together. Raw passage text remains separately accessible without the header. BM25 and vector scores are combined through declared ranking logic, not treated as directly comparable numbers.

Store full normalized vectors as explicitly encoded little-endian float32 BLOBs, keyed by `(space_id, search_item_id)`, in a table separate from text and graph rows. Validate dimension, finite values and nonzero norm before accepting vectors. The embedding-space definition records model/configuration, dimension, metric and normalization. A vector's byte length must equal `4 × dimension`; query vectors obey the same contract. Fetch candidate vectors in bounded batches for refinement. Payload storage and page amplification are part of the SQLite benchmark.

Faiss stores integer search-item IDs plus its vector/index structures. SQLite owns the mapping to record revisions or source passages, all filtering metadata, and the retained full vectors. Document and research indexes are separate per embedding space. A missing or corrupt Faiss file can therefore be rebuilt without reconstructing the research graph or regenerating embeddings.

## 6. Contribution and consistency contract

`contribute` accepts an idempotency key, author identity, new records, revisions, relationships and expected revisions for changed objects. The same key and payload return the original receipt; reusing the key with a different payload is an error.

One transaction validates references and revision pins, performs compare-and-swap checks, writes revisions and current projections, updates lexical content, and appends durable change events. It returns a commit sequence and the committed IDs/revisions. The batch is atomic. Conflicting edits return the current revisions so the caller can reconcile; no silent last-write-wins. Two agents adding distinct Findings can both succeed without contending on a shared company record.

For the shared-agent deployment, one owner process per workspace holds the serving indexes and coordinates writes. Use WAL, foreign keys, an explicit busy timeout and `synchronous=FULL` as the initial durability configuration. A bounded queue feeds short `BEGIN IMMEDIATE` write transactions. Read connections are pooled; network/model calls and index training stay outside SQLite transactions. A single-agent process can embed the same engine without a separate service. Agents sharing a workspace do not each load independent mutable copies of its Faiss indexes.

Large source ingestion stages immutable artifacts and passages in bounded batches. Files are durably written before their manifests are published. A short publication transaction exposes a complete content version/chunk set; staged rows remain ineligible for retrieval. A crash may leave unreferenced staged data for later cleanup, but must not publish a citation to a missing artifact. Cleanup respects active staging leases and retained history.

Graph and lexical reads after a committed contribution are immediately available. Embedding availability is separate: records awaiting embedding generation remain available to lexical search and graph operations, with semantic coverage reported. A revised search representation gets a new search-item ID; the previous embedding never silently represents the new text.

Model-backed ingestion jobs run outside database write transactions. Bind each job to its input content/context revisions and provider configuration, persist status and successful outputs, and publish only if those inputs are still applicable. Retried work reuses accepted outputs; failed or superseded work cannot overwrite newer context. Batch and cache model requests where supported, with bounded concurrency and explicit cost/latency controls. These are processing jobs, not financial model Run records.

SQLite-backed workers claim jobs with a lease and attempt number in a short transaction. Completion checks that the lease/attempt and input revisions still match; a late worker cannot publish over a newer attempt. Expired claims may be retried, so result publication is idempotent. Cancellation, failed attempts and backpressure are explicit states; no separate queue service is needed.

Faiss maintenance consumes committed changes idempotently. Start with a searchable published base and a bounded delta of new vectors, with tombstones/current-revision checks. Build a replacement from a stable sequence, replay subsequent changes, persist and verify its manifest, then atomically publish the new generation pointer. Keep the old generation alive until its readers finish. A restart rebuilds or replays from durable state, not process-local update lists.

Use CPU Faiss initially. Share immutable serving generations among readers; synchronize writes to a mutable delta or replace that delta with immutable snapshots. Faiss supports concurrent CPU searches but does not itself make concurrent search/add or add/add safe. PremiseDB owns this coordination. [Faiss concurrency guidance](https://github.com/facebookresearch/faiss/wiki/FAQ#can-searches-and-additions-be-performed-concurrently-from-several-threads).

Each retrieval pins a database sequence and index generation. Candidate IDs are validated against the corresponding record/passage revision at that sequence. Ahead-of-snapshot and obsolete candidates are discarded; an index's lag is reported. `require_indexed_through` can wait up to a deadline and return an explicit not-ready result; it cannot generate missing embeddings or silently downgrade the request.

Archive/deletion is immediately enforced by the authoritative eligibility check even if derived indexes lag. Semantic recall and indexing freshness are separate properties. Historical revisions remain available for explicit reads; arbitrary historical semantic search is deferred and must be rejected as unsupported rather than approximated with today's index. Stored timestamps do not remove knowledge embedded in a caller's LLM.

Backups capture a consistent SQLite snapshot and its referenced immutable objects. Preserve generated and supplied context/embedding payloads so restoring an ANN index does not require regenerating them. Derived Faiss files may be rebuilt. Test restoration before claiming recoverability.

## 7. Retrieval primitives

### `search`: discover entry points with compact connected context

Illustrative request, using hypothetical IDs and the workspace's configured retrieval profile:

```json
{
  "query": "What could delay Supplier A's capacity ramp?",
  "targets": ["research", "passages"],
  "seed_ids": ["entity_supplier_a"],
  "filters": {
    "primary_kinds": ["Finding", "Question", "Variable"],
    "about_any": ["entity_supplier_a"]
  },
  "research_context": {
    "hypothesis": "Equipment deliveries determine the ramp date",
    "seen": [{"id": "finding_equipment", "revision": 2}]
  },
  "budget": {"max_primary": 8, "max_context_tokens": 6000}
}
```

Only `query` is required for search; the default targets both research and passages. Optional structured inputs:

- `seed_ids` identify known starting points; a seed boosts or adds candidates but does not restrict the result universe.
- Primary filters select entry points. `primary_kinds` applies only to research hits; `about_any` uses explicit subject membership, inherited by passages from their Source. It is not automatic transitive exposure inference.
- Separate `scope` restrictions apply to every returned hit, path and preview. Workspace access and publication eligibility are always hard constraints. A company primary filter can still expose an external supplier through a connection.
- Typed date filters state which date they address and how unknown values are handled. A publication cutoff is not a historical knowledge cutoff. Unsupported filters fail explicitly.
- `retrieval_profile` defaults to the workspace's configured document-retrieval pipeline, including its embedding and reranking providers. Optional supplied query vectors name an embedding space and bypass query embedding. An explicitly selected lexical-only profile skips vector retrieval.
- `research_context` supplies a hypothesis and seen revisions for novelty/qualification handling. A configured query-rewriting stage can use this alongside recorded graph paths; the response reports the generated subqueries and their origins.
- Optional budgets cap returned tokens and primary results. Server-side limits also bound candidate search, edges examined, fan-out and execution time.

For document results, `max_primary` counts returned RSE segments, not individual chunks. Optional `document_retrieval.max_segment_tokens` limits one segment; the overall context budget covers segment text, displayed headers and graph context together. RSE runs for document search by default. Raw chunk candidates are available for inspection and scoring, not the normal evidence response.

The response contains:

| Field | Contract |
| --- | --- |
| `hits` | Records at explicit revisions or RSE document segments with immutable source locators, separate contextual headers and section references. |
| `connections` | Selected related previews, link IDs/revisions, direction and the path connecting them to a hit. |
| `evidence` | Deduplicated source segments and locators needed by the returned context. |
| `why_returned` | Retrieval channels, actual path, stored relationship explanation and matched subquery IDs. Generated query intent is labeled separately from recorded graph evidence. |
| `issues` | Recorded contradictions, supersession, unresolved Questions and support pinned to an earlier revision. These describe graph state, not a fresh adjudication of truth. |
| `frontier` | Concrete record/link IDs and continuation handles for further inspection. |
| `coverage` | Retrieval profile and providers used, context completeness, chunk-scoring profile, embedding availability, index sequence, snapshot sequence, limits reached and whether results are approximate or incomplete. |

For example, the equipment Finding can bring back a linked Question about utility energization, a Finding about the power constraint, and the affected ramp-date Variable. The agent can discover that issue even if the search never mentioned electricity. The database must have a recorded path to surface it; it does not invent the dependency.

A missing provider configuration or failed model request is explicit. A profile may define a fallback, but the response identifies which stages ran and any resulting limits; a failed semantic search is not reported as a successful search with no evidence.

Small budgets still include an issue marker when a selected Finding has a recorded challenge that cannot fit in full. If the budget cannot fit even the required envelope and markers, return a budget error or fewer primary hits. Markers and counts respect hard scope restrictions. Only claim no recorded challenges after checking the relevant exact adjacency, not after a truncated general traversal.

### `read`: resolve exact references

Batch-read records and links by ID/revision, or Source ranges by immutable locator. Omitting a revision resolves the current version and returns its revision. Explicit historical reads can use `at_seq`. Missing, archived and redirected identities have distinct responses. A caller can inspect the exact supporting revision as well as the current one.

### `expand`: follow connections deliberately

Accept seed IDs/revisions, optional query/context, relation labels, direction, depth and budgets. Return the same connection and evidence envelopes as search. Default to one hop; permit bounded two-hop exploration. Never expose unlimited recursive traversal as the default.

Traversal can follow a pinned support edge to historical evidence while also reporting its current status. Related-record status and scope checks apply to each intermediate node, not just the destination. Paginated expansion cursors preserve the graph sequence and ordering; an expired snapshot requires an explicit restart. Ranked search pagination continues a pinned candidate set and makes no claim to enumerate the entire corpus.

A separate `connect` path-finding primitive can follow after these three operations prove useful. No general graph query language is required for v0.

## 8. Query execution: the part PremiseDB should own

At ingestion, the document path is **document/section context and graph background → Contextual Chunk Headers → contextualized embeddings and lexical index**. At query time, document search and graph exploration feed one another: graph paths can introduce evidence subqueries before RSE, and RSE-selected ranges can reveal prior research through reverse citations. Model providers and retrieval profiles are configurable.

1. **Generate entry candidates.** Embed the query through the configured provider unless a query vector is supplied or the selected profile skips embeddings. Query contextualized lexical research/passage indexes, vector indexes, explicit seeds and identity aliases. Keep per-channel ranks; do not add raw BM25 and cosine scores as though they shared a scale. Begin with rank fusion and stable tie-breaking.
2. **Explore graph entry points and plan evidence retrieval.** Expand selected records through bounded paths. Reuse an existing Question when it expresses the evidence need. Otherwise, one batched model call can rewrite the original question into focused evidence subqueries grounded in selected Findings, Questions and link explanations. Scoped queries can introduce cited documents missed by initial search. Every subquery inherits hard scope constraints and receives a bounded share of the retrieval budget.
3. **Refine and score chunks for each query.** Use compressed Faiss search, fetch full vectors in batches and rescore in the declared original embedding space. This is the minDB pattern. Separately, the configured text reranker uses each query and header-plus-chunk representation to supply relevance scores to RSE. Check hard scope eligibility before submitting candidate text to a provider. Accept caller-supplied scores for integrations and test fixtures. A documented rank-based scoring profile supports explicit lexical-only operation. Vector similarity refinement and semantic reranking are distinct stages, and the latter's quality must not be assumed when it is absent.
4. **Check eligibility and revisions.** Apply exact scope, revision, status and metadata constraints. Where practical, restrict ANN candidates before ranking. For small eligible sets, exact vector scoring may be cheaper. For larger sets, increase candidates/probes within a budget. Report underfilled/limited results; returning k eligible hits does not prove high filtered recall.
5. **Run Relevant Segment Extraction.** Use dsRAG's scored, variable-length contiguous segment selection as the reference implementation, with explicit source locators and token-budget enforcement. Preserve its ability to include intervening chunks when a longer segment is useful and keep scores associated with their originating queries. Resolve selected ranges against canonical source text and keep contextual descriptions separate. Deduplicate overlapping evidence from the same content version.
6. **Recover connected research from evidence.** Follow reverse citations from selected source ranges, alongside the paths already discovered. Prioritize recorded challenges, qualifications, dependencies, relevant open questions and linked Variables. Entity co-membership provides additional candidates but is not itself supporting evidence. Cap high-degree hubs and report omitted branches. Start with one bounded round of graph-guided evidence queries plus this reverse lookup; further recursive investigation is an explicit agent action.
7. **Select context bundles.** A candidate bundle contains a useful destination, the links explaining its connection, and any necessary intermediate previews. Rank usefulness to the original question against added token cost, accounting for repeated records, repeated evidence and already-seen revisions. A high score on a generated subquery alone does not establish relevance to the original investigation. Preserve a small exploration allowance for strong relationships whose destination text has weak literal similarity.
8. **Return navigation, not a generated conclusion.** Include selected context, evidence, issue markers, limits and the frontier. The calling agent decides what to inspect and what the evidence means.

The document selector and graph selector share budgeting and deduplication machinery, but use different candidate structures. The first baseline uses documented relation priorities, lexical/vector ranks, path length and seen revisions. We should test whether a learned scorer improves selection before adding one. An optional scorer such as Jev belongs behind a caller-controlled boundary; it is not a required database dependency or a source of verified facts.

The internal query plan contains `query_id`, `parent_query_id`, text, originating record/link IDs and revisions, source/entity scope, scoring profile and budgets. It is an in-memory execution structure in v0; optional diagnostic traces do not add a domain record type. Generated evidence queries are not automatically persisted as Questions. Requests must not retain a SQLite transaction while waiting for query generation or reranking: capture immutable inputs and a logical sequence, then resolve subsequent reads against retained revisions.

### Relevant Segment Extraction contract

RSE takes ordered chunk references, per-query ranks/relevance scores, source boundaries and selection budgets. Start with dsRAG's relevance transform and greedy segment selection, retaining penalties for unhelpful intervening content, nonoverlap and coverage across supplied queries. Adapt storage access and budget accounting while preserving a reference configuration for comparison. This is an actual document retrieval component, independent of the graph selector. [dsRAG RSE implementation](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/rse.py#L3).

The database must fetch chunk ranges efficiently, including neighbors outside the initial top-k. RSE evaluates candidate contiguous intervals rather than mechanically returning a fixed number of neighbors around each hit. Missing candidate scores receive the configured missing-score penalty, not an invented relevance estimate; additional neighbor scoring can occur within a bounded scoring budget. BM25, cosine similarity and semantic scores require declared scoring profiles rather than being fed interchangeably into the relevance transform.

A segment cannot cross a document/content-version boundary or a hard eligibility boundary. Section boundaries are available to guide selection but are not universally hard stops: a table and its explanatory text may straddle one. Preserve table headings, units and footnotes where the selected source span contains them. Multiple disjoint relevant regions remain separately cited segments; do not present stitched text as one continuous excerpt.

Each returned segment contains its source/content version, canonical byte range, original text, participating chunk range, contextual-header references, matched query IDs and scoring-profile identifier. Use source ranges to remove duplicate text when chunks overlap. Charge the final rendered segment and its context against the token budget. If needed context cannot fit, expose the limitation and a continuation locator.

Document retrieval can return RSE segments even when the source has no Findings or graph links. Reverse citations then add prior research where it exists. A direct citation read remains exact; any surrounding RSE context is identified as an expanded evidence segment and never changes the original citation range.

## 9. Vector index and resource policy

Evaluate Faiss IVF-PQ with full-vector refinement for passages. Compare 32- and 64-byte codes, probe counts and candidate counts; evaluate OPQ/PCA rather than adopting minDB's defaults without checking our embeddings. Keep research records in a separate candidate pool so documents do not crowd out prior understanding. Small datasets can use exact search; the 500,000-Finding pool also needs a measured memory policy.

At 15 million passages, 32-byte codes plus 8-byte IDs imply 0.60 GB of payload; 64-byte codes plus IDs imply 1.08 GB. These exclude index overhead and all serving/maintenance memory. Full float32 vectors stay disk-backed. Fetching 500 768-dimensional candidates reads 1.536 MB of vector payload per query, before database page amplification and additional query variants. [Faiss index formats](https://github.com/facebookresearch/faiss/wiki/Faiss-indexes).

Bound training samples, insertion batches, delta size and simultaneous builds. Stream compact IDs rather than creating corpus-wide Python-object lists. Admission control must include the old serving index, the new index, training workspaces, query buffers and page cache. Pause or throttle indexing when budgets are exceeded; do not silently exceed them or drop committed updates.

Monitor indexing lag, missing embeddings, delta size, deleted-vector fraction, training-distribution drift and sampled ANN recall separately. Start with an explicit rebuild operation; add automatic scheduling only after its behavior is measurable.

## 10. First implementation and decision tests

The first deliverable is a small vertical slice: six record types, versioned links, source ranges, atomic contribution, document/section context storage, Contextual Chunk Headers, Relevant Segment Extraction, hybrid search, semantic reranking, read and bounded expansion. Define small provider interfaces for context generation, embeddings and reranking, and wire a configured provider path alongside supplied-output fixtures for repeatable tests. CCH and RSE are included in this slice, rather than deferred until after graph retrieval. Frontends, an autonomous investigation fleet and a financial calculation engine remain outside this slice.

Demonstrate this sequence:

1. One agent records a supplier bottleneck and its evidence; another records a dependent customer's plan.
2. A new agent asks about the customer's forecast and receives the connection, its basis and an unresolved condition without knowing the supplier's name.
3. A Variable is linked to a particular assumption and supporting Finding revision.
4. A new disclosure qualifies the Finding. Retrieval surfaces that change and the Variable that merits review.
5. Revising the Variable preserves the earlier value and marks its old support as requiring reassessment.

Evaluate progressively, separating useful research retrieval from storage throughput:

| Test | What would change our decision |
| --- | --- |
| Financial document retrieval | Use a manageable FinanceBench subset with available source documents and evidence annotations, supplemented by table/footnote cases. Verify that contextual headers disambiguate issuer/period/section and RSE returns the relevant complete spans, even with an empty research graph. |
| dsRAG component fidelity | Compare against the pinned CCH/RSE reference behavior using supplied context and scores. Check nonoverlap, intervening chunks, disjoint segments, multiple queries, context revisions and citation stability. Evaluate token-budget adaptations separately from the reference behavior. |
| Context refresh | Revise an exact input, add a qualifying link, and add a new Finding within a watched entity scope. Check refresh scheduling, concurrent publication, unchanged-text embedding reuse and preserved source citations. |
| Context value | At the same token budget, graph assembly should expose recorded relevant dependencies and challenges that search-only and a raw one-hop dump miss. Use real, manually reviewed research paths; report unsupported and irrelevant additions too. |
| SQLite storage behavior | Measure batched vector reads under warm/cold cache, ingestion and graph writes. Record p50/p95 latency, total disk, peak memory and recovery behavior; investigate another store only for a demonstrated limitation. |
| ANN quality | Compare candidate recall and refined top-k against exact search on representative frozen data, including narrow filters. Sweep code size/probes/candidates; semantic usefulness is a separate evaluation. |
| Concurrent contributions | Duplicate retries, two revisions of one Finding, independent Findings about one Entity and atomic Finding-plus-evidence batches. No lost update or partial publication. |
| Recovery | Interrupt contribution, source publication, index replay and generation swap; verify restart, deletion enforcement, citation resolution and backup restoration. |
| Scale | Progress to 15M passages, 500k Findings and 2.5M links with skewed hubs, then the proposed steady/burst traffic. Synthetic data tests capacity, not research relevance. |

Before calling the design ready for a desk, set and measure latency/memory targets on a named machine. With SQLite and Faiss selected, the immediate choices to settle are the graph-context selection policy, physical schema details and the filtered ANN operating point. None requires rebuilding low-level persistence or ANN algorithms.
