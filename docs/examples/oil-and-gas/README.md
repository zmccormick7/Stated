# Oil and gas producers, 2023–2024

This example uses Stated to measure three things about 31 US oil and gas producers, every week of 2023 and 2024, from their SEC filings:

- how far each company's production guidance for the year has moved from the first figure it gave,
- whether it says how much drilling inventory it has left, and
- what share of its chief executive's annual bonus is tied to growing production or reserves.

Each week's values come only from filings published by that date, and each value links to the passages it was read from. The values describe what the companies had said. They are not tested here as predictors of anything.

| File | Contents |
| --- | --- |
| [feature-table.csv](feature-table.csv) | The exported table, with one row per company and weekday from 2023-01-02 to 2024-12-31. |
| [definitions/](definitions/) | The three feature definitions. |
| [tools/](tools/) | The two scripts that chose the companies and downloaded their filings. |

## What the run produced

The table has one row per company and weekday. Here are six of the companies on 2024-12-31:

| Company | production_guidance_drift | inventory_disclosure | bonus_weight_on_growth |
| --- | --- | --- | --- |
| Devon Energy | 1.05 | quantified | 0.1 |
| Diamondback Energy | | quantified | 0 |
| EQT | 0.955556 | quantified | 0 |
| Marathon Oil | 1.010526 | stopped_quantifying | 0 |
| Murphy Oil | 0.983696 | stopped_quantifying | 0 |
| SM Energy | 1.022643 | described | 0.35 |

An empty cell is a null, and every null has a recorded reason. Diamondback's guidance drift is null because it revised its 2024 guidance to include its merger with Endeavor, which closed in September 2024, without saying how much Endeavor would add to the year's production.

Each feature also has two date columns. `_as_of` is the date the value was written for, and `_checked` is the latest date on which it was read again or confirmed. Here is Murphy Oil's guidance drift on the days around its third-quarter 2024 earnings release, which it filed on Thursday, 2024-11-07:

| date | production_guidance_drift | production_guidance_drift_as_of | production_guidance_drift_checked |
| --- | --- | --- | --- |
| 2024-11-01 | 1.0 | 2024-01-29 | 2024-10-28 |
| 2024-11-04 | 1.0 | 2024-01-29 | 2024-11-04 |
| 2024-11-08 | 1.0 | 2024-01-29 | 2024-11-04 |
| 2024-11-11 | 0.983696 | 2024-11-11 | 2024-11-11 |

The features are read every Monday. On 2024-11-04 nothing had changed, so the value from January was confirmed. The release came out on the Thursday, and the next Monday's reading picked it up and wrote a new value.

## How Murphy Oil's guidance drift moved over two years

Each time Murphy's guidance drift changed, the new value rested on a sentence in an earnings release:

