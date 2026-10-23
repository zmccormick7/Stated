# Building point-in-time features

PremiseDB can turn a research graph into features for systematic models: numbers, counts, yes/no answers and categories that describe a company, a market or anything else as of a date, built by agents reading what was knowable on that date. This guide explains the approach, how it maps onto the graph, how to write a feature, and what PremiseDB does and does not do about lookahead bias.

## Describe, don't predict

A model trained on text through its cutoff knows which deals closed, which drugs failed and which firms blew up. Asked in a 2021 backtest whether a merger will close, it may simply remember the answer, and that memory looks exactly like skill. Telling the model to answer "as of March 2021" does not make it forget, and masking names leaves amounts, industries and wording that still identify the case.

So agents are never asked to forecast. The work splits in two:

1. **State (agents).** At a date *t*, agents turn what was knowable at *t* into a structured description of the subject: one feature value per feature. The right value is fixed by evidence dated on or before *t*.
2. **Prediction (trained).** A separate model, fit walk-forward, maps those descriptions to outcomes. Its training window is under your control.

This works because in a description task, knowledge of the future can only show up as error; in a prediction task it shows up as skill. When the right answer is fixed by the evidence on the date, an agent that knows how things ended gains nothing by using that knowledge. All forecasting moves into the model whose training window you control.

**Example.** SVB Financial Group filed its 2022 annual report on February 24, 2023 and failed on March 10. "How likely is this bank to get into trouble?" asked at February 24 is answered by memory. "What share of deposits is outside FDIC coverage?" is not: $165.4B of $173.1B, or 96%, assembled from estimated uninsured US deposits ($151.5B) and foreign deposits ($13.9B), which the filing reports separately. Someone who knows SVB failed would get the same answer as someone frozen on February 24. The same procedure runs on every listed bank, and the predictive model learns how states like that one have tended to play out.

## How features live in the graph

| What | In PremiseDB |
| --- | --- |
| A feature | A **feature definition**, kept beside the graph (`premisedb define-feature`): the instructions an agent follows, the `key` its values carry and their `type`: `number` (optionally within `min` and `max`), `count`, `boolean`, or `category` with its `categories`; optionally `units`, and a `formula` for a derived feature. |
| A subject | An **Entity**: a company, a market, a sector, a deal, whatever the instructions describe. |
| A value | A **Variable** whose `feature` attribute is the definition's ID, linked `about` its subject, carrying the feature's `key` and a `value` of its type, linked `depends_on` the Findings and Sources it was read from. PremiseDB refuses a value outside the type; nothing else about the value is checked. |
| The date | The session's cutoff. A session opened with `PREMISEDB_AS_OF=2023-02-24` sees only Sources published by then and what earlier sessions wrote at or before then, and stamps everything it writes with that date. |
| A change | A new Variable linked `updates` the previous value, which keeps its own value and date. |
| A derived feature | A definition with a `formula` over other features' keys. `premisedb fill-model ENTITY_ID` computes every derived value about a subject from the latest version of its inputs. |
| The feature table | `premisedb state` exports values as change points per subject: a row whenever some feature changed. A daily series is a forward fill of those rows. |

Definitions are procedures, not knowledge, so they live beside the graph rather than in it: every cutoff reads them alike, they never turn up in search, and each records `written_at`, when its instructions were fixed. A definition can be edited until a value names it. After that, new instructions for its key are a new definition that `replaces` it, so every value stays tied to the instructions it was made under.

The [premisedb-state skill](../premisedb/skills/premisedb-state/SKILL.md) teaches an agent to produce one value: given a feature, a subject and a cutoff, read what is visible, write the value or leave the previous one standing, and leave derived features to `fill-model`.

## Writing a feature

A feature is a task: the instructions and output type for one agent run on one subject at one date. The best tasks demand a lot of intelligence while giving hindsight nowhere useful to go. Sophistication is not what leaks; dated knowledge is. Knowing what a springing covenant does, how revenue recognition works or what a central bank's conditional language means is the point of using an LLM. Knowing what later happened to this firm, deal or drug is the only thing that leaks.

A feature belongs in the state when it passes these tests:

- **Grader invariance.** Someone who knows what happened next would score the answer the same as someone frozen at the date.
- **Timeless, not dated, knowledge.** The work needs legal, accounting or economic expertise, not recall of the subject's later history.
- **Traceable.** Every value rests on what a reader at the date could see, linked so a reviewer can follow it, and a derived value can be recomputed from its inputs.
- **Ingredients, not judgments.** Product lists, not "competitors"; stated conditions, not "hawkishness"; guidance and its track record, not "credibility". The predictive model does the judging.
- **Same procedure for every subject.** The instructions decide how to measure, applied identically to every subject in the universe; they never decide where to dig for a particular one.

Values do not have to be figures a document states. Most good features are constructed: SVB's equity after marking its held-to-maturity securities to market appears nowhere in its filing, and has to be built from the securities note, a tax rate and the equity section. The type keeps the answer space closed. Prefer a number with units, a count, a yes/no or a fixed list of categories to a score: "relevant", "material", "key risk", "surprising" and 1–10 ratings are partly defined by what turned out to matter. Break them into fixed sub-answers instead, each its own feature.

Agents add the most where the state exists in no single document and has to be assembled, checked or computed:

