# The base layer

The base layer is a published, read-only graph of US public companies and their SEC filings, already split into passages and embedded, so your agents can search any covered company's filings as of any date from the first minute. It is the [Stated SEC filings dataset](https://statedlabs.com/filings/) as a graph file: the same companies, the same documents, the same text.

It is a starting point. Every user gets the same file, and SEC filings are among the most widely read documents in the market, so a feature built only from the base layer is one anyone can build. An edge is more likely to come from what you add on top: your own documents, your own subjects and your own feature definitions.

This guide covers what the base layer holds, how to start from it and keep it current, how its contents behave at a past cutoff, what it leaves out, and how to bring your own data.

## What it holds

| Part | Contents |
| --- | --- |
| Companies | An Entity for every domestic SEC filer that has been in the Russell 1000 index at any time since 2016, with its CIK. |
| Filings | A Source for every annual report (10-K), quarterly report (10-Q), proxy statement (DEF 14A) and current report (8-K, with each exhibit 99 filed with it, such as a results release) those companies have filed since 1 January 2016, whether or not the company was in the index that day. Each holds the document's full text as Markdown and is linked `about` its company, and about each other covered company a joint filing was made for. |
| Filing labels | On each Source: the time the SEC accepted it (`published`), the company's name and tickers on the filing day, `form`, `document`, `accession`, `filed`, the `period` it reports on, a report's `fiscal_year` and `fiscal_period`, an 8-K's `items`, and its address on sec.gov (`url`). A label the filing's own data does not bear out is left empty, so one that is present can be relied on. |
| Passages and embeddings | Every document split into passages of up to 2,000 characters, each with its embedding by OpenAI's `text-embedding-3-small`, so a search needs no indexing on your machine. |

A version is named for a filing day. `2026.11.24`, the file to download, holds everything filed up to 24 November 2026. Each later version adds the filings published since the one before and is named for the last filing day that had ended when it was made. It can include filings the SEC accepted that evening, which the SEC dates the next business day.

Nothing in it was written by an agent or a model. A script builds it from the dataset, and the same inputs give the same file. It holds no findings, summaries, feature definitions or feature values: those are yours to make.

## Start from it

The base is too large to copy into another database, so it becomes your research database, and your own work goes in a layer beside it in the same file. The download is about 11 GB and opens out to about 34 GB.

```bash
curl -O https://data.statedlabs.com/base/base-2026.11.24.sqlite.zst
zstd -d base-2026.11.24.sqlite.zst
stated ui --base base-2026.11.24.sqlite
```

`zstd -d` checks the download as it unpacks it, and the `.zst` file can be deleted afterwards. The first run moves the unpacked file to `~/Stated/research.sqlite`, gives it a layer for your work and opens the Features view. Without the app, put the file where you want the database and run `stated --db FILE init`. Either way `stated layers` then lists your layer and the base with its version.

One limit for now: a research folder that already exists cannot be moved onto the base, so start a new one from it.

A search finds passages by meaning, which takes one call to embed the question with the model that embedded the base, before the reranker that every document search uses. The `OPENROUTER_API_KEY` that document search already needs covers both, so there is nothing more to set. If `OPENAI_API_KEY` is also set, the question is embedded at OpenAI directly; the vectors are the same either way. Embedding a question costs a small fraction of a cent.

## Keep it current

```bash
stated pull
```

A new version is published each day there are new filings. `stated pull` reads the list of versions at `https://data.statedlabs.com/base/`, downloads what has been added since the version you hold, usually a few megabytes for a day, and adds it to the base in place: the new filings with their passages and embeddings, and any new companies. It checks each download, removes nothing and leaves your own layer as it is. With nothing newer it changes nothing, so it can be run as often as you like. `stated layers` shows the version you hold.

## Find a company and its filings

```bash
stated list --kind Entity --where 'cik="0000320193"'
stated --as-of 2020-06-30 sources ENTITY_ID
stated --as-of 2020-06-30 search-documents "share repurchase authorization" --about ENTITY_ID
```

