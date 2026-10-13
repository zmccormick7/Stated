---
name: premisedb-go-deeper
description: Deepen a PremiseDB research graph's understanding of one company past its standard coverage: build or extend a driver model, turn its unsourced inputs and the answerable open Questions into a research list, and work that list from filings, releases and transcripts until the model's inputs are sourced.
---

# Go deeper on a company

Use this when the graph already holds a company's standard coverage (its business, segments, key figures, relationships, risks and open Questions) and the task is to understand it better. Use the premisedb-research skill for every command; this skill says what to do with them and in what order.

Depth has a definition here. A company is understood when the graph holds a driver model whose inputs are sourced from documents, with their recent history, and when the open Questions that bear on those inputs have been worked. More Findings of the kind the coverage already has do not add depth.

## 1. Orient

- `recall` the company by name, then `get ENTITY_ID` and `connections ENTITY_ID --relation about` for its Findings and Questions. Read them; they are the starting figures.
- `list --kind Model` and `expand MODEL_ID --relation in_model` for any model already about the company. Extend an existing model rather than writing a second one.
- Decide the business type, because it fixes what drives value: volume, price and margin for a manufacturer or retailer; earning assets, net interest margin and fee rates for a bank; premiums, combined ratio and investment yield for an insurer; units, occupancy and rent for a REIT; subscriptions, net retention and margin for a software company; production, realized price and unit cost for a producer. Name the type in the Model's text.

## 2. Write the driver model

- One Model record, `about` the company. Its text says what it forecasts, for which periods, and the business type.
- Eight to twenty Variables, each `in_model`. Inputs are the quantities an analyst would argue about; outputs are revenue, profit and cash figures computed from them with a `formula`. A full three-statement model is not the goal; a model with three drivers that a reader can move is worth more than one with fifty that nobody can source.
- Every input carries `key`, `value`, `units`, the period or a `periods` row, and a `basis`: `disclosed` when a document states it, `assumption` when you set it. A disclosed input is linked `depends_on` the Finding that states it, and that Finding cites the document. An assumption's text says why that value and what would change it.
- Where a quantity has a history, store it as a row: `value` and `periods` aligned, the last three to five years or eight quarters, then the forecast periods. A row that mixes sourced history and a forecast has `basis: assumption` for the forecast, is linked `depends_on` the Findings that source its history, and says in its text which periods are sourced and which assumed. Levers and charts rest on these rows.
- Store outputs unrounded. Run `check-model MODEL_ID` and fix every problem before going on; a model that does not check is not finished.

## 3. Make the research list

- Every input whose basis is `assumption`, whose value rests on one period only, or whose source is a summary rather than the document itself, is a gap. For each, write a Question with `status: open`, `about` the company, whose text names the Variable's `key` and says exactly what document would settle it.
- Read the company's existing open Questions and sort them: those a public document could answer, and those only management could. Work the first kind, starting with any that bears on a model input. Leave the second kind open and untouched; they are what a reader watches for, not work for you.

## 4. Work the list

- Sources, in order: the filing text already in the graph (`search-documents` and `source-text`), then the latest quarterly reports and earnings releases on EDGAR, then earnings-call transcripts and investor presentations, all through `ingest --url`, which reads HTML and PDF. Chunk what you ingest.
- An answer is a disclosed Finding with an exact citation, linked `answered_by` from the Question and `about` the company. Then `patch` the Variable with the sourced value and `basis: disclosed`; when it already had a value, `patch` returns a new version of it, so link the Variable `patch` returned `depends_on` the Finding, and set the Question's `status` to `answered`. A partial answer uses `partly_answered_by` and `partly_answered`.
- Evidence that conflicts with a stored value gets a Finding linked `contradicts` the Variable, not a silent edit. Evidence that narrows it gets `qualifies`.
- After each round of changes, `check-model` again. Outputs change when inputs do; stored output values must follow.

## 5. Stop

Stop when every input is sourced, or marked as an assumption with its reason, or when the budget is used. Do not fill remaining time with Findings outside the model. Finish with a short report: the Model's ID, inputs sourced and inputs still assumed, Questions answered and Questions opened, documents ingested, and what the next session should take up first.

## Rules

- A disclosed Finding states its figure, with period and units, as the coverage Findings do: "second-quarter 2026 raw materials external sales were $742 million", not "the 10-Q discloses raw materials external sales". A Finding without its number is useless to a reader and cannot source a Variable. What stays out of prose is the model's own numbers: an output, a derived figure or a scenario value lives in a Variable, and a Finding about it states the conclusion in words and links to the Variable, because a copied model value goes stale silently.
- Never invent a value. An input you cannot source is an assumption, labelled as one, with a range (`low`, `high`) when the documents bound it.
- Do not create an Entity for the company's own brands or units, and reuse existing Entities for counterparties.
- Keep the research skill's citation rules: exact quotes, located in the stored text, one Finding per fact, self-contained text with company, period and units.
