"""Rerun the dates on which a value written before readings could carry `expires` lapsed with no new filing.

For bonus_weight_on_growth the reading lapses the day after its latest proxy (DEF 14A) turns 15 months old;
for inventory_disclosure, the day after its latest annual report or quarterly earnings materials (a 10-K, or
an 8-K exhibit filed under item 2.02) does. Where the table still holds a non-null value on the first
scheduled date on or after that day, this removes the checks made without a session from that date up to
the next agent reading, runs one session at that date with confirmation without a session turned off, and
leaves the rest to an ordinary run.

Usage: python tools/rerun_expired.py [--apply]
"""

from calendar import monthrange
from datetime import date, timedelta
import json
from pathlib import Path
import subprocess
import sys

from stated import RecordStore

HERE = Path(__file__).resolve().parent.parent
DB = HERE / "research.sqlite"
STATED = "/Users/zach/Code/PremiseDB/.venv/bin/stated"
END = "2024-12-31"
AUTOMATIC = "/no-new-documents"


def lapse(published: str) -> str:
    day = date.fromisoformat(published)
    month = day.month + 15
    year, month = day.year + (month - 1) // 12, (month - 1) % 12 + 1
    return (date(year, month, min(day.day, monthrange(year, month)[1])) + timedelta(1)).isoformat()


def plan():
    with RecordStore(DB) as db:
        features = {d.key: d for d in db.list_features()}
        subjects = [line.strip() for line in (HERE / "subjects.txt").read_text().split() if line.strip()]
        found = []
        for subject in subjects:
            key_of = db._record_key(subject)
            docs = db._db.execute(
                """SELECT json_extract(s.attributes, '$.published'), json_extract(s.attributes, '$.document_type'),
                          json_extract(s.attributes, '$.exhibit'), coalesce(json_extract(s.attributes, '$.items'), '')
                   FROM records s JOIN links a ON a.from_key = s.record_key
                   WHERE s.kind = 'Source' AND a.relation = 'about' AND a.to_key = ?""", (key_of,)).fetchall()
            kinds = {"bonus_weight_on_growth": [p for p, form, _, _ in docs if form == "DEF 14A"],
                     "inventory_disclosure": [p for p, form, exhibit, items in docs if form in ("10-K", "10-K/A")
                                              or (form.startswith("8-K") and exhibit != "primary" and "2.02" in items)]}
            for key, published in kinds.items():
                feature = features[key]
                dates = sorted(row[0] for row in db._db.execute(
                    f"""SELECT v.as_of FROM records v JOIN links a ON a.from_key = v.record_key
                        WHERE json_extract(v.attributes, '$.feature') = ? AND a.relation = 'about' AND a.to_key = ?
                        UNION SELECT c.as_of FROM feature_checks c JOIN records v ON v.record_key = c.value_key
                        JOIN links a ON a.from_key = v.record_key
                        WHERE json_extract(v.attributes, '$.feature') = ? AND a.relation = 'about' AND a.to_key = ?
                          AND c.author_id NOT LIKE '%{AUTOMATIC}'""", (feature.id, key_of, feature.id, key_of)))
                for day in _weeks(feature.start, END):
                    with RecordStore(DB, as_of=day) as at:
                        from stated.features import _latest
                        value = _latest(at, feature.id, subject)
                    if value is None or value.attributes.get("value") is None:
                        continue
                    seen = [p for p in published if p <= day]
                    if seen and lapse(max(seen)) <= day and lapse(max(seen)) > max(d for d in dates if d <= day):
                        until = min([d for d in dates if d > day] + ["9999-12-31"])
                        found.append((subject, key, day, until, value.id))
                        break
        return found


def _weeks(start: str, end: str):
    day = date.fromisoformat(start)
    while day.isoformat() <= end:
        yield day.isoformat()
        day += timedelta(7)


if __name__ == "__main__":
    found = plan()
    with RecordStore(DB) as db:
        for subject, key, day, until, value_id in found:
            checks = db._db.execute(
                f"""SELECT c.check_key FROM feature_checks c WHERE c.value_key = ? AND c.as_of >= ? AND c.as_of < ?
                      AND c.author_id LIKE '%{AUTOMATIC}'""", (db._record_key(value_id), day, until)).fetchall()
            print(db.read(subject).text, key, "from", day, "until", until, len(checks), "checks to remove")
            if "--apply" in sys.argv[1:]:
                with db._db:
                    db._db.executemany("DELETE FROM feature_checks WHERE check_key = ?", checks)
    if "--apply" in sys.argv[1:]:
        for subject, key, day, until, value_id in sorted(found, key=lambda f: f[2]):
            run = subprocess.run([STATED, "--db", str(DB), "run-features", key, "--subject", subject, "--from", day,
                                  "--to", day, "--every-date", "--author", "run/claude-code"],
                                 capture_output=True, text=True)
            print(run.stdout.strip(), run.stderr.strip()[-600:], flush=True)
