# Stated

**Most of the information that moves markets is qualitative. Stated turns it into data a systematic strategy can use.** Agents read filings, transcripts, news, research and any other text you bring, the way an analyst would, and record typed, point-in-time values for every company and date you schedule, each tied to the evidence behind it. They work on a shared research graph, so what one investigation learns, the next starts from.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)

[Install](#install) · [Build features](#build-features) · [Ask a question](#ask-a-question) · [Use your own agent](#use-your-own-agent) · [How the graph is organised](#how-the-graph-is-organised) · [Status](#status) · [CLI](docs/cli.md) · [Python API](docs/python-api.md) · [Feature guide](docs/features.md)

## Install

You need Python 3.12 or later built against SQLite 3.51.3 or later with FTS5 (`python -c "import sqlite3; print(sqlite3.sqlite_version)"` shows the version), plus [Claude Code](https://code.claude.com) or [Codex](https://openai.com/codex) signed in to your own account. The steps install [pipx](https://pipx.pypa.io) with Homebrew; on Linux, use your package manager.

```bash
brew install pipx
pipx install "stated[cli] @ git+https://github.com/zmccormick7/Stated.git"
stated ui
```

The first run creates a research folder at `~/Stated`, installs the bundled skills for your agent, and opens the app in your browser. To start from the published base layer instead of an empty graph, run it once with the base file: `stated ui --base base-2026.10.01.sqlite`.

To work on Stated itself, clone the repository anywhere but `~/Stated`, which the app uses for research, and install it in editable mode (`python -m pip install -e '.[cli]'` inside a virtual environment); `stated` then runs from your checkout.

## Build features

A feature is a question you would put to an analyst, written once and answered for every subject on every date you schedule. The answer is a typed value (a number, a count, yes or no, or one of a fixed set of categories) that describes the subject as it stood on that date. Agents never forecast: they describe, from what was published by then, and the predicting is left to your models. The [feature guide](docs/features.md) explains why that keeps hindsight out and how to write features that need real analysis.

```bash
stated define-feature --input - <<'JSON'
{"author_id": "researcher", "key": "uninsured_share", "type": "number", "min": 0, "max": 1,
 "units": "fraction of total deposits",
 "instructions": "Share of the bank's total deposits not covered by deposit insurance, as of the latest balance sheet the bank has reported. Add estimated uninsured domestic deposits to foreign deposits, which are uninsured, when they are disclosed separately; divide by total deposits. Use the bank's own estimates. Null when the bank has not disclosed an uninsured estimate."}
JSON

stated schedule-feature uninsured_share --input - <<'JSON'
{"every": "weekday", "start": "2021-01-04"}
JSON

stated run-features uninsured_share --subjects-file banks.txt --workers 4
stated feature-table --from 2021-01-04 --feature uninsured_share --out features.csv
```

- **Define** a feature once: what to measure, the type of its values, and when the answer is unknown. The stated-feature-design skill helps turn an idea into definitions that describe rather than predict.
- **Schedule** when a value is due for each subject: every day, weekday, week, month or quarter.
- **Run** the due dates. For each subject, in date order, a fresh session of your own Claude Code or Codex opens at that date's cutoff with the web off. Stated shows it only the sources published by then, and it writes the value linked to the evidence it rests on, or confirms the last one still holds. A value with no new document about the subject since it was last read is confirmed without a session, so runs follow the flow of documents rather than the calendar. Subjects run in parallel, and a stopped run picks up where it left off when you run it again. Codex's sandbox can still read files on disk, so keep later documents out of its reach.
- **Derive** features that are formulas over other features; Stated computes them, never an agent.
- **Export** the daily table: one row per subject and day, each feature's value, the date it last changed and the date it was last checked.

Run the commands in your research folder, or point `STATED_DB` at its database. A subjects file holds one Entity ID per line. Sources must carry the date they became public and be linked `about` their subject, since a source without a date is never seen at any cutoff.

To look over what a run produced, open Features (▦) in `stated ui`: every definition with its schedule and coverage, then each subject's latest value, when it last changed and was last checked, and how many values are dated on or before the day the instructions were written, which may carry hindsight. A subject opens to its values over time, each opening to the evidence it rests on, with anything a cutoff on the value's date could not have seen marked.

## Ask a question

`stated ui` runs a small local web app on your machine, next to your research folder: a Python server and a plain web page, with nothing hosted elsewhere. The page holds threads of questions and their answers, like a chat, in a sidebar on the left. The answers come from your own Claude Code or Codex, which the app runs in your research folder each time you ask.

Type a question and pick an effort beside the Ask button:

| Effort | What the agent does |
| --- | --- |
| 1 minute | Answers from what the graph already holds. |
| 5 minutes | Looks through the graph, adds what it learns to your layer, and answers. |
| 20 minutes | Also reads outside sources: SEC filings, releases, transcripts, pages and PDFs. |
| 1, 3 or 8 hours | A research run: plans pieces of work, has fresh sessions do them in parallel, and reports. You can pause it, finish it early or add a note to steer it. |

An answer is a one-line headline, a body as long as the question needs, numbered sources, and where they help a chart, a quote, a flow of who gets what, or a lever you can drag to move a model's input and watch its output. Every figure on the page is checked against the record it cites before the answer is shown.

The agent runs headless in your research folder with your own login and settings. Stated adds only what it needs: permission to run `stated`, to read and write files there, and, from 20 minutes up, to use the web. To let it fetch filings from SEC.gov, which requires a contact, run `stated ui --contact "Your Name you@example.com"` once; the contact is kept beside the database and sent nowhere else.

## Scaling research

A question that takes a minute is answered by one session. A brief that takes a day is answered by many: a research run breaks the work into pieces, gives each piece to a fresh session, runs as many sessions at once as you choose, and keeps going until the queue is empty or the time is up. Nothing about the run limits its size. A run of eight hours with ten agents is the same loop as a run of one hour with one, and the next run starts from everything the last one left in the graph.

This is test-time compute scaling applied to research. More compute at the moment of asking buys a better answer, as it does when a model reasons longer before replying. The difference is where the compute goes. A model's reasoning is discarded when its answer is returned; a research run's work is written into the graph as findings, sources, questions and model inputs, each cited and checkable, so it compounds. The hundredth question about a company starts from the work of the first ninety-nine, and so does every other agent and every other person who uses the same graph.

The pieces are what make the loop work:

- **Sessions are short and fresh.** Each takes one piece of work, starts with no memory of the others, and leaves behind only what it put in the graph. That is what lets the run survive a killed session, a usage limit or a restart: the queue and the graph are the state, and the next session picks them up.
- **Agents coordinate only through the graph.** There is no messaging between them. A worker sees the list of pieces so it does not repeat one, checks the graph before creating a record, and a review session folds duplicates into the records they repeat.
- **The budget is yours.** The agents run on your own Claude Code or Codex subscription, and that is the ceiling. A run waits out a usage limit and carries on; using both harnesses spreads the load across two subscriptions.

What this has done so far: a run of 274 company coverage packs, two sessions per company and six at a time, took seven and a half hours and about 40% of one week's Codex limit, and added 6,900 cited findings and 1,300 entities to the base layer. The same loop, pointed at the go-deeper skill, builds driver models with sourced inputs one company at a time.

## Use your own agent

Five skills teach an agent the graph. `stated ui` installs them; for any other harness, copy the folders under `stated/skills/` into its skills directory.

- **stated-research**: recall what the graph holds, contribute findings with exact citations, and revise understanding as evidence changes.
- **stated-answer**: return an answer document that passes `stated check-answer`.
- **stated-go-deeper**: take a company past its standard coverage by building a driver model, turning its unsourced inputs into a research list, and working that list from filings and transcripts.
- **stated-feature-design**: turn an idea for something to measure into feature definitions whose instructions describe state as of a date rather than forecast, and review existing definitions against the same tests.
- **stated-state**: produce one feature's value for a subject as of a cutoff date, following the feature definition's instructions over everything a reader on that date could see, or computed by its formula from other features.

Point a session at the folder with `STATED_DB=/path/to/research.sqlite` and ask it to use the skill. The [CLI guide](docs/cli.md) covers every command, and the [Python API](docs/python-api.md) is there for agents and tools you write yourself:

```python
from stated import RecordStore, recall

with RecordStore("research.sqlite") as db:
    for found in recall(db, "customer concentration").records:
        print(found.record.kind, found.record.text[:80])
```

Document search and the model-written document summaries use OpenRouter; set `OPENROUTER_API_KEY` in the agent's environment to turn them on.

## How the graph is organised

Six record types, connected by named links:

| Record | Holds |
| --- | --- |
| **Entity** | A company, facility, product or market. |
| **Source** | A filing, transcript, release or page, with its text captured so quotes can be checked. |
| **Finding** | A statement to build on or challenge, labelled disclosed, inference or assumption, citing its source. |
| **Question** | Something unresolved and worth investigating. |
| **Model** | A driver model of a business. |
| **Variable** | A model input or output, or a feature value, with its value, units, periods and the evidence it rests on. |

Links such as `about`, `supported_by`, `depends_on`, `contradicts` and `raises` carry the reasoning: a Variable depends on a Finding, which is supported by a Source and raises a Question. Models are checked with `stated check-model`, which recomputes every output's formula from the stored inputs, and evaluated with overrides for the levers on the answer page.

The graph is layered. A **base layer** is a published, read-only graph that any research folder can attach and later update; your own layer holds what your agents add on top, including corrections to base records. Knowledge belongs to the folder, not to any one conversation or harness: a fresh session, on either harness, picks up exactly where the last one left off.

The graph can also be read **as of a date**. Every record carries the date its contents are as of, and a session opened at a cutoff sees only the sources published by then, the disclosed facts those sources support, and what earlier sessions wrote at or before that cutoff; what it writes is stamped with the cutoff in turn. That is what lets agents describe a company's state at a past date from the evidence that existed then, with their work checkable against the dated record rather than against hindsight. [Features](#build-features) are built on this: each value is a Variable written at its date's cutoff, while the feature definitions are kept beside the graph rather than in it, since they are procedures, not knowledge.

## Status

Stated is early software. The database format changes without migrations; recreate a prototype database when it does.

What exists today:

- The CLI, Python API and five skills. Three are used daily by Claude Code and Codex; the feature-design skill is new; the state skill has been tried on six banks at one cutoff and on one bank at two, in a workspace holding only their pre-cutoff filings.
- Features end to end: definitions, schedules, `run-features`, derived features and the daily `feature-table`. Runs so far are small, a handful of banks over a few dates.
- The answer page with checked answers, levers, research runs and the notes, pause and finish controls.
- A base layer covering the 1,000 largest US filers by public float, with the full text of each company's latest annual report and, for 474 of them, a coverage pack of its business, segments, key figures, relationships, risks and open questions. Driver models exist for a handful of companies.

What is being worked on: coverage packs for the rest of the top 1,000, driver models with sourced inputs for covered companies through the go-deeper skill, quarterly updates as filings land, collecting a universe's documents for feature runs (sources are ingested one at a time today), runs across many companies and years, and measuring how much hindsight still leaks into values. The [design specification](docs/database-spec.md) records the reasoning behind the schema.

## Contributing

Issues and pull requests are welcome. [AGENTS.md](AGENTS.md) sets out the conventions: one reviewable change per branch, tests for contracts and failure modes, and honest documentation of what is implemented. Run the tests from a checkout:

```bash
python -m unittest discover -s tests -v
```

## License and foundations

[MIT](LICENSE). The document-retrieval pipeline adapts ideas and code from [dsRAG](https://github.com/D-Star-AI/dsRAG), with storage ideas from [minDB](https://github.com/D-Star-AI/minDB); see [NOTICE](NOTICE) and the [technical review](docs/dsrag-mindb-review.md).
