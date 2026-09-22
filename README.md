# PremiseDB

**A shared knowledge graph that grows with every investment research investigation.**

PremiseDB gives your agents a persistent understanding of the companies, markets and economic relationships they research.

Agents contribute findings, connect them to existing knowledge, and use those connections in subsequent investigations. Evidence, interpretations and unanswered questions accumulate in a graph that any connected agent can explore and extend.

The goal is to support one agent you guide carefully or a fleet researching independently.

**Every investigation becomes a starting point for the next.**

> **Status: early implementation.** A Python store supports creating, reading and updating current records with a last-edited timestamp. The graph, retrieval pipeline, installer and agent integrations below remain planned.

The [database design specification](docs/database-spec.md) selects SQLite with FTS5 for records, graph storage and lexical search, and Faiss for vector search. It covers retrieval and concurrent contributions, building on a [source review of dsRAG and minDB](docs/dsrag-mindb-review.md). Detailed implementation choices remain proposals to validate.

Document retrieval will incorporate dsRAG's **Contextual Chunk Headers** to give each chunk its document and section context, and **Relevant Segment Extraction** to return useful variable-length evidence spans. The graph connects that evidence to accumulated research and model assumptions.

## Try the record store

From this checkout, use Python 3.12+ linked against SQLite 3.51.3+ (which includes the [WAL-reset fix](https://sqlite.org/wal.html#walresetbug)). There are no third-party runtime dependencies in this slice.

```python
from premisedb import RecordStore

with RecordStore("research.sqlite") as db:
    first = db.create("Finding", text="Ramp expected in June", author_id="alice")
    second = db.update(first.id, text="Ramp delayed to September", author_id="bob")
    assert db.read(first.id) == second
    print(second.updated_at)
```

Run tests from the repository root with `python -m unittest discover -s tests -v`.

Each record has a stable ID and kind, current text, JSON attributes, the last editor's `author_id`, and a database-assigned UTC `updated_at` timestamp. Updates replace text and attributes in full; omitted attributes become `{}`. Missing references raise `KeyError`. Invalid SQL-constrained fields raise `sqlite3.IntegrityError`. Returned metadata is a detached copy; changing it does not update the database.

One table stores current records. Writes are atomic, and the last committed update wins. There are no revision counters, stale-edit checks or historical reads. Opening a version-1 database keeps each record's latest contents and removes its history tables. Schema format numbers only identify file layouts. Use one store per thread on a local filesystem; the API and schema are experimental.

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