| Value date | Value | Filing | What it says |
| --- | --- | --- | --- |
| 2023-01-30 | 1.000 | [Q4 2022 release](https://www.sec.gov/Archives/edgar/data/717423/000071742323000004/mur-2022q4xex991.htm), 2023-01-26 | "Full year 2023 production is expected to be in the range of 175.5 to 183.5 MBOEPD" |
| 2023-08-07 | 1.020 | [Q2 2023 release](https://www.sec.gov/Archives/edgar/data/717423/000071742323000048/mur-2023q2xex991.htm), 2023-08-03 | Guidance raised to 180,000 to 186,000 barrels of oil equivalent per day |
| 2023-11-06 | null | [Q3 2023 release](https://www.sec.gov/Archives/edgar/data/717423/000071742323000059/mur-2023q3xex991.htm), 2023-11-02 | Guidance raised to 185 to 187, after the sale of some Canadian assets closed |
| 2024-01-29 | 1.000 | [Q4 2023 release](https://www.sec.gov/Archives/edgar/data/717423/000071742324000004/mur-2023q4xex991.htm), 2024-01-25 | "Full year 2024 production is expected to be in the range of 180 to 188 MBOEPD" |
| 2024-11-11 | 0.984 | [Q3 2024 release](https://www.sec.gov/Archives/edgar/data/717423/000071742324000036/mur-2024q3xex991.htm), 2024-11-07 | Range tightened to 180 to 182 |

The value is the midpoint of the latest guidance divided by the midpoint of the first guidance for the same year. The August 2023 raise took the 2023 midpoint from 179.5 to 183, a drift of 1.020. In November 2023 Murphy raised the range again, but the new figure no longer included the Canadian assets it had sold, and it never said how much those assets would have produced over the full year. The two figures could no longer be compared, so the value became null for the rest of 2023. In January 2024 the first 2024 guidance started a new year at 1.000.

The last value, 0.984 on 2024-11-11, was written by an agent session that could see only filings published by that date. Its record says, in part:

> Initial guidance: 180-188 MBOEPD (midpoint 184), Q4 2023 earnings release of 2024-01-25. Latest expectation: 180-182 MBOEPD (midpoint 181), Q3 2024 earnings release of 2024-11-07; FY2024 actuals are not yet reported. No in-year acquisition or divestiture adjustment applies.

The value is linked to two Findings, one for each release. Each Finding cites the exact passage it was read from. The newer one cites this sentence from the Q3 2024 release:

> The company is tightening its full year 2024 production range to 180 to 182 MBOEPD, consisting of approximately 50 percent oil and 55 percent liquids volumes.

## What the three features measure

[production_guidance_drift](definitions/production_guidance_drift.json) is the company's latest expectation for a year's production divided by the first guidance it gave for that year. The latest expectation is the midpoint of the most recent guidance until the company reports the year's actual production, and the actual after that. When guidance is revised to include an acquisition or a sale, the agent removes the deal's effect if the company says how much it contributes. If the company doesn't say, the value is null for the rest of the year.

[inventory_disclosure](definitions/inventory_disclosure.json) records whether the company's latest annual report or quarterly earnings materials state how many drilling locations, or how many years of drilling, it has left. The value is `quantified` if either of them gives a number. It is `stopped_quantifying` if neither does but one of those documents did in the previous 18 months. Otherwise it is `described` if they discuss inventory in words, and `not_discussed` if they don't mention it.

[bonus_weight_on_growth](definitions/bonus_weight_on_growth.json) is the share of the chief executive's target annual bonus that is tied to growth in production or reserve volumes, read from a proxy statement filed in the previous 15 months. Measures per share, costs, returns and cash flow do not count as growth.

## How to reproduce the run

Start in an empty folder. Copy this example's `tools/` and `definitions/` into it, and add a file named `contact.txt` holding your name and email address, which the SEC asks every request to carry. Then run:

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

`select_universe.py` writes the list of companies to `edgar/universe.json`. The changes to that list described below are made by editing the file before running `fetch_corpus.py`. Every agent session used Claude Code with Claude Opus 5.5.

## How the companies and filings were chosen

The companies are every SEC filer with industry code (SIC) 1311, crude petroleum and natural gas, and total assets of at least $3 billion at the end of 2021. Apache Corp was removed because its parent, APA, is already included, and Hess Midstream because it doesn't produce oil or gas. ConocoPhillips and Hess were added, because the SEC classes them as refiners although neither of them refines. Companies acquired during 2023 and 2024 stay in: PDC Energy, Callon, Pioneer, Southwestern and Marathon Oil. Each company appears under the name it used in mid-2021, so that no session sees a later name. Coterra, for example, appears as Cabot Oil & Gas.

The filings are every 10-K, 10-Q, 8-K and proxy statement the companies filed from mid-2021 to the end of 2024, together with the earnings releases and presentations furnished with the 8-Ks: 3,186 documents in all. Only text filed with the SEC is included. EOG files its earnings releases' guidance as images, so its guidance drift is null throughout.

On about a quarter of the 3,255 company-weeks, Stated started an agent session, because a new filing had appeared since the last reading or because a reading had passed the age limit its definition sets: 839 sessions in all. On the other weeks it confirmed the existing values without a session. The agents wrote 393 values.

Every date in the example falls before the definitions were written and before the model's training cutoff, so the example cannot show whether the features are free of hindsight. The [feature guide](../../features.md#lookahead-what-stated-handles-and-what-it-doesnt) explains why only later dates can test that.

## Limitations

`_checked` keeps advancing after a company stops filing, because each week without a new filing confirms its latest values. A company acquired in 2023 still shows values checked on 2024-12-30. Its `_as_of` dates show when the values were last written.

Seven values are linked to a Finding written later than the value, by a session that confirmed the value still held. A point-in-time read leaves those links out, but someone following evidence from an old value in the current graph can reach documents published after its date.

Values carry the author `run/claude-code`, which names the harness but not the model. The [feature guide](../../features.md) recommends naming the model in `author_id`.
