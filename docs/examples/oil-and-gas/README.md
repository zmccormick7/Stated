# Oil and gas producers, 2023–2024

This example uses Stated to measure three things about 31 US oil and gas producers, every week of 2023 and 2024, from their SEC filings:

- how far each company's production guidance for the year has moved from the first figure it gave,
- whether it says how much drilling inventory it has left, and
- what share of its chief executive's annual bonus is tied to growing production or reserves.

Each week's values come only from filings published by that date, and each value links to the passages it was read from. The result is a table with one row per company and weekday. This page explains how the companies were chosen, which documents were collected, what each feature asks, and how to reproduce the run. It shows how the features are built, not whether they predict anything.

| File | Holds |
| --- | --- |
| [feature-table.csv](feature-table.csv) | The exported table: one row per company and weekday, 2023-01-02 to 2024-12-31. |
| [definitions/](definitions/) | The three feature definitions. |
| [tools/](tools/) | The scripts that chose the universe and collected the filings, as run. |

## The universe

SEC filers whose industry code (SIC) is 1311, crude petroleum and natural gas, and whose total assets at the 2021 year-end were at least $3 billion, from the SEC's XBRL frame for that date ([select_universe.py](tools/select_universe.py)). The rule returns 31 companies. Two were dropped by hand: Apache Corp, a subsidiary of APA Corp, which is in the universe, and Hess Midstream, which gathers and processes rather than produces. ConocoPhillips and Hess, which the SEC classes as petroleum refining (SIC 2911) though neither refines, were added by name at review, leaving 31. The 2021 year-end fixes the universe before the period begins, so companies acquired during it stay in: PDC Energy (filings end August 2023), Callon (April 2024), Pioneer (May 2024), Southwestern (October 2024) and Marathon Oil (November 2024). Hess's sale was still pending at the end of 2024. Viper Energy, a mineral and royalty owner, qualifies under SIC 1311 and stays in.

Each company is named as it was on 2021-07-01, so no session sees a later name: Cabot Oil & Gas rather than Coterra, Chesapeake rather than Expand, Oasis rather than Chord, Bonanza Creek rather than Civitas, Centennial rather than Permian Resources, IE PubCo rather than Crescent.

## The documents

[fetch_corpus.py](tools/fetch_corpus.py) collected every 10-K, 10-Q, 8-K (with its EX-99 exhibits: earnings releases and presentations furnished with them) and DEF 14A each company filed from 2021-07-01 to 2024-12-31: 3,186 Sources in all, split into 92,328 passages. Each Source carries its filing date as `published`, its form, exhibit, accession number and URL, and is linked `about` its company. Filings start eighteen months before the first cutoff so the first sessions see a year and a half of history.

The corpus holds only what was filed with the SEC, as text. Guidance a company publishes only on its website, or in images, is not in it. EOG files the narrative pages of each earnings release, guidance included, as images, so its production guidance is never visible and its drift is null throughout.

## The features

Each describes how a producer runs its business, from what it has disclosed, without judging it:

- **[production_guidance_drift](definitions/production_guidance_drift.json)** (number): latest expectation for a year's production over the first guidance given for it, ending at the reported actual. It records how a company's operating plan has held up against its own first word, adjusted for acquisitions and divestitures where the company states their contribution.
- **[inventory_disclosure](definitions/inventory_disclosure.json)** (category: `quantified`, `stopped_quantifying`, `described`, `not_discussed`): whether the company puts a number on the drilling locations or years of inventory it has left, in its recurring documents. Remaining inventory is most of a producer's value; a company that stops counting it says something by the silence, and silence can only be compared between documents that recur.
- **[bonus_weight_on_growth](definitions/bonus_weight_on_growth.json)** (number from 0 to 1): the share of the chief executive's target annual bonus tied to absolute production or reserve growth, from a proxy filed in the last 15 months. It records what management is paid to pursue.

All three are scheduled weekly from Monday 2023-01-02.

