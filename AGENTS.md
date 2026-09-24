# Working on PremiseDB

## Product direction

Read [README.md](README.md) before planning or implementing changes. PremiseDB is a shared, evolving knowledge graph for fundamental investment research. Its purpose is to let agents build on understanding accumulated across investigations.

- Support both one carefully guided agent and many independent agents contributing to the same graph. Knowledge belongs to the shared workspace, not a conversation or a particular harness.
- Organize the interface around **Recall, Contribute and Revise**: retrieve relevant understanding, add findings and connections, and update current understanding.
- Keep the core usable through existing agent harnesses, including Codex, Claude Code and custom workflows. Skills should teach agents to use the common operations.
- A useful first demonstration is a fresh agent connecting findings from independent investigations without access to their conversation histories.

## Scope

- This repository starts from a clean foundation. Earlier prototypes impose no architecture, compatibility or migration requirements on new work.
- During early development, reject incompatible database schema formats with a clear error. Do not add migrations; developers recreate prototype databases when the schema changes.
- Do not add a frontend unless the user requests one.
- PremiseDB may call configured local or hosted models for document processing, contextual headers, embeddings and retrieval scoring. Keep providers replaceable and accept caller-supplied outputs where useful. The earlier manual-only prototype workflow is not a permanent product constraint. Existing agent harnesses conduct investigations; do not build an agent orchestrator into PremiseDB.
- Include dsRAG's Contextual Chunk Headers and Relevant Segment Extraction in the document-retrieval foundation. Preserve source text and citations separately from model-generated context.
- Use Python and explicit SQL. Use SQLite, including FTS5, for authoritative storage and lexical search, and Faiss for derived vector indexes. Do not add another database or search service without a demonstrated need. Agents use a thin CLI over the Python API and the bundled research skill. Do not add an MCP server.
- Keep all table, index and trigger definitions together in `premisedb/schema.sql`.
- Keep the initial implementation small and coherent. Financial calculators, broad data integrations and deployment infrastructure are outside the initial scope.

## Knowledge and shared writes

- Preserve evidence, source locators, authorship and scope. Records and links store only current contents and a database-assigned UTC `updated_at` timestamp. Domain dates belong in source metadata; unknown values stay unknown.
- Keep disclosed facts, interpretations, scenario assumptions and open questions distinguishable. Inferences need a traceable basis; do not invent evidence or imply expert review that did not happen.
- Do not add history tables, revision counters, endpoint pins, audit trails or historical reads. Keep disagreements as separate current Findings when useful.
- Keep stable identities. Updates replace current contents atomically; the last committed update wins. There is no stale-edit detection or automatic historical reconstruction.
- Let agents follow relationships, supporting evidence and unresolved questions. Useful knowledge must not require a numerical model or deterministic scenario calculation.

## Development

- Keep README status and examples honest about what is implemented. Skill installation is a documented file copy; do not imply an automated harness installer.
- Build one reviewable commit at a time, targeting 100–400 changed lines including SQL, tests and documentation. Stop for review before adding the next layer.
- For each increment, create a new `codex/` branch and open a draft PR against `main` for review. Keep the increment in one commit; merge only when the user requests it.
- Test meaningful contracts and failure modes with `python -m unittest discover -s tests -v`; see README for runtime requirements.
- Keep downloaded source documents and local research material out of version control. Commit only appropriate excerpts, summaries and provenance.
- Preserve the existing GitHub repository and history. The open-source ambition does not authorize changing visibility, publishing packages or deploying a site.