| Kind of work | What the agent does | Example feature |
| --- | --- | --- |
| Reconstruction | Builds an object no document contains | Debt maturing within two years across every indenture and amendment |
| Implied expectations | Solves for what prices and consensus at the date assume | Unit growth implied by the share price, against the company's and its peers' history |
| Triangulation | Checks one source against independent others | Management claims counted as corroborated, unverifiable or contradicted |
| Consistency across a value chain | Reconciles what related companies say | Whether a distributor reports high channel inventory while its supplier reports robust demand |
| Absence | Compares disclosures with history and peers | Whether a KPI reported for the last four years stopped being reported |
| Exposure screening | Applies fixed criteria across the universe when a shock hits | Share of revenue from a newly tariffed country, as disclosed |
| Situation assembly | Reports where a process stands | Approvals still outstanding for a pending deal, and the days left on each statutory clock |

Write the instructions as you would brief an analyst who will run them on every subject: what to measure, where it is usually disclosed, how to handle the common variations, the units and period, and when the answer is `null` because nothing visible bears on it. Do not name a subject, an event or an outcome the instructions were inspired by.

## Lookahead: what PremiseDB handles and what it doesn't

The danger is a feature that is point-in-time in form but predictive in substance, or a process that lets the future in around the edges. The leaks, and where each is handled:

| Leak | What PremiseDB does | What is left to you |
| --- | --- | --- |
| **Outcome-defined judgments**: "material", "key risk", scores | Definitions restrict values to numbers, counts, yes/no and fixed categories. | Writing instructions that ask for ingredients. |
| **Environment**: live search, current pages, restated data, today's ticker lists | A session at a cutoff sees no Source published later, no record written later and no model-written document summary; it cannot ingest from the web or link to anything hidden. | Running sessions with web access off; ingesting documents as filed, with their publication dates, not restated data; including subjects that later failed or were delisted. |
| **Navigation**: digging into a firm that later blew up | A feature is one procedure; the universe of subjects is chosen outside the session. | Running every feature on the whole universe, not on subjects you have a hunch about. |
| **Analog selection**: choosing the comparisons | Nothing. | Writing the similarity criteria into the instructions, so retrieval is mechanical. |
| **Outcome-correlated error**: more careful work on firms that later became famous | Nothing; it can only be measured. | The audits below. |
| **Researcher lookahead**: designing features around what turned out to matter | Each definition records when it was written, and its instructions are fixed once values name it. | Deriving features from theory (run risk, asset–liability mismatch), fixing them in advance, and treating dates after a definition was written as its honest test. |

Leakage cannot be proven zero. The case is structural, backed by audits:

1. **Design.** Features give hindsight no useful outlet, so leakage that does occur shows up as error against the evidence.
2. **Provenance.** Every value rests on records a reader at its date could see; a session at a cutoff cannot link to anything later.
3. **Invariance.** Running the same task with names masked, with documents edited, or a second time should not change the answers. Repeat runs can be written to a separate layer and compared.
4. **Post-cutoff holdout.** Dates after the agent model's training cutoff are the only assumption-free test: feature quality and the feature-to-outcome relationship should hold there as well as before. Name the agent model in each session's `author_id` so values can be split by it.

## Building the history

1. **Collect documents as filed.** Ingest each filing, transcript or release as a Source with `published` set to the date it became public, and ideally `document_type`. A Source without a publication date is never seen at any cutoff.
2. **Create the subjects.** One Entity per subject, including those that later failed, merged or were delisted.
3. **Define the features**, without a cutoff, before any values. Each definition's `written_at` records when its instructions were fixed.
4. **Run sessions at cutoffs.** For each feature, subject and date, open a fresh agent session with `PREMISEDB_AS_OF` set to the date, web access off, and the premisedb-state skill. Sessions for the same feature and subject run in date order, since each starts from the previous value; different subjects can run in parallel. Then run `fill-model` for derived features.
5. **Export** with `premisedb state` and forward-fill to the frequency your model needs.

For example, a feature defined once:

```bash
premisedb define-feature --input - <<'JSON'
{"author_id": "researcher", "key": "uninsured_share", "type": "number", "min": 0, "max": 1,
 "units": "fraction of total deposits",
 "instructions": "Share of the bank's total deposits not covered by deposit insurance, as of the latest balance sheet the bank has reported. Add estimated uninsured domestic deposits to foreign deposits, which are uninsured, when they are disclosed separately; divide by total deposits. Use the bank's own estimates. Null when the bank has not disclosed an uninsured estimate."}
JSON
```

and a value written by a session at February 24, 2023:

```bash
PREMISEDB_AS_OF=2023-02-24 premisedb contribute --input - <<'JSON'
{"author_id": "feature-run/claude-opus-5-5",
 "records": [{"ref": "v", "kind": "Variable",
              "text": "Uninsured US deposits of $151.5B plus foreign deposits of $13.9B, of $173.1B total, at December 31, 2022.",
              "attributes": {"key": "uninsured_share", "feature": "FEATURE_ID", "value": 0.955, "units": "fraction of total deposits", "period": "2022-12-31"}}],
 "links": [{"from_id": "v", "to_id": "SVB_ENTITY_ID", "relation": "about"},
           {"from_id": "v", "to_id": "FINDING_ID", "relation": "depends_on"}]}
JSON
```

`premisedb state --feature uninsured_share --from 2021-01-01 --to 2023-12-31` then gives each bank's values at the start of the range and at every date one changed, from its first value on.

## Not built yet

These parts of the workflow are designed but not yet in PremiseDB; until they are, they are scripts you write around it:

- A schedule on each feature definition and a list of the (feature, subject, date) sessions still to run.
- A runner that works through that list, opening sessions at each cutoff.
- Reports for repeat-run agreement and for values before and after an agent model's training cutoff.
- A daily feature table; `state` exports change points.

Training the predictive model is outside PremiseDB: the feature table is where it starts.
