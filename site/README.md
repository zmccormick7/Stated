# Landing page

`index.html` is the Stated landing page: one static file with its styles, scripts and the logo inline. `logo.svg` is the same logo as a file: the mark beside "Stated" in Geist Medium, converted to outlines so it needs no font. Fonts load from Google Fonts. Open it in a browser to view it; there is no build step. `.github/workflows/pages.yml` publishes this folder to GitHub Pages whenever it changes on `main`.

`filings/index.html` is the guide to the free set of SEC filings as Markdown, at `/filings/`: how to fetch one filing by address, find a company's filings, or download everything, with the columns, what is covered and the licence. It is one static file in the landing page's manner, and borrows `logo.svg` and the icons from this folder. Its example is the parser's real output for a table in Apple's 2024 annual report, beside the same rows as filed. The data sits in a public bucket, not in this repository; every address on the page starts with the bucket's public address, which is written out in full each time so it can be replaced in one pass when the data moves to its own domain.

The icons sit beside it. `favicon.svg` and `favicon.ico` (16, 32 and 48 px) use a cut of the mark for small sizes, with the gap between the blocks widened to whole pixels so it stays open; the SVG turns light when the browser is in dark mode. `apple-touch-icon.png` (180 px) uses the mark unchanged.

`describe-dont-predict.pdf` is the note "Describe, Don't Predict: LLM Agents as Point-in-Time State Builders" (Nov 4, 2026), linked from the backtests section.

`robots.txt` lets every crawler and agent read the whole site, and `sitemap.xml` names its two pages. The file is there so a fetcher that asks for it first gets a plain yes; without it the host answers that request with an error now and then, which a careful fetcher takes as a no.

The companies, filings and values in the page's examples are illustrative.
