---
name: premisedb-state
description: Describe one company's state as of a cutoff date in a PremiseDB research graph, under a fixed schema the caller supplies, as a Model whose inputs are quoted from the filings a reader on that date could see and whose outputs are formulas, building on the state an earlier cutoff left. A description task, never a forecast.
---

# Describe a company's state as of a date

Use this when the session was opened at a cutoff (`PREMISEDB_AS_OF` is set, and every `premisedb` command reads and writes as of that date) and the task names a company and a schema. Use the premisedb-research skill for every command; this skill says what to do with them, in what order, and what not to do.

The task is a description, not a judgment. Every number you record is either quoted from a filing or computed by a formula from quoted numbers, so that a reader can check each one against the filing, and so that knowing what happened to the company later gives you nothing useful to do. Use the same procedure for every company and every date; your judgment goes into how to read a filing, never into which company deserves a closer look or what its figures mean.

## The schema

The caller supplies the schema: a name, such as `bank-state-v1`, and a list of fields. Each field has a `key`, a definition precise enough that two readers of the same filing would record the same figure, its units, and either "input", a figure a filing states, or "output", a `formula` over other keys. A field that spans dates is a row with named `periods`. Apply the schema as written:

- Do not add fields, except the component inputs a sum or a formula needs to be checkable, named `<field>_<part>`, with the field itself an output whose formula sums them.
- Do not leave a field out because it seems irrelevant to this company. A zero the filing states is a disclosed zero. A figure the filing does not give is undisclosed, not zero.
- Where a definition is ambiguous for this filing, choose the reading closest to its words, say in the Variable's text which reading you took and what the alternative would give, and keep the schema's key.

## 1. Orient

- `list --kind Source` shows the filings a reader on the cutoff date could see; nothing filed later exists for you, and there is nothing to fetch. `search` the company by name and ticker for its Entity, then find its filings among the Sources by its name in their descriptions. The definitions say which filing and which date each figure comes from; the latest filing you can see is usually the one.
- `list --kind Model --where schema=<schema>` and `recall "<company> state <schema>"` find a state an earlier cutoff left for this company. If one exists, `expand MODEL_ID --relation in_model` for its Variables and `connections VARIABLE_ID` for the Findings they rest on. That work is yours to build on, not to redo.

## 2. Find the figures

- Search within the filing: `search-documents --source-id SOURCE_ID "<the line's words>"`, then `source-text` around the hit for the exact table row or sentence. Balance-sheet lines recur in the notes and the parent-company statements: when a quote occurs more than once, `locate SOURCE_ID QUOTE` lists the offsets, and the citation takes the `start_byte` of the one you mean.
- A figure stated in billions or thousands is stored in the schema's units; the Finding's text gives both ("$25.9 billion, i.e. 25,900"), and the quote shows the figure as stated.
- When the earlier state's Finding is still the latest disclosure of a figure, the same period from the same filing, do not write a new Finding: link your Variable `depends_on` the earlier one.
- When a figure is undisclosed, give the Variable `"value": null`, `"basis": "undisclosed"`, and a text that says what was looked for and where: the sections read and the searches run. Never estimate it, never take it from a filing the cutoff hides, and never take a figure for a different date than the definition names unless the definition allows the latest available. Outputs that need it come out `null` by themselves.

## 3. Write the state

- One Model per company and cutoff: text "State of <company> as of <cutoff> under <schema>", linked `about` the Entity, with attributes `{"schema": "<schema>"}`. PremiseDB stamps it with the cutoff; do not record the date yourself.
- One Variable per field, linked `in_model`, with `key`, `value`, `units`, `period` or `periods`, `role` ("input" or "output") and `basis`: `disclosed` for a quoted figure, `derived` for a formula, `undisclosed` for a null. Each disclosed input is linked `depends_on` a Finding with `basis: disclosed`, linked `about` the Entity, whose text states the figure with its period and units in a sentence and which cites the exact passage. Each output carries its `formula` and is linked `depends_on` every Variable the formula names.
- When an earlier state exists, link each of your Variables `updates` the earlier Variable with the same key. The earlier state stays true of its own date; a reader at that date must see it unchanged, and PremiseDB refuses edits to records written at another cutoff. Revise by adding, never by patching.
- Compose the whole state as one `contribute` payload, written to a file with your file-writing tool rather than a shell heredoc, naming new records by `ref`. Give outputs `"value": null`; then run `fill-model MODEL_ID --input FILE` (payload `{"author_id": ...}`) to store each output's exact value from its formula, and `check-model MODEL_ID`, which must report no problems and `exact` true for every output. A contribution that fails writes nothing; fix the citation it names and resend.

## 4. Stop

Stop when every field has a value or is marked undisclosed with its reason. Do not add Findings, Questions or Variables outside the schema, and do not write any conclusion about the company. Finish with a short report: the Model ID; a table of each key, its value, its period and its basis; which inputs were undisclosed and why; which Variables build on the earlier state; and anything in the schema that was ambiguous for this filing, which is a note for the schema's author, not a reason to depart from it.

## Rules

- Checkable or not written. A figure you cannot quote or compute from quotes does not go in the graph.
- Nothing from after the cutoff. Not a figure, not a restatement, not a word about what happened next. If you know something about this company that the filings you can see do not say, it has no place here.
- Same procedure for every company. The schema decides what to measure; you decide only how to read each filing.
