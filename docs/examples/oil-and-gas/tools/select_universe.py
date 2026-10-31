"""Select the showcase universe from SEC data: companies reporting total assets at the 2021 year-end
instant (XBRL frame CY2021Q4I) of at least $3B whose SEC industry code (SIC) is 1311, crude petroleum and
natural gas. Writes edgar/universe.json. SEC requests carry the contact in contact.txt and stay under the
fair-access rate."""

import json
from pathlib import Path
import time
import urllib.request

HERE = Path(__file__).resolve().parent.parent
AGENT = (HERE / "contact.txt").read_text().strip()
THRESHOLD = 3e9


def get(url):
    request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(request, timeout=60) as response:
        time.sleep(0.15)
        return json.load(response)


frame = get("https://data.sec.gov/api/xbrl/frames/us-gaap/Assets/USD/CY2021Q4I.json")["data"]
large = [row for row in frame if row["val"] >= THRESHOLD]
print(f"{len(frame)} companies report assets at the 2021 year-end; {len(large)} at or above $3B")
chosen = []
for n, row in enumerate(large):
    cik = f"{row['cik']:010d}"
    try:
        submissions = get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    except Exception as error:
        print("skip", cik, error)
        continue
    if str(submissions.get("sic")) == "1311":
        chosen.append({"cik": cik, "name": submissions["name"], "assets_2021": row["val"],
                       "tickers": submissions.get("tickers", []), "exchanges": submissions.get("exchanges", []),
                       "sic": submissions["sic"], "fiscal_year_end": submissions.get("fiscalYearEnd")})
    if n % 200 == 0:
        print(n, len(chosen), flush=True)
(HERE / "edgar" / "universe.json").write_text(json.dumps(chosen, indent=1))
print(len(chosen), "chosen")