- **Find a company by its CIK**, the SEC's number for a filer, which never changes. The dataset's [company list](https://data.statedlabs.com/companies.json) gives each company's CIK with its latest name and tickers. `stated search NAME` also works, on names as they were filed: a company's Entity is named as on its earliest filing here, and each filing's description carries the name of its day.
- **`sources ENTITY_ID`** lists the company's filings. Each is described in a line, such as `Apple Inc. 10-K filed 2019-10-31, fiscal 2019 FY, for the period ended 2019-09-28`, so a session can pick the report it wants without opening one.
- **`search-documents QUESTION --about ENTITY_ID`** searches every filing of one company, and `--source-id SOURCE_ID` searches inside named documents. A search that names neither covers only documents in a keyword index, which the base does not have: it finds your own documents and nothing in the base.

## At a past cutoff

A session opened at a cutoff sees a filing from the day the SEC accepted it, and its link to its company from the same day, so `sources` and `search-documents` at any date return exactly what had been filed by then.

- **A cutoff is a whole day, in UTC.** A session at a date sees everything accepted up to the end of that day in UTC, which is 7 or 8 in the evening in New York. That includes filings made after the US market closed, as most results releases are. Treat a value at a date as known before the next day's open, not at that day's close. Each Source keeps the exact acceptance time in `published`.
- **No record carries a later name.** A company's Entity keeps the name on its earliest filing here, however it was renamed since, and each filing carries the name and tickers of its day. Tickers change and are reused; match companies on CIK.
- **The list of companies is not point in time.** Entities are reference data, seen at every date, and the companies were chosen by index membership at any time since 2016. A company that was acquired in 2018 is here for the years it filed, which a survivorship-free backtest needs. But a company is also here for the years before it entered the index, and the base does not say who was a member when. Choose each run's subjects from your own universe as of each date.

## What it does not provide

- **An edge by itself.** Everything in the base layer is public, the same for every user, and drawn from documents that many data vendors already parse.
- **Market data.** No prices, returns, volumes, corporate actions or index membership. `stated feature-table` writes one row per subject and day, ready to join to market data from your own provider.
- **Figures as data.** Financial statements are in the filings as Markdown tables, for an agent to read. There are no reported figures as values; for a fundamentals history, use XBRL data or a fundamentals vendor, loaded with `stated ingest-table`.
- **Other issuers.** No companies that file only foreign forms (20-F, 40-F, 6-K), no private companies, and no subjects other than companies, such as markets, products or deals.
- **Other documents.** No amended filings, exhibits other than exhibit 99, registration statements, insider trades, institutional holdings, earnings call transcripts, news or research. Images and charts in a filing are left out, as are exhibits filed as scans.
- **Anything read for you.** No summaries, findings or models. The filings' text is the filers' own words and figures, and what they mean for a feature is for your sessions to work out.

The dataset's [known limits](https://statedlabs.com/filings/) apply to the text and the labels here as they do there.

## Bring your own data

What a model can learn from depends on what its features can see. The usual sources of an edge are documents other users lack (licensed transcripts and news, broker research, expert-call notes, industry publications, regulatory filings outside the SEC), subjects the base layer does not cover (private companies, products, markets, deals), and feature definitions that ask what others have not thought to ask.

Your documents and records go in your own layer, beside the base. To make a document usable at past cutoffs:

1. **Ingest it with the date it became public** as `published`, not the date you obtained it. A Source without a date is never seen at any cutoff.
2. **Split it with `OPENROUTER_API_KEY` or `OPENAI_API_KEY` set**, so its passages are embedded by the same model as the base's and one search of a company covers its filings and your documents together.
3. **Link it `about` each subject, at that date as the cutoff**, so a session sees the link exactly when it sees the document. Find a company's Entity by its CIK, or create an Entity for a new subject.
4. **Mark licensed text `"redistributable": false`**, so that if you ever publish your layer, its text, passages and summaries are withheld.

```bash
stated ingest acme-q2-2025-call.txt --input - <<'JSON'
{"author_id": "researcher", "description": "Acme Corp second-quarter 2025 earnings call transcript",
 "attributes": {"published": "2025-07-24", "publisher": "Acme Corp",
                "document_type": "earnings call transcript", "redistributable": false}}
JSON
stated chunk SOURCE_ID
echo '{"author_id": "researcher"}' | stated --as-of 2025-07-24 link SOURCE_ID ENTITY_ID about --input -
```

A document about several subjects gets one `about` link to each. Load many documents with a short script over the [Python API](python-api.md): `ingest_source`, `chunk_source`, `embed_source`, then `create_link` from a `RecordStore` opened with `as_of` set to the publication date.
