# Working on PremiseDB

## Product direction

Read [README.md](README.md) before planning or implementing changes. PremiseDB is a shared, evolving knowledge graph for fundamental investment research. Its purpose is to let agents build on understanding accumulated across investigations.

- Support both one carefully guided agent and many independent agents contributing to the same graph. Knowledge belongs to the shared workspace, not a conversation or a particular harness.
- Organize the interface around **Recall, Contribute and Revise**: retrieve relevant understanding, add findings and connections, and correct earlier understanding while preserving its history.
- Keep the core usable through existing agent harnesses, including Codex, Claude Code and custom workflows. Skills should teach agents to use the common operations.
- A useful first demonstration is a fresh agent connecting findings from independent investigations without access to their conversation histories.

## Scope

- This repository starts from a clean foundation. Earlier prototypes impose no architecture, compatibility or migration requirements on new work.
- Do not add a frontend unless the user requests one.
- PremiseDB may call configured local or hosted models for document processing, contextual headers, embeddings and retrieval scoring. Keep providers replaceable and accept caller-supplied outputs where useful. The earlier manual-only prototype workflow is not a permanent product constraint. Existing agent harnesses conduct investigations; do not build an agent orchestrator into PremiseDB.
- Include dsRAG's Contextual Chunk Headers and Relevant Segment Extraction in the document-retrieval foundation. Preserve source text and citations separately from model-generated context.
- Use SQLite, including FTS5, for authoritative storage and lexical search, and Faiss for derived vector indexes. Do not add another database or search service without a demonstrated need. Runtime and transport remain open; the illustrative README command is not a commitment to a language or package manager.
- Keep the initial implementation small and coherent. Financial calculators, broad data integrations and deployment infrastructure are outside the initial scope.

## Knowledge and shared writes

- Preserve evidence, source locators, authorship, scope and relevant dates. Distinguish when a finding applies from when the graph learned it; unknown values stay unknown.
- Keep disclosed facts, interpretations, scenario assumptions and open questions distinguishable. Inferences need a traceable basis; do not invent evidence or imply expert review that did not happen.
- Preserve revisions and explicit disagreements. New evidence must not silently erase earlier understanding or make prior research appear to have known more than it did.
- Treat stable entity identity and concurrent contributions as core design requirements. Shared writes must avoid lost updates and make conflicting revisions visible.
- Let agents follow relationships, supporting evidence and unresolved questions. Useful knowledge must not require a numerical model or deterministic scenario calculation.

## Development

- Keep README status and examples honest about what is implemented. The proposed installer and harness integrations are not available yet.
- Add tests for meaningful contracts and failure modes as implementation arrives. Validate the behavior being changed; there are currently no application build or test commands.
- Keep downloaded source documents and local research material out of version control. Commit only appropriate excerpts, summaries and provenance.
- Preserve the existing GitHub repository and history. The open-source ambition does not authorize changing visibility, publishing packages or deploying a site.
