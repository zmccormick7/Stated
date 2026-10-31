# Oil and gas producers, 2023–2024

This example uses Stated to measure three things about 31 US oil and gas producers, every week of 2023 and 2024, from their SEC filings:

- how far each company's production guidance for the year has moved from the first figure it gave,
- whether it says how much drilling inventory it has left, and
- what share of its chief executive's annual bonus is tied to growing production or reserves.

Each week's values come only from filings published by that date, and each value links to the passages it was read from. The result is a table with one row per company and weekday. This page explains how the companies were chosen, which documents were collected, what each feature asks, and how to reproduce the run. It shows how the features are built, not whether they predict anything.

| File | Contents |
| --- | --- |
| [feature-table.csv](feature-table.csv) | The exported table, with one row per company and weekday from 2023-01-02 to 2024-12-31. |
| [definitions/](definitions/) | The three feature definitions. |
| [tools/](tools/) | The two scripts that chose the companies and downloaded their filings, as they were run. |

## The companies

The universe is every SEC filer whose industry code (SIC) is 1311, crude petroleum and natural gas, and that reported total assets of at least $3 billion at the end of 2021. [select_universe.py](tools/select_universe.py) reads total assets from the SEC's XBRL data for that date, and the rule returns 31 companies. Two of them were removed by hand. Apache Corp is a subsidiary of APA Corp, which is already in the universe, and Hess Midstream gathers and processes oil and gas rather than producing it. Two companies were added by name: ConocoPhillips and Hess. The SEC classes both as petroleum refining (SIC 2911), although neither of them refines. That leaves 31 companies.

The universe is fixed at the end of 2021, before the period begins, so companies acquired during the period stay in it until their filings stop. PDC Energy's filings end in August 2023, Callon's in April 2024, Pioneer's in May 2024, Southwestern's in October 2024 and Marathon Oil's in November 2024. Hess's sale was still pending at the end of 2024. Viper Energy owns mineral and royalty interests rather than operating wells, but the SEC classes it under SIC 1311, so it stays in.

Each company appears under the name it used on 2021-07-01, so that no session sees a name the company adopted later. Coterra appears as Cabot Oil & Gas, Expand Energy as Chesapeake Energy, Chord as Oasis Petroleum, Civitas as Bonanza Creek Energy, Permian Resources as Centennial Resource Development, and Crescent Energy as IE PubCo.

## The documents

[fetch_corpus.py](tools/fetch_corpus.py) downloaded every 10-K, 10-Q, 8-K and DEF 14A that the companies filed between 2021-07-01 and 2024-12-31, along with the EX-99 exhibits furnished with each 8-K, which hold earnings releases and investor presentations. That comes to 3,186 documents, split into 92,328 passages for search. Each document is stored as a Source that records its filing date (as `published`), form, exhibit, accession number and URL, and is linked `about` its company. The collection starts eighteen months before the first date in the table, so that the earliest sessions can see a year and a half of filings.

The corpus contains only the text of what the companies filed with the SEC. Guidance published only on a company's website, or only in images, is missing. EOG files the narrative pages of its earnings releases as images, including its production guidance, so no session could see that guidance, and EOG's guidance drift is null throughout.

## The features

The three features describe how each company runs its business, using only what it has disclosed. None of them rates the company.

[production_guidance_drift](definitions/production_guidance_drift.json) is a number. For the latest fiscal year the company has given production guidance for, it divides the company's latest expectation for that year's production by the first guidance it gave. The latest expectation is the midpoint of the most recent guidance until the company reports the year's actual production, and the actual after that. The value is 1.0 when guidance is first given, and 1.05 means the company now expects 5% more than it first said. When a company revises its guidance to include an acquisition or a sale, the agent removes the deal's effect if the company states how much it contributes. If the company doesn't state it, the value is null for the rest of that year.

[inventory_disclosure](definitions/inventory_disclosure.json) is one of four categories: `quantified`, `stopped_quantifying`, `described` or `not_discussed`. It records whether the company's latest annual report or latest quarterly earnings materials state how many drilling locations, or how many years of drilling, the company has left. `stopped_quantifying` means that neither of them states a number, but one of those documents did in the previous 18 months. Only annual reports and quarterly earnings materials count. Because companies publish them on a regular cycle, a number missing from the latest one can be compared with the same document a quarter or a year earlier.

[bonus_weight_on_growth](definitions/bonus_weight_on_growth.json) is a number from 0 to 1. It is the share of the chief executive's target annual bonus that is tied to growth in production or reserve volumes, read from a proxy statement filed in the previous 15 months. Metrics measured per share, costs, returns and cash flow do not count as growth. The value is 0 when the bonus plan assigns weights and none of them goes to growth. It is null when there is no recent proxy statement, or when the plan assigns no weights.

All three features are scheduled weekly, starting on Monday 2023-01-02.

