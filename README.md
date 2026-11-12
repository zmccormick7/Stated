# Stated

**Most of the information that moves markets is qualitative. Stated turns it into data a systematic strategy can use.** Agents read filings, transcripts, news, research and any other text you bring, the way an analyst would, and record typed, point-in-time values for every company and date you schedule, each tied to the evidence behind it. Structured data you bring, such as XBRL financials, sits beside the text as dated tables. Stated builds the features; your models do the predicting.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)

[Install](#install) · [Build features](#build-features) · [Use your own agent](#use-your-own-agent) · [How the graph is organised](#how-the-graph-is-organised) · [Status](#status) · [CLI](docs/cli.md) · [Python API](docs/python-api.md) · [Feature guide](docs/features.md) · [Base layer](docs/base-layer.md)

## Install

You need Python 3.12 or later built against SQLite 3.51.3 or later with FTS5 (`python -c "import sqlite3; print(sqlite3.sqlite_version)"` shows the version), plus [Claude Code](https://code.claude.com) or [Codex](https://openai.com/codex) signed in to your own account. The steps install [pipx](https://pipx.pypa.io) with Homebrew; on Linux, use your package manager.

```bash
brew install pipx
pipx install "stated[cli] @ git+https://github.com/zmccormick7/Stated.git"
stated ui
```

The first run creates a research folder at `~/Stated`, installs the bundled skills for your agent, and opens the Features view in your browser. To start from the published base layer instead of an empty graph, run it once with the base file: `stated ui --base base-2026.10.01.sqlite`. The [base layer guide](docs/base-layer.md) covers what it holds and what it leaves out: it is a starting point, and an edge is more likely to come from the data you bring.

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

Values come from three kinds of feature: **agentic**, read by an agent at the cutoff; **imported**, taken by code from a dated table of structured data such as XBRL financials; and **derived**, computed by formula. A fundamental model is built from them, with agentic and imported inputs and derived outputs. The [feature guide](docs/features.md#kinds-of-features) covers each, and what is built so far.

Run the commands in your research folder, or point `STATED_DB` at its database. A subjects file holds one Entity ID per line. Sources must carry the date they became public and be linked `about` their subject, since a source without a date is never seen at any cutoff.

To look over what a run produced, open `stated ui`: every definition with its schedule and coverage, then each subject's latest value, when it last changed and was last checked, and how many values are dated on or before the day the instructions were written, which may carry hindsight. A subject opens to its values over time, each opening to the evidence it rests on, with anything a cutoff on the value's date could not have seen marked.

## Use your own agent

Four skills teach an agent the graph. `stated ui` installs them; for any other harness, copy the folders under `stated/skills/` into its skills directory.

- **stated-research**: find what the graph holds, ingest and cite sources, record findings and revise them as evidence changes; every other skill uses it for its commands.
- **stated-feature-design**: turn an idea for something to measure into feature definitions whose instructions describe state as of a date rather than forecast, review existing definitions against the same tests, and decide which features share a run.
- **stated-state**: produce one feature's value for a subject as of a cutoff date, following the feature definition's instructions over everything a reader on that date could see, or computed by its formula from other features.
- **stated-model**: maintain a company's driver model as of a cutoff for a dynamic-model feature, whose output is the feature's value.

Point a session at the folder with `STATED_DB=/path/to/research.sqlite` and ask it to use the skill. The [CLI guide](docs/cli.md) covers every command, and the [Python API](docs/python-api.md) is there for agents and tools you write yourself:

```python
from stated import RecordStore

with RecordStore("research.sqlite", as_of="2024-03-31") as db:
    for record in db.search_records("customer concentration", kind="Finding"):
        print(record.as_of, record.text[:80])
```

Document search and the model-written document summaries use OpenRouter; set `OPENROUTER_API_KEY` in the agent's environment to turn them on.

## How the graph is organised

Five record types, connected by named links:

| Record | Holds |
| --- | --- |
| **Entity** | A company, facility, product or market. |
| **Source** | A filing, transcript, release or page, with its text captured so quotes can be checked, or a table of structured data released on one date, cited row by row. |
| **Finding** | A statement to build on or challenge, labelled disclosed, inference or assumption, citing its source. |
| **Model** | A company's driver model, its inputs and formulas dated by the cutoff each was written at. |
| **Variable** | A model input or output, or a feature value, with its value, units, periods and the evidence it rests on. |

Links such as `about`, `supported_by`, `depends_on`, `updates` and `contradicts` carry the reasoning: a Variable depends on a Finding, which is supported by a Source. Models are checked with `stated check-model`, which recomputes every output's formula from the stored inputs as they stood at the session's cutoff.

The graph is layered. A **base layer** is a published, read-only graph that any research folder can attach and later update; your own layer holds what your agents add on top, including corrections to base records. Knowledge belongs to the folder, not to any one conversation or harness: a fresh session, on either harness, picks up exactly where the last one left off.

The graph can also be read **as of a date**. Every record carries the date its contents are as of, and a session opened at a cutoff sees only the sources published by then, the disclosed facts those sources support, and what earlier sessions wrote at or before that cutoff; what it writes is stamped with the cutoff in turn. That is what lets agents describe a company's state at a past date from the evidence that existed then, with their work checkable against the dated record rather than against hindsight. [Features](#build-features) are built on this: each value is a Variable written at its date's cutoff, while the feature definitions are kept beside the graph rather than in it, since they are procedures, not knowledge.

## Status

Stated is early software. The database format changes without migrations; recreate a prototype database when it does.

What exists today:

- The CLI, the Python API and four skills, which teach an agent to work with the graph, design a feature, write a feature's value at a date and maintain a company's driver model at one. The skills work in Claude Code and Codex.
- Features end to end: definitions, schedules, `run-features`, derived features and the daily `feature-table`. The [oil and gas example](docs/examples/oil-and-gas/) runs three features weekly on 31 producers over 2023 and 2024 from their SEC filings, with the definitions, the exported table and its known limitations.
- Structured data as Sources: `stated ingest-table FILE.csv` keeps a CSV file byte for byte as one release, public from the date it carries, so a model input or feature value can cite the row it came from. Turning a table's rows into Variables still takes an agent or your own script.
- `stated ui`, a local read-only Features view: every definition with its schedule and coverage, each subject's values over time, and the evidence behind each value.
- A base layer covering the 1,000 largest US filers by public float: three years of their 10-Ks, 10-Qs and results releases in full text, each dated and linked to its company for point-in-time reads, and for about 470 of them a coverage pack of the business, segments, key figures, relationships, risks and open questions. The published file, `base-2026.10.01`, predates the quarterly reports and results releases; the next export carries them. Driver models exist for a handful of companies.
- Dynamic-model features: a company's driver model kept as of each cutoff, its inputs, drivers and formulas dated by the session that changed them, and its output the feature's value. None has been run at scale yet.

What is being worked on: a first run of a dynamic-model feature against a static template, quarterly updates as filings land, runs across many companies and years, and measuring how much hindsight still leaks into values.

## Contributing

Issues and pull requests are welcome. [AGENTS.md](AGENTS.md) sets out the conventions: one reviewable change per branch, tests for contracts and failure modes, and honest documentation of what is implemented. Run the tests from a checkout:

```bash
python -m unittest discover -s tests -v
```

## License and foundations

[MIT](LICENSE). The document-retrieval pipeline adapts ideas and code from [dsRAG](https://github.com/D-Star-AI/dsRAG), with storage ideas from [minDB](https://github.com/D-Star-AI/minDB); see [NOTICE](NOTICE).
