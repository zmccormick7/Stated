# What PremiseDB can reuse from dsRAG and minDB

Reviewed September 21, 2026. This is a source-code review, not a reproduced performance evaluation. Neither repository's application code or model integrations were executed.

Pinned versions:

- [dsRAG `5215e97`](https://github.com/D-Star-AI/dsRAG/tree/5215e9791133fc1ad383af043fb2fde0e7d4e4d4)
- [minDB `249688c`](https://github.com/D-Star-AI/minDB/tree/249688c225c53f2335734f0eb243a69b5f2ff6ee)

## The useful combination

minDB gives us a way to find vector candidates with a relatively small in-memory index. dsRAG gives us a way to turn search candidates into useful evidence rather than isolated chunks. PremiseDB can combine these ideas with a graph that stores current research and why one piece of understanding matters to another.

**Adopt Contextual Chunk Headers and Relevant Segment Extraction as core document-retrieval components.** The project author's experience with these techniques on financial documents, including FinanceBench, is the reason to carry them into the first PremiseDB implementation. Their role is concrete: improve chunk representations before search, then extract useful variable-length segments after scoring. Graph context selection builds on those components.

The important new work is selecting connected research context and maintaining consistency between evidence, current graph contents and search indexes. Neither project already supplies that layer.

## dsRAG: search units and returned context can differ

The query pipeline retrieves and reranks chunks, then performs Relevant Segment Extraction (RSE). It selects nonoverlapping, contiguous spans under per-segment and overall length limits. A chunk's value combines its rank and relevance score with an irrelevant-content penalty. Selection cycles through the supplied queries. The implementation is a greedy interval-selection procedure, not a globally optimal selection or a graph-traversal algorithm. [Query pipeline](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/knowledge_base.py#L787), [RSE implementation](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/rse.py#L3).

**Retain:** incorporate RSE itself: its chunk-relevance transform, variable-length contiguous segment selection, treatment of intervening content, overlap removal and multi-query coverage. Keep the pinned implementation as a reference when adapting it to PremiseDB storage and budgets.

**Adapt:** use token budgets and immutable source locators. A document result can remain a contiguous segment. A research result should be a finding plus selected evidence, qualifications, dependencies and questions, each with an actual connecting path. That requires a new graph-context selector: graph neighborhoods have branches and shared nodes, unlike document intervals.

### Contextual Chunk Headers are part of the indexing contract

dsRAG prepends document and section context to chunks used for embedding and reranking. Its chunking code also preserves section, line and page information and handles visual elements separately. These are useful boundaries between content, search representation and source navigation. [Context headers](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/auto_context.py#L136), [chunking](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/dsparse/sectioning_and_chunking/chunking.py#L5).

**Retain:** use document title and summary plus section title and summary to build Contextual Chunk Headers. Embed and text-rerank the header together with the original chunk. Store shared document/section context explicitly, including last-edited timestamps and provenance. Finding search representations can use similar enrichment, but that extension does not replace CCH for source documents.

**Adapt:** keep raw evidence separate from enrichment. Generated or agent-authored summaries and contextual labels must never become purported source quotations. Store their provenance and include a fingerprint of their current contents in the search representation's identity. PremiseDB can generate section structure and context using configured models, or accept existing outputs from the caller.

### Reuse components selectively

dsRAG has useful boundaries around document storage, vector retrieval, reranking and segment assembly. Its default `KnowledgeBase` initializes hosted model providers and basic local stores. The basic vector backend holds Python lists, persists them with pickle and performs exhaustive search, including in its optional Faiss path. Its `search` accepts a metadata filter but does not apply it in that backend. These observations concern the basic backend, not all dsRAG integrations. [Initialization](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/knowledge_base.py#L133), [basic vector backend](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/database/vector/basic_db.py#L23).

PremiseDB should retain dsRAG's model-backed document-retrieval pipeline with configurable providers, while adapting persistence to the shared graph. The basic backend's storage and filtering behavior should not determine PremiseDB's implementation. Also, dsRAG writes chunks and vectors through separate stores; PremiseDB needs an explicit publication and recovery protocol when one of those writes fails. [Ingestion helpers](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/dsrag/add_document.py#L158).

## minDB: compression for discovery, full vectors for refinement

minDB searches a compressed Faiss index for a relatively broad candidate set, fetches those vectors from LMDB, and runs full-vector nearest-neighbor search to produce the final results. It keeps metadata separately and fetches it for the finalists. This refinement compares vectors; it is distinct from a semantic text reranker. Refinement can reorder retrieved candidates, but cannot recover neighbors the first stage missed. [Query implementation](https://github.com/D-Star-AI/minDB/blob/249688c225c53f2335734f0eb243a69b5f2ff6ee/mindb/mindb.py#L368), [LMDB vector reads](https://github.com/D-Star-AI/minDB/blob/249688c225c53f2335734f0eb243a69b5f2ff6ee/mindb/utils/lmdb_utils.py#L50).

**Retain:** compressed candidate discovery, batched disk reads of full vectors, then exact scoring within that candidate set. Keep text and graph payloads out of the ANN index. Normalize and validate vectors consistently and record which embedding space they belong to.

For our illustrative 15 million 768-dimensional float32 passage vectors, raw vector payload is 46.08 GB. With 32-byte PQ codes and 8-byte IDs, codes plus IDs would be 0.60 GB. This is arithmetic, not a predicted memory measurement: centroids, codebooks, lookup tables, allocator overhead, caches and maintenance copies are additional. [Faiss index storage formulas](https://github.com/facebookresearch/faiss/wiki/Faiss-indexes).

The published minDB benchmark measures a much smaller FIQA corpus, and its stated memory comparison uses index size rather than total process memory. It supports testing this approach; it does not establish PremiseDB's latency or memory use at 15 million passages. [Benchmark methodology](https://github.com/D-Star-AI/minDB/blob/249688c225c53f2335734f0eb243a69b5f2ff6ee/README.md#performance-evaluation), [evaluation notebook](https://github.com/D-Star-AI/minDB/blob/249688c225c53f2335734f0eb243a69b5f2ff6ee/eval/minDB_performance_eval.ipynb).

### Index construction deserves a memory budget too

minDB supports sampled training and two-level clustering, reads training vectors in batches, and adds corpus vectors to the index in batches. It uses heuristics to choose training parameters and the training route. The two-level implementation still materializes corpus-wide ID and assignment structures, so batching vectors alone does not bound every allocation. [Training](https://github.com/D-Star-AI/minDB/blob/249688c225c53f2335734f0eb243a69b5f2ff6ee/mindb/train/train.py), [two-level clustering](https://github.com/D-Star-AI/minDB/blob/249688c225c53f2335734f0eb243a69b5f2ff6ee/mindb/train/two_level_clustering.py), [parameter selection](https://github.com/D-Star-AI/minDB/blob/249688c225c53f2335734f0eb243a69b5f2ff6ee/mindb/utils/training_utils.py).

**Retain:** train and rebuild under a resource budget; the ability to query cheaply is insufficient if constructing the index requires a much larger machine.

**Adapt:** start with sampled Faiss training and batched ingestion; evaluate two-level clustering when the sample budget limits recall. Measure total peak memory, temporary disk space and concurrent serving costs. Parameter heuristics are starting points to benchmark, not fixed PremiseDB defaults.

### Updates and filters need additional work

minDB tracks the population present during training to decide when retraining is warranted. Its server tracks additions and removals during training in process-local collections, then catches the new index up. Vector data, metadata, Faiss files and configuration are persisted separately. [Writes and saves](https://github.com/D-Star-AI/minDB/blob/249688c225c53f2335734f0eb243a69b5f2ff6ee/mindb/mindb.py#L162), [training coordination](https://github.com/D-Star-AI/minDB/blob/249688c225c53f2335734f0eb243a69b5f2ff6ee/mindb/api/fastapi.py#L215).

**Retain:** explicit index health and retraining rather than assuming a trained index stays representative forever.

**Adapt:** track pending item IDs and publish replaceable index files, rebuilding from current stored payloads after a crash. Distinguish three things: whether a committed item has been indexed, whether the training distribution still represents the corpus, and measured retrieval recall. A training coverage ratio is not a recall measurement.

Metadata filtering is explicitly future work in the reviewed README, and the query method has no filter parameter. PremiseDB needs filters during candidate generation where feasible, exact eligibility checks before return, and a way to report when filtering exhausted the search budget. A naive global top-k followed by filtering will often underfill narrow company or date queries. [minDB metadata scope](https://github.com/D-Star-AI/minDB/blob/249688c225c53f2335734f0eb243a69b5f2ff6ee/README.md#metadata).

## Reuse decisions for the first implementation

| Area | Proposed approach |
| --- | --- |
| Document ingestion and CCH | Include document/section context storage and deterministic Contextual Chunk Header construction in v0; configured models generate summaries, or callers supply them. Use headers for embeddings, lexical retrieval and text reranking. Preserve original artifacts and exact locators. |
| Document context assembly | Include RSE in v0, using dsRAG's algorithm as the reference. Adapt source access and token accounting with explicit fidelity checks; default document results are RSE segments. |
| Vector search | Implement minDB's compressed-candidate/full-vector-refinement pattern using Faiss. |
| Training | Start with sampling and bounded batches; evaluate minDB's two-level method before adopting its additional complexity. |
| Persistence | Use one authoritative SQLite database, including disk-backed full vectors. Measure it under load before considering another store. |
| Graph retrieval | Build the new part: bounded, relation-aware context selection with evidence paths, current-state checks and explicit omissions. |
| Model calls | PremiseDB invokes configured context, embedding and reranking providers as part of ingestion and retrieval. Caller-provided outputs are supported alternatives. |

SQLite is the preferred starting point, not a claim that it outperforms LMDB. We can reuse minDB's retrieval architecture independently of its storage backend. LMDB's named databases can share a transaction, but adopting it would also mean maintaining more secondary-index machinery ourselves. That is an alternative to revisit only for a measured limitation. [LMDB transaction structure](https://lmdb.readthedocs.io/en/release/#named-databases).

Both projects use the MIT license. Any later code adaptation must retain the applicable notices. This review introduces no copied implementation. [dsRAG license](https://github.com/D-Star-AI/dsRAG/blob/5215e9791133fc1ad383af043fb2fde0e7d4e4d4/LICENSE), [minDB license](https://github.com/D-Star-AI/minDB/blob/249688c225c53f2335734f0eb243a69b5f2ff6ee/LICENSE).

The resulting proposal is in [the database specification](database-spec.md).
