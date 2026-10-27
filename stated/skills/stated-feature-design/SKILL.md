---
name: stated-feature-design
description: Design or review feature definitions in a Stated research graph, the instructions and value type agents follow to describe a subject (a company, a market or anything else) as of a date for systematic models. Turns a measurement idea into one or more features that describe point-in-time state, never forecast, and give hindsight nowhere to go.
---

# Design a feature

Use this when asked to turn an idea for something to measure into features, or to review existing ones. Use the stated-research skill for every command. A feature definition, kept beside the graph rather than in it, holds the instructions an agent follows to produce one value for one subject as of one date, using only what was visible on that date, the `key` its values carry and their `type`. Another agent, following the stated-state skill, reads only the definition and the documents, and writes each value as a Variable with the value, its `period` and a text saying what it rests on. A separate model, trained walk-forward on the values, does all the predicting and all the combining. Design each feature so that someone who knows what happened next would give the same answer as someone frozen on the date.

**1. Start from what exists.** `features` lists the current definitions, and `feature KEY` shows one in full; reuse one rather than writing a near-duplicate. A key has one current definition, and Stated refuses to define it again. `update-feature ID --input FILE` rewrites a definition that no value names yet (`values` is 0). One that values name cannot change: new instructions for its key are a new definition with `replaces` set to the current one's ID, or a new key.

**2. Ask for ingredients, not judgments.** Words like "risk", "power", "quality", "material", "credible", "surprising" and 1–10 scores are partly defined by what turned out to matter. Break each into fixed sub-answers a careful reader could give from the documents: product lists, not "competitors"; stated conditions, not "hawkishness"; guidance and how past guidance compared with results, not "credibility". Each sub-answer is its own feature. Do not add a feature that combines them into a score; weighing the pieces is the predictive model's job. Ask for the state on the date, which includes what the latest documents say; never ask what will happen, or for a probability.

**3. Pick the type.** Every value is `null` or one of:
- `number`, with `units` and, where the scale has limits, `min` and `max` (a share is 0 to 1);
- `count`, a whole number of things the instructions define precisely;
- `boolean`, for a question with a clear yes or no;
- `category`, with `categories` for every state a subject the evidence describes can be in, each defined in the instructions.

A value need not be a figure any document states: most useful features are assembled from several disclosures, compared across periods or computed. When the documents give only a bound (a customer is "more than 10%" of revenue), pick a type that can hold what is known, such as a boolean or a category of ranges, rather than a number that will mostly be null. Units must mean the same for every subject: prefer ratios and shares, and when a level is needed, state the currency and scale.

**4. Say what silence means.** Most hard cases are about what to record when the documents do not say. Write the rule into every definition: when the answer is `null` (nothing visible bears on it, the documents the instructions name are not visible, or the feature does not apply to this kind of subject, such as receivables for a bank), and when silence is itself the answer, as `false` or `0`, because the documents read would have said so (a required disclosure that is absent). For a value resting on an absence, the instructions should ask the agent to name in the value's text the documents it read.

**5. Write the instructions for every subject.** The text is all the value-writing agent sees, so each definition must stand alone: repeat shared terms (who counts as one customer, how to treat restatements) in each one that needs them. Write it as a brief to an analyst who will apply it identically to every subject in the universe: what to measure; which documents to read; the period the value describes and how to label it (`period`, as the documents label it, such as "FY2025", "FY2026Q2" or a balance-sheet date), which may be longer than how often values are made; where it is usually disclosed and how to handle common variations in reporting; restatements and conflicting sources; and the silence rule from step 4. Derive the feature from general theory and standard practice (run risk, asset–liability mismatch, customer concentration, the accounting standards), not from a case: do not name a company, event or outcome that inspired it, and do not tell the agent where to dig for a particular subject. When the feature compares a subject with others, write the criteria for choosing them, so the choice is mechanical rather than the agent's.

**6. Derived features.** A feature that is arithmetic on other features gets a `formula` over their keys, using `+ - * /`, `**`, comparisons, `a if condition else b`, `min`, `max`, `abs` and `fill(x, v)`; define each input as a feature of its own, with the same period, so the latest values line up. Stated computes it, after each run or with `derive-features`, at every date an input changed, from the latest value of each input about the same subject; it is never written by an agent, and it has no schedule. A formula that reads a `null` input yields `null`; when the inputs carry different `period`s, nothing is computed for that date.

**7. Check it against the tests.** Before writing the definition, confirm each and revise until it passes:
- *Grader invariance*: knowing what happened next would not change the right answer.
- *Timeless, not dated, knowledge*: the work needs accounting, legal or economic expertise, not recall of the subject's later history.
- *Traceable*: the value can rest on documents visible on the date, named or cited so a reviewer can follow it, and a derived value can be recomputed.
- *Same procedure everywhere*: nothing in the instructions depends on which subject it is.

**8. Write it.** `define-feature --input FILE` with `author_id`, `key` (a short snake_case name), `instructions`, `type`, and `units`, `min`, `max`, `categories` or `formula` as they apply. Stated checks the key, the type and its fields, and that a formula parses; it does not check that a formula's keys are other features, so check that yourself. Define features before any values exist, so each definition's `written_at` records that its instructions were fixed in advance.

When reviewing, report for each definition which test or step it fails and the revision that would pass, and write revisions as new definitions rather than editing one that values name. Finish with each definition's ID, key and type, and a sentence on what it measures.
