---
name: premisedb-state
description: Describe one company's state as of a cutoff date in a PremiseDB research graph, under a schema the caller supplies, as a Model whose inputs are quoted from the filings a reader on that date could see and whose outputs are formulas, building on the state an earlier cutoff left. A description, never a forecast.
---

# Describe a company's state as of a date

Use this when the session was opened at a cutoff (`PREMISEDB_AS_OF` is set, so every `premisedb` command reads and writes as of that date) and the task names a company and a schema. Use the premisedb-research skill for every command. The schema supplies the fields, their definitions, units and formulas; apply it as written to this company, as you would to any other.

**1. Describe, do not judge.** Every number you record is either quoted from a filing a reader on the cutoff date could see, or computed by a formula from such numbers, so that each can be checked against the filing. A figure the filings do not give is `null` with `basis: undisclosed` and a text saying what was looked for; never estimate it. Write nothing about what the figures mean, nothing from after the cutoff, and nothing you know about the company that the filings you can see do not say. `list --kind Source` shows what you can see; there is nothing to fetch.

**2. Build on the earlier cutoff's state.** `list --kind Model --where schema=<schema>` finds the state an earlier cutoff left for this company; `expand MODEL_ID --relation in_model` shows its Variables and the Findings they rest on. Write a new Model for your cutoff, link each of your Variables `updates` the earlier Variable with the same key, and where a figure is unchanged, link your Variable `depends_on` the earlier Finding instead of writing a new one. Never edit the earlier state: a reader at that date must see it as it was, and PremiseDB refuses the edit.

**3. Write it as one checkable model.** One Model per company and cutoff, `about` the Entity, with attributes `{"schema": "<schema>"}`; one Variable per field, `in_model`, with `key`, `value`, `units`, `period` or `periods`, `role` and `basis` (`disclosed`, `derived` or `undisclosed`). Each disclosed input is linked `depends_on` a Finding that states the figure with its period and units and cites the exact passage; each output carries its `formula` and is linked `depends_on` every Variable it names. Compose it as one `contribute` payload, written to a file with your file-writing tool, with outputs' values `null`. Then run `fill-model MODEL_ID` to store each output's exact value from its formula, and `check-model MODEL_ID`, which must report no problems and `exact` true for every output.

Finish with the Model ID, a table of each key with its value, period and basis, and the inputs that were undisclosed and why.
