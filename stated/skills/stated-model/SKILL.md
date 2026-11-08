---
name: stated-model
description: Maintain a company's driver model as of a cutoff date for a dynamic-model feature in a Stated graph. Build it from what was visible on the first date, update its inputs and change its structure only where the visible record calls for it, and let its output be the feature's value.
---

# Maintain a driver model as of a date

Use this from the stated-state skill when a feature's instructions say its value is the output of the subject's driver model. The session is at a cutoff (`STATED_AS_OF` is set), so every command reads and writes as of that date and sees the model as it stood then. Use the stated-research skill for every command.

**1. Find the model.** `expand ENTITY_ID --relation about` shows the Model about the subject, if there is one. `check-model MODEL_ID` lists its outputs and `expand MODEL_ID --relation in_model` its Variables, as of this cutoff: the latest version of each key, with retired drivers left out. With no Model yet, build one (step 2); otherwise update it (step 3).

**2. Build it on the first date.** Follow the feature's instructions on the business type and the output.
- One Model record `about` the Entity, whose text names the business type and what drives the output.
- Inputs: Variables with `role: input`, a `key`, units, `period` and the value the visible documents give, each linked `in_model` the Model and `depends_on` the disclosed Findings, with citations, it rests on. A value the documents do not give stays `null`. Do not assume one.
- The output: one Variable with the feature's `key`, `feature` set to the definition's `id`, `role: output` and a `formula` over the inputs' keys, linked `in_model` the Model, `about` the Entity and `depends_on` each input it names.
- Use the drivers the company discloses. A model with three sourced drivers is worth more than one with ten guessed ones.
- Run `fill-model MODEL_ID --input FILE` with your `author_id`, then `check-model MODEL_ID`, and fix what it reports.

**3. Update it on later dates.** Read what was published since the model's last date: `list --kind Source` and `search-documents` show what you can see.
- An input the new documents give differently: `patch` its `value`, which writes a new version dated this cutoff, and link the new evidence.
- A driver the company now reports and the output should use, such as a new segment or product line: create the input linked `in_model`, then `patch` the output's `formula` to use it (a new version) and link the output `depends_on` it.
- A driver the company no longer reports, or that no longer applies: `patch` it with `retired: true` and take it out of the formula.
- Change the structure only when the visible documents call for it, never because of what happened later. Leave a model that still fits as it is.

Then run `fill-model MODEL_ID`. If the output changed, it wrote the output's new version, and that version is the feature's value for this date. If nothing changed, `confirm` the output's latest version as the stated-state skill says.

**4. Describe, do not forecast.** The output is a figure the model computes from disclosed drivers as of the date: revenue at the latest disclosed volumes and prices, say, or a segment's share of revenue. It is one value of the feature's type, not a row of future periods. Management guidance may be an input, labelled as guidance, never as your expectation.

Finish with the Model's ID, the output's value and the ID of its latest version, and what changed in the model and why.
