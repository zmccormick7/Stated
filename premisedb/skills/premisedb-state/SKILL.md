---
name: premisedb-state
description: Compute one feature of one company as of a cutoff date in a PremiseDB research graph, from the instructions the caller supplies, as a Variable whose value is quoted from the filings a reader on that date could see or computed by a formula from such values. A description, never a forecast.
---

# Compute a feature as of a date

Use this when the session was opened at a cutoff (`PREMISEDB_AS_OF` is set, so every `premisedb` command reads and writes as of that date) and the task names a company, a feature and its instructions, and usually the document that arrived on that date. Use the premisedb-research skill for every command. The instructions define the feature: its name, what it measures, its units, which filing it comes from, and for a computed feature the formula over other features. Apply them as written to this company, as you would to any other.

**1. Describe, do not judge.** The value is either quoted from a filing a reader on the cutoff date could see, or computed by a formula from such values, so that it can be checked against the filing. A figure the filings do not give is `null` with `basis: undisclosed` and a text saying what was looked for; never estimate it. Write nothing about what the figure means, nothing from after the cutoff, and nothing you know about the company that the filings you can see do not say. `list --kind Source` shows what you can see; there is nothing to fetch.

**2. Write the value as a Variable about the company.** Find the company's Entity with `search`. The feature value is one Variable linked `about` the Entity, with `key` (the feature's name), `value`, `units`, `period` or `periods`, and `basis`: `disclosed` for a quoted figure, linked `depends_on` a Finding that states the figure with its period and units and cites the exact passage; `derived` for a computed one, carrying its `formula` and linked `depends_on` the Variable holding each feature the formula names, the latest version about this Entity (`list --kind Variable --where key=KEY` shows the versions; take the latest). Then `check-model VARIABLE_ID` must report no problems and `exact` true, or for a derived feature leave `value` null and run `fill-model VARIABLE_ID`, which computes and stores it.

**3. Change only what the document changes.** `list --kind Variable --where key=<feature>` shows the feature's earlier values for this company; the latest is the value as of the day before. If the new document does not bear on it, write nothing. If it does, write the new value as a new Variable, linked `updates` the latest earlier one, with its own Finding; never edit an earlier version, since a reader at that date must see it as it was, and PremiseDB refuses the edit. Derived features that depend on a changed value are not yours to redo: `fill-model ENTITY_ID` writes their new versions.

Finish with the Variable's ID, its value, period and basis, or the reason it is undisclosed.
