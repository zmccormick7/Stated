---
name: premisedb-answer
description: Answer a question from a PremiseDB research graph as a structured answer document (a one-line title, a body paragraph, cited sources and optional chart or quote blocks) that passes `premisedb check-answer`.
---

# Answer with PremiseDB

Use this when the reply will be rendered as an answer: a headline, a paragraph, and at most a few supporting blocks. Do the research with the premisedb-research skill first (recall, read the evidence, check or evaluate models, and add to the graph what the answer needs). Then write the answer as one JSON object.

## The document

```json
{
  "title": "Two customers were 36% of revenue.",
  "body": "In fiscal 2026 one direct customer was 22% of revenue and another 14%. [1] A year earlier the three largest were 12%, 11% and 11%. The filing does not name them.",
  "sources": [
    {"record": "1f323139", "quote": "sales to one direct customer represented 22% of total revenue"}
  ],
  "blocks": [
    {"type": "chart", "unit": "% of revenue", "whole": 100, "rest_label": "Everyone else",
     "rows": [{"label": "Fiscal 2026", "parts": [
       {"label": "Largest customer", "record": "1f323139", "value": 22, "as_written": "22%"},
       {"label": "Second customer", "record": "1f323139", "value": 14, "as_written": "14%"}]}]},
    {"type": "quote", "source": 1}
  ]
}
```

- **`title`** is the answer itself in one line of at most 120 characters: a statement that replies to the question, not a topic.
- **`body`** is one short paragraph that explains the title. Cite with `[n]`, the number of an entry in `sources`.
- **`sources`** lists only the records the answer cites, each by ID (a unique prefix of 8 or more characters works). An optional `quote` is exact text from the record or from a passage it cites. Every source must be cited by a `[n]` or shown by a quote block.
- **`blocks`** is optional. Most answers need none.

Those four keys are the whole format; anything else is refused. There is no place for caveats, methods or next steps. A limit that changes the answer belongs in the body's own sentences; otherwise leave it out.

## Blocks

Add a block only when it changes what the reader understands. At most six, and usually one or two.

- **`text`**: a further paragraph, `{"type": "text", "text": "…"}`, with `[n]` citations.
- **`quote`**: shows a source's quote in full, `{"type": "quote", "source": 1}`. Use it when the exact words matter.
- **`lever`**: one model input to drag and one output to watch, recomputed live through the model: `{"type": "lever", "model": MODEL_ID, "input": "accel_supply_growth_cy2027", "output": "dc_rev_fy28", "input_label": "Supply growth, 2027", "output_label": "FY28 Data Center revenue, $B"}`. Both keys name Variables of that model with a single numeric value; the output should have a formula so it moves. The slider spans the Variable's `low` and `high`, or a `range` of `[low, high]` you give. Use it when the answer rests on an assumption the reader would want to push on.
- **`flow`**: who depends on what, in proportion: a `from` node (`{"label", "note"?}`), `parts` drawn as a stacked column (each a figure as in a chart, with an optional `note`), and an optional `to` node (`{"label", "note"?, "items"?}`) for what sits downstream. `unit`, `whole` and `rest_label` work as in a chart. Use it for allocation and dependency questions.
- **`chart`**: labelled bars. `rows` is a list of `{"label", "parts"}`; one part draws a plain bar and several draw a stacked one. Give the `unit`. With `whole` (such as 100), each row's remainder is drawn and named by `rest_label`. Optional `caption`. Set `"lead": true` on at most one block to make it the centre of the answer.

## Figures

Every number a chart draws names the record it comes from, so a chart cannot show a figure the graph does not hold.

- **From a Variable**: `{"label": "FY28 revenue", "record": VARIABLE_ID}`, plus `"period": "2027"` when its value is a row. The value is read from the graph; do not supply it.
- **From any other record**: give `value`, and `as_written`, the figure exactly as it appears in the record's text or in a passage it cites: `{"label": "Largest customer", "record": FINDING_ID, "value": 22, "as_written": "22%"}`. When the chart's unit differs from the record's, add `scale`: `"as_written": "$193,479", "scale": 0.001, "value": 193.5` turns USD millions into billions. `as_written` times `scale` must equal `value` to within 0.5%.
- **Derived numbers** are not supplied. Use `whole` for a remainder. For anything else, state it in the body, or write a Finding or Variable that holds it and cite that.

## Check, then return

Write the document to a file and run `premisedb check-answer --input answer.json`. It returns `ok`, `problems` and, when ok, the resolved `answer`. Fix every problem and run it again. Your final message is the JSON document alone, with no prose around it.