Every date in the example precedes the day the definitions were written, and the training cutoff of the agent model. The values are as good as the instructions and the documents allow, but they are no test of hindsight; the [feature guide](../../features.md#lookahead-what-stated-handles-and-what-it-doesnt) explains why only later dates are.

## Running it

In a new folder holding `contact.txt`, the name and email the SEC asks requests to carry, with this example's `tools/` and `definitions/`:

```bash
stated init --layer oil-and-gas
mkdir edgar
python tools/select_universe.py     # writes edgar/universe.json; exclusions and additions edited by hand
python tools/fetch_corpus.py        # Entities, Sources and passages; resumable
stated list --kind Entity --limit 100 \
  | python -c "import json, sys; print(*(r['id'] for r in json.load(sys.stdin)['records']), sep='\n')" > subjects.txt
for k in production_guidance_drift inventory_disclosure bonus_weight_on_growth; do
  stated define-feature --input definitions/$k.json
  echo '{"every": "week", "start": "2023-01-02"}' | stated schedule-feature $k --input -
done
stated run-features production_guidance_drift inventory_disclosure bonus_weight_on_growth \
  --subjects-file subjects.txt --to 2024-12-31 --workers 4 --author run/claude-code
stated feature-table --subjects-file subjects.txt --from 2023-01-02 --to 2024-12-31 \
  --every weekday --out feature-table.csv
```

`subjects.txt` lists the 31 Entities the corpus script created, one ID per line. Every session used Claude Code with Claude Opus 5.5.

## What the run did

| | |
| --- | --- |
| Company-weeks | 3,255 (31 companies × 105 weeks), each with all three features due |
| Agent sessions | 839: an agent read on about a quarter of company-weeks |
| Confirmed without a session | 7,323 feature values, on dates with no new filing since the last read |
| Confirmed by an agent | 2,049 feature values, read again and found to hold |
| Values written | 393: 162 drift (23 null), 140 inventory (1 null), 91 bonus weight (21 null) |

A value is written only when the answer changes, including its period or the document it rests on; otherwise the last value is confirmed. Each null carries its reason: for drift, mostly no numeric guidance, or an acquisition or divestiture whose contribution the company never stated; for bonus weight, a plan that states no metric weights, or no proxy filed in the last 15 months.

## The table

[feature-table.csv](feature-table.csv) has one row per company and weekday. For each feature it gives the value, `_as_of`, the date that value was written for, and `_checked`, the latest date it was read or confirmed. An empty value is null. The companies as they stood on 2024-12-31, with the period each value covers:

| Company | CIK | Guidance drift | Inventory | Bonus weight on growth | Latest filing |
| --- | --- | --- | --- | --- | --- |
| Antero Resources | 1433270 | 1.015 (FY2024) | quantified | 0.25 (FY2023) | 2024-10-30 |
| APA | 1841666 | null (FY2024) | described | 0 (FY2023) | 2024-12-03 |
| Bonanza Creek Energy | 1509589 | 1.036 (FY2024) | quantified | null (FY2023) | 2024-11-07 |
| Cabot Oil & Gas | 858470 | 1.019 (FY2024) | quantified | 0.2 (FY2023) | 2024-12-31 |
| California Resources | 1609253 | 0.963 (FY2024) | described | 0 (FY2023) | 2024-11-25 |
| Callon Petroleum | 928022 | null (FY2023) | described | null (FY2023) | 2024-04-01 |
| Centennial Resource Development | 1658566 | null (FY2024) | described | null (FY2023) | 2024-11-07 |
| Chesapeake Energy | 895126 | 1.000 (FY2025) | described | 0.05 (FY2023) | 2024-12-02 |
| CNX Resources | 1070412 | 0.948 (FY2024) | described | 0 (FY2023) | 2024-12-05 |
| Comstock Resources | 23194 | null (FY2022) | quantified | 0.15 (FY2023) | 2024-10-31 |
| ConocoPhillips | 1163165 | 1.008 (FY2024) | not_discussed | 0.075 (FY2023) | 2024-12-30 |
| Continental Resources | 732834 | null (FY2022) | quantified | null (FY2022) | 2024-11-12 |
| Devon Energy | 1090012 | 1.050 (FY2024) | quantified | 0.1 (FY2023) | 2024-12-09 |
| Diamondback Energy | 1539838 | null (FY2024) | quantified | 0 (FY2023) | 2024-11-07 |
| EOG Resources | 821189 | null (FY2022) | described | 0.05 (FY2023) | 2024-11-21 |
| EQT | 33213 | 0.956 (FY2024) | quantified | 0 (FY2023) | 2024-12-31 |
| Hess | 4447 | 1.094 (FY2023) | not_discussed | 0.35 (FY2023) | 2024-11-06 |
| IE PubCo | 1866175 | 1.020 (FY2024) | described | null (FY2022) | 2024-12-20 |
| Kosmos Energy | 1509991 | 0.905 (FY2024) | described | 0.141 (FY2023) | 2024-11-29 |
| Marathon Oil | 101778 | 1.011 (FY2024) | stopped_quantifying | 0 (FY2023) | 2024-11-22 |
| Matador Resources | 1520006 | null (FY2024) | quantified | null (FY2023) | 2024-11-07 |
| Murphy Oil | 717423 | 0.984 (FY2024) | stopped_quantifying | 0 (FY2023) | 2024-12-23 |
| Oasis Petroleum | 1486159 | 1.000 (FY2025) | described | 0 (FY2023) | 2024-11-08 |
| Occidental Petroleum | 797468 | 1.023 (FY2023) | quantified | 0 (FY2023) | 2024-11-12 |
| Ovintiv | 1792580 | 1.000 (FY2025) | described | null (FY2023) | 2024-12-12 |
| PDC Energy | 77877 | 1.000 (FY2023) | null | null | 2023-08-24 |
| Pioneer Natural Resources | 1038357 | 1.044 (FY2023) | quantified | null (FY2023) | 2024-05-03 |
| Range Resources | 315852 | 1.014 (FY2024) | described | 0 (FY2023) | 2024-10-23 |
| SM Energy | 893538 | 1.023 (FY2024) | described | 0.35 (FY2023) | 2024-12-12 |
| Southwestern Energy | 7332 | null (FY2023) | quantified | null (FY2023) | 2024-10-01 |
| Viper Energy Partners | 1602065 | 1.000 (FY2025) | not_discussed | null (FY2023) | 2024-12-09 |

## One company over time

Murphy Oil's values, each at the date it was written; between them the last value was confirmed weekly.

| Date | Guidance drift | Inventory | Bonus weight on growth |
| --- | --- | --- | --- |
| 2023-01-02 | null (FY2022): guidance revised for acquisitions and a divestiture, without the divestiture's full-year contribution | not_discussed | 0.15 (FY2021): production target 15% |
| 2023-01-30 | 1.000 (FY2023): 179.5 MBOEPD guided | | |
| 2023-03-27 | | | 0 (FY2022): production metric removed |
| 2023-08-07 | 1.020: raised to 183 | quantified: about 488 Kaybob Duvernay locations after a pending sale | |
| 2023-11-06 | null: the sale closed, contribution not stated | stopped_quantifying | |
| 2024-01-29 | 1.000 (FY2024): 184 guided | | |
| 2024-03-25 | | | 0 (FY2023) |
| 2024-11-11 | 0.984: lowered to 181 | | |

The inventory value was also rewritten five times with the same category as newer documents became the latest read; those rows are omitted.

## One value traced

Murphy's `inventory_disclosure` at 2023-08-07 is `quantified`. The value depends on one Finding, written by the same session:

> Murphy Oil's Q2 2023 earnings release (8-K EX-99.1, 2023-08-03) states a remaining drilling inventory for the Kaybob Duvernay: after the pending divestiture of 250 gross (138 net) drilling locations, Murphy will have approximately 488 gross drilling locations remaining there.

The Finding cites a byte range of the [release as filed](https://www.sec.gov/Archives/edgar/data/717423/000071742323000048/mur-2023q2xex991.htm), published 2023-08-03:

> After the transaction closes, Murphy will have approximately 488 gross drilling locations with an average 75 percent oil weighting remaining in the Kaybob Duvernay

At 2023-11-06 the value became `stopped_quantifying`. It depends on a new Finding that the Q3 release states no count and mentions drilling locations only in its forward-looking boilerplate, and on the Q2 Finding as the last document that did. Read literally, the definition counts a figure given to explain an asset sale as quantifying inventory; whether it should is a question for the next version of the definition, not for the agent.

## Known limitations

- **`_checked` keeps advancing after a company stops filing.** Each week with no new filing confirms the last values, so a company acquired in 2023 shows values checked on 2024-12-30. `_as_of` and the latest-filing date tell the two apart.
- **Later evidence on earlier values.** Seven values were linked to a Finding written at a later date, by a session that confirmed the value still held. The link carries the later date and the value's own contents never changed, but following evidence from an old value can reach documents published after it; a point-in-time read leaves those links out.
- **Author names the harness, not the model.** Values carry `run/claude-code`; the model, Claude Opus 5.5 throughout, comes from the session logs. The [feature guide](../../features.md) recommends naming it in `author_id`.