Every date in the example falls before the day the definitions were written, and before the training cutoff of the model the agents ran on. The values are only as good as the instructions and the documents allow, and they cannot show whether the features are free of hindsight. The [feature guide](../../features.md#lookahead-what-stated-handles-and-what-it-doesnt) explains why only dates after a definition was written can test that.

## Running it

To reproduce the run, start in an empty folder. Copy this example's `tools/` and `definitions/` into it, and add a file named `contact.txt` holding your name and email address, which the SEC asks every request to carry. Then run:

```bash
stated init --layer oil-and-gas
mkdir edgar
python tools/select_universe.py
python tools/fetch_corpus.py
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

`select_universe.py` writes the list of companies to `edgar/universe.json`. The removals and additions described above are made by editing that file before running `fetch_corpus.py`, which creates one Entity for each company along with its Sources and passages. If `fetch_corpus.py` is interrupted, running it again picks up where it stopped. `subjects.txt` lists the IDs of the 31 Entities, one per line.

Every session used Claude Code with Claude Opus 5.5.

## What the run did

On each weekly date, `run-features` looked at each company in turn. If no new filing had been linked to the company since its values were last read, Stated confirmed those values without starting a session. Otherwise it started one agent session for the company at that date, which read the new filings and either wrote new values or confirmed that the existing ones still held.

| | |
| --- | --- |
| Company-weeks | 3,255 (31 companies over 105 weeks), each with all three features due |
| Agent sessions | 839, so an agent read about a quarter of the company-weeks |
| Values confirmed without a session | 7,323 |
| Values confirmed by an agent | 2,049 |
| Values written | 393: 162 for guidance drift (23 of them null), 140 for inventory (1 null) and 91 for bonus weight (21 null) |

An agent writes a new value only when the answer changes, including when the period it covers or the document it rests on changes. Otherwise it confirms the latest value. Every null value states its reason. For guidance drift, the usual reasons are that the company gave no numeric guidance, or that it closed an acquisition or sale without saying how much it contributes to the year's production. For bonus weight, they are that the plan assigns no weights to its metrics, or that no proxy statement was filed in the previous 15 months.

## The table

[feature-table.csv](feature-table.csv) has one row per company and weekday. Each feature has three columns: the value itself, `_as_of`, which is the date the value was written for, and `_checked`, which is the latest date on which it was read or confirmed. An empty value is null. The table below shows each company's values on 2024-12-31, with the period each value covers:

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

The table below lists each value written for Murphy Oil, on the date it was written for. In the weeks between, the latest value was confirmed.

| Date | Guidance drift | Inventory | Bonus weight on growth |
| --- | --- | --- | --- |
| 2023-01-02 | null (FY2022): guidance was revised for acquisitions and a sale, without the sale's full-year contribution | not_discussed | 0.15 (FY2021): a production target weighted 15% |
| 2023-01-30 | 1.000 (FY2023): 179.5 thousand barrels of oil equivalent per day guided | | |
| 2023-03-27 | | | 0 (FY2022): the production metric was removed |
| 2023-08-07 | 1.020: guidance raised to 183 | quantified: about 488 Kaybob Duvernay locations left after a pending sale | |
| 2023-11-06 | null: the sale closed without its contribution being stated | stopped_quantifying | |
| 2024-01-29 | 1.000 (FY2024): 184 guided | | |
| 2024-03-25 | | | 0 (FY2023) |
| 2024-11-11 | 0.984: guidance lowered to 181 | | |

The inventory value was also written five more times with the same category, each time because a newer document had become the latest one read. Those rows are left out.

## One value traced

Murphy's `inventory_disclosure` value at 2023-08-07 is `quantified`. It rests on one Finding, written by the same session:

> Murphy Oil's Q2 2023 earnings release (8-K EX-99.1, 2023-08-03) states a remaining drilling inventory for the Kaybob Duvernay: after the pending divestiture of 250 gross (138 net) drilling locations, Murphy will have approximately 488 gross drilling locations remaining there.

The Finding cites a passage of the [release as filed](https://www.sec.gov/Archives/edgar/data/717423/000071742323000048/mur-2023q2xex991.htm), published 2023-08-03:

> After the transaction closes, Murphy will have approximately 488 gross drilling locations with an average 75 percent oil weighting remaining in the Kaybob Duvernay

On 2023-11-06 the value became `stopped_quantifying`. That value rests on two Findings: a new one recording that the third-quarter release gives no count and mentions drilling locations only in its forward-looking statements disclaimer, and the second-quarter Finding above, as the last document that gave one. The definition, read literally, treats a number given to explain an asset sale as quantifying inventory. Whether it should is a decision for the next version of the definition, not for the agent.

## Known limitations

`_checked` keeps advancing after a company stops filing. Each week without a new filing confirms the latest values, so a company acquired in 2023 still shows values checked on 2024-12-30. The `_as_of` column and the date of the latest filing tell the two cases apart.

Seven values are linked to a Finding written at a later date than the value. In each case a later session confirmed that the value still held and linked it to the Finding it relied on. The value's contents did not change, and the link carries the later date, so a point-in-time read leaves it out. Someone following evidence from an old value in the current graph, however, can reach documents published after the value's date.

Values carry the author `run/claude-code`, which names the harness but not the model. The model, Claude Opus 5.5 for every session, is recorded only in the session logs. The [feature guide](../../features.md) recommends naming the model in `author_id`.
