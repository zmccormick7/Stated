"""Build the showcase corpus: for each company in edgar/universe.json, its 10-K, 10-Q, 8-K (the filing and
its EX-99 exhibits: earnings releases, presentations) and DEF 14A filings dated in WINDOW, fetched from
EDGAR as filed, ingested as Sources with `published` set to the filing date, linked `about` the company's
Entity, and split into passages for document search.

Names are the ones in force on the date: an Entity carries the company's name at the start of the
window and a Source the name on its filing date, so no session reads a later name. Resumable: each fetched
document's URL is recorded in edgar/done.json.

Usage: python tools/fetch_corpus.py [CIK ...]
"""

import json
from pathlib import Path
import re
import sys
import time
import urllib.request

from stated import RecordStore
from stated.fetch import fetch_page

HERE = Path(__file__).resolve().parent.parent
AGENT = (HERE / "contact.txt").read_text().strip()
WINDOW = ("2021-07-01", "2024-12-31")
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A", "DEF 14A"}
DB = HERE / "research.sqlite"
DONE = HERE / "edgar" / "done.json"


def get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept-Encoding": "identity"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                time.sleep(0.12)
                return response.read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))


def name_at(submissions: dict, day: str) -> str:
    """The company's name in force on a date, from EDGAR's former names."""
    for former in submissions.get("formerNames", []):
        if former["from"][:10] <= day <= former["to"][:10]:
            return former["name"]
    return submissions["name"]


def filings(submissions: dict) -> list[dict]:
    pages = [submissions["filings"]["recent"]]
    for extra in submissions["filings"].get("files", []):
        if extra["filingTo"] >= WINDOW[0]:
            pages.append(json.loads(get(f"https://data.sec.gov/submissions/{extra['name']}")))
    rows = []
    for page in pages:
        for i, form in enumerate(page["form"]):
            if form in FORMS and WINDOW[0] <= page["filingDate"][i] <= WINDOW[1]:
                rows.append({key: page[key][i] for key in ("accessionNumber", "filingDate", "reportDate", "form",
                                                            "items", "primaryDocument", "primaryDocDescription")})
    return sorted(rows, key=lambda r: (r["filingDate"], r["accessionNumber"]))


def exhibits(cik: int, accession: str) -> list[tuple[str, str]]:
    """(exhibit type, URL) of the EX-99 documents of a filing, from its index page."""
    index = get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}-index.htm")
    found = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", index.decode("utf-8", "replace"), re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        links = re.findall(r'href="(/Archives/[^"]+)"', row)
        if len(cells) >= 4 and cells[3].startswith("EX-99") and links:
            found.append((cells[3], "https://www.sec.gov" + links[0]))
    return found


def main(selected: list[str]) -> None:
    universe = json.loads((HERE / "edgar" / "universe.json").read_text())
    done = json.loads(DONE.read_text()) if DONE.is_file() else {}
    with RecordStore(DB) as db:
        for company in universe:
            if selected and company["cik"] not in selected:
                continue
            cik = int(company["cik"])
            submissions = json.loads(get(f"https://data.sec.gov/submissions/CIK{company['cik']}.json"))
            entity_name = f"{name_at(submissions, WINDOW[0])} (CIK {cik})"
            found = [r for r in db.list_records(kind="Entity", where={"cik": company["cik"]}).records]
            entity = found[0] if found else db.create("Entity", text=entity_name, author_id="corpus",
                                                      attributes={"cik": company["cik"], "category": "company"})
            rows = filings(submissions)
            print(f"{entity_name}: {len(rows)} filings", flush=True)
            for row in rows:
                accession, day = row["accessionNumber"], row["filingDate"]
                documents = [("primary", f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/"
                                         f"{row['primaryDocument']}")]
                if row["form"].startswith("8-K"):
                    documents += exhibits(cik, accession)
                for exhibit, url in documents:
                    if url in done:
                        continue
                    try:
                        text = fetch_page(url, contact=AGENT).text
                    except Exception as error:
                        print("  failed", url, error, flush=True)
                        continue
                    name = name_at(submissions, day)
                    what = row["form"] if exhibit == "primary" else f"{row['form']} {exhibit}"
                    items = f", items {row['items']}" if row["items"] else ""
                    description = (f"{name} {what} filed {day}{items}"
                                   + (f" for the period ended {row['reportDate']}" if row["reportDate"] else "")
                                   + f" (SEC accession {accession})")
                    attributes = {"published": day, "document_type": row["form"], "exhibit": exhibit,
                                  "cik": company["cik"], "accession": accession, "url": url,
                                  "period_end": row["reportDate"] or None, "items": row["items"] or None}
                    try:
                        source = db.ingest_source(description=description, document_text=text, author_id="corpus",
                                                  attributes=attributes)
                    except ValueError as error:
                        print("  skipped", url, str(error)[:80], flush=True)
                        done[url] = None
                        continue
                    db.create_link(source.id, entity.id, "about", author_id="corpus")
                    db.chunk_source(source.id)
                    done[url] = source.id
                    DONE.write_text(json.dumps(done))
            print(f"  {sum(1 for v in done.values() if v)} documents in all", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
