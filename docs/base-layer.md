# The base layer

The base layer is a published, read-only graph of US public companies and their SEC filings that you can attach to a research folder, so your agents start from documents and companies rather than an empty database. It is a starting point. Every user who attaches it gets the same file, and SEC filings are among the most widely read documents in the market, so a feature built only from the base layer is one anyone can build. An edge is more likely to come from what you add on top: your own documents, your own subjects and your own feature definitions.

This guide covers what the base layer holds, how its contents behave at a past cutoff, what it leaves out, and how to bring your own data.

## What it holds

| Part | Contents |
| --- | --- |
| Companies | The 1,000 largest companies filing annual reports on Form 10-K with the SEC, ranked by the public float each reported on its 10-K cover in the year to September 2026, each with name, tickers, CIK, industry code and rank. A further 6,100 SEC filers appear with identity only, so that filings and findings can name them. |
| Filings | Every 10-K, 10-Q and results release (the exhibit to an 8-K reporting Item 2.02, results of operations) those 1,000 companies filed between October 30, 2023 and October 29, 2026: about 2,950 annual reports, 8,950 quarterly reports and 12,300 results releases, or 24 per company. Each holds the document's full text, split into passages for search, with its filing date, acceptance time, period, accession number and a hash of the original file. |
| Reported figures | Revenue, net income and total assets for each company from its latest 10-K's XBRL data. |
| Coverage packs | For about 470 of the largest 476 companies, agent-written findings on the business, its segments, key figures, outlook, relationships with other companies, risks and open questions, each citing the filing it rests on: about 12,900 findings and 2,300 questions. |
| Other research | About 270 documents agents fetched while researching AI infrastructure (transcripts, presentations, press releases and other filings), the 600 findings drawn from them, and six driver models. |

It holds no feature definitions or feature values. Features are yours to define and run.

## At a past cutoff

A session opened at a cutoff sees a Source only once it was published, so the filings work for point-in-time features as they stand. Each carries the date it became public and is linked `about` the company that filed it, with the link dated to the filing, so a session at any date finds exactly the company's filings published by then. Model-written document summaries are hidden at a cutoff, since they were written later.

The coverage packs were written in October 2026 to describe each company as it was then, and they are not built for features. Their figures cite filings and become visible on those filings' dates, but the choice of what to record was made with everything known in October 2026, and their links and questions are dated October 2026, so at an earlier cutoff a session sees pack figures without the companies they are about. Use packs as context for questions about the present. Have a feature's own sessions read the filings.

Three years of filings is a short history: a quarterly feature gets about 12 readings per company, and a session at a cutoff before October 30, 2023 finds no filings at all.

## What it does not provide

- **An edge by itself.** Everything in the base layer is public, the same for every user, and drawn from documents that many data vendors already parse.
- **Market data.** No prices, returns, volumes, corporate actions or index membership. `stated feature-table` writes one row per subject and day, ready to join to market data from your own provider.
- **A fundamentals history.** Three reported figures per company, from one year. For financial statement history, use XBRL data or a fundamentals vendor.
- **A survivorship-free universe.** The companies were chosen by their size in 2026. Companies that were acquired, delisted, went bankrupt or shrank out of the top 1,000 before then are missing, and companies that grew into it are present for years when they were smaller. A backtest needs a universe defined as of each date, which you supply as the subjects of a run.
- **Other issuers.** No foreign companies that report on Form 20-F, no private companies, and no subjects other than companies, such as markets, products or deals.
- **Other documents.** No earnings call transcripts beyond a handful, news, sell-side research, proxy statements, 8-Ks other than results releases (deals, leadership changes, material agreements), insider trades, institutional holdings, registration statements, SEC comment letters, regulatory dockets or industry data.
- **Human review.** Filing text is exact and its hash can be checked against the original, but every finding was written by an agent or, for the reported figures, a script, and nobody has reviewed them.

Known gaps: five companies (IBM, Wells Fargo, Progressive, U.S. Bancorp and BNY) put their financial statements in an exhibit to the 10-K that was not captured; about 150 filings were left out because the 8-K had no results exhibit, the text was too short to be the document, or it repeated another filing word for word; three companies filed no results releases in the window.

## Bring your own data

What a model can learn from depends on what its features can see. The usual sources of an edge are documents other users lack (licensed transcripts and news, broker research, expert-call notes, industry publications, regulatory filings outside the SEC), subjects the base layer does not cover (private companies, products, markets, deals), and feature definitions that ask what others have not thought to ask.

Your documents and records go in your own layer, on top of the base. To make a document usable at past cutoffs:

1. **Ingest it with the date it became public** as `published`, not the date you obtained it. A Source without a date is never seen at any cutoff.
2. **Link it `about` each subject, at that date as the cutoff**, so a session sees the link exactly when it sees the document. Find a company's Entity with `stated search`, or create an Entity for a new subject.
3. **Mark licensed text `"redistributable": false`**, so that if you ever publish your layer, its text, passages and summaries are withheld.

```bash
stated ingest acme-q2-2025-call.txt --input - <<'JSON'
{"author_id": "researcher", "description": "Acme Corp second-quarter 2025 earnings call transcript",
 "attributes": {"published": "2025-07-24", "publisher": "Acme Corp",
                "document_type": "earnings call transcript", "redistributable": false}}
JSON
stated chunk SOURCE_ID
echo '{"author_id": "researcher"}' | stated --as-of 2025-07-24 link SOURCE_ID ENTITY_ID about --input -
```

A document about several subjects gets one `about` link to each. Load many documents with a short script over the [Python API](python-api.md): `ingest_source`, `chunk_source`, then `create_link` from a `RecordStore` opened with `as_of` set to the publication date.

Attach the base layer with `stated attach-layer FILE`, or start the app from it with `stated ui --base FILE`. A later version replaces the attached one in place: records keep their identity, so your links to them stay valid, and `attach-layer` lists what your layer should reassess.
