# Working on Stated

## Product direction

Read [README.md](README.md) before planning or implementing changes. Stated is for systematic investing. It turns qualitative information (filings, transcripts, news, research and any other text), together with structured data the user brings, into point-in-time features a systematic strategy can use. Agents read the documents the way an analyst would and record typed values for every subject and date a feature schedules, each tied to the evidence behind it. They work on a shared graph, so what one session records, the next can build on. The graph is the means, not the product.

- Serve systematic investing only. Do not add, and do not keep, support whose only use is fundamental research: answering questions, scenario analysis with levers, open-ended investigation. Judge each command, skill and document by whether a feature pipeline needs it.
- Features describe, never predict: a value says what a subject's state was as of a date, from what was public by then. Forecasting is left to the user's models.
- A feature is one of three kinds, by what writes its values: **agentic** (an agent session at the cutoff), **imported** (code, from the rows of a dated table Source) or **derived** (a formula Stated computes). A fundamental model is a pattern built from them: a **static model** is input features plus derived outputs, the same structure for every subject; a **dynamic model** is a company's own driver model, maintained by agent sessions at each cutoff, whose feature value is a standard output read from it.
- Organize the interface around the feature pipeline: **define, schedule, run, derive, export and review**.
- Support many independent sessions writing to the same graph. Knowledge belongs to the shared workspace, not a conversation or a particular harness.
- Keep the core usable through existing agent harnesses, including Codex, Claude Code and custom workflows. Skills should teach agents to use the common operations.

## Scope

- During early development, reject incompatible database schema formats with a clear error. Do not add migrations; developers recreate prototype databases when the schema changes.
- The UI (`stated/ui`) is a local desktop app that runs on the user's machine beside their workspace: a small Python server and plain JavaScript with no build step, showing a read-only Features view of definitions, values and the evidence behind them. It needs no phone layout. Do not add a hosted service or another frontend unless the user requests one. Its Ask half (question threads, research runs and answer documents) serves fundamental research and is being removed; do not extend it.
- Stated may call configured local or hosted models for document processing, contextual headers, embeddings and retrieval scoring. Keep providers replaceable and accept caller-supplied outputs where useful. Existing agent harnesses do the reading; do not build an agent orchestrator into Stated. The one loop is a feature run (`stated run-features`): it works through the sessions a feature's schedule leaves pending, each a fresh session of the user's own harness (Claude Code or Codex, run headless with the user's own login and settings) at its date's cutoff with the web off, subjects in parallel and each subject's dates in order, and keeps no state beyond the graph's values and checks. Sessions share nothing but the graph; add no messaging between agents and no other scheduler.
- Document retrieval keeps dsRAG's Contextual Chunk Headers and Relevant Segment Extraction. Preserve source text and citations separately from model-generated context.
- Use Python and explicit SQL. Use SQLite, including FTS5, for authoritative storage and lexical search, and Faiss for derived vector indexes. Do not add another database or search service without a demonstrated need. Agents use a thin CLI over the Python API and the bundled skills. Do not add an MCP server.
- Keep all table, index and trigger definitions of the research database together in `stated/schema.sql`. The UI's conversation threads, until they are removed with the Ask half, live in a separate SQLite file defined in `stated/ui/schema.sql`.
- Keep the initial implementation small and coherent. Structured data comes in as tables the user supplies; vendor-specific connectors, financial calculators, backtesting, portfolio construction and deployment infrastructure are outside the initial scope.

## Knowledge and shared writes

- Preserve evidence, source locators, authorship and scope. Records and links store only current contents and a database-assigned UTC `updated_at` timestamp. Domain dates belong in source metadata; unknown values stay unknown.
- Keep disclosed facts and interpretations distinguishable. Inferences need a traceable basis; do not invent evidence or imply expert review that did not happen.
- Do not add history tables, revision counters, endpoint pins, audit trails or historical reads. A point-in-time read is a filter on evidence dates and write stamps over current rows, never a reconstruction of earlier contents. Keep disagreements as separate current Findings when useful.
- Keep stable identities. Updates replace current contents atomically; the last committed update wins. A change to a Variable's value is the exception: it is written as a new Variable record linked `updates` the previous one, which keeps its value and its date, so a model's inputs can be reviewed as they stood on any date. There is no stale-edit detection or automatic historical reconstruction.
- A feature definition holds the instructions and value type agents follow. Definitions are procedures, not knowledge: they live in their own table beside the graph, and once a value names one it is fixed, so new instructions replace it. A value is a Variable about its subject, written by a session at its date's cutoff and linked to the evidence it rests on. A derived feature is a formula Stated computes; agents never write its values.
- Let agents follow relationships and supporting evidence. Every value must be traceable to what a reader on its date could see.

## Development

- Keep README status and examples honest about what is implemented. `stated ui` installs the bundled skills for the user's harness; for any other harness, installation is a documented file copy.
- Build one reviewable commit at a time, targeting 100–400 changed lines including SQL, tests and documentation. Stop for review before adding the next layer.
- For each increment, create a new branch from `main`, named for the change with no prefix, and open a draft PR against `main` for review. Start each increment as one commit and add review follow-ups as new commits, without amending or force-pushing. Merge only when the user requests it.
- Test meaningful contracts and failure modes with `python -m unittest discover -s tests -v`; see README for runtime requirements.
- Keep downloaded source documents and local research material out of version control. Commit only appropriate excerpts, summaries and provenance.
- Preserve the existing GitHub repository and history. The open-source ambition does not authorize changing visibility, publishing packages or deploying a site.
