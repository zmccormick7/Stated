"""A feature run: the sessions `pending` lists for some features and a list of subjects. One fresh agent
session, opened at a date's cutoff, takes every feature due for a subject on that date, writing each one's
value or confirming that its latest value still holds. A feature with no new document about the subject
since it was last read is confirmed without a session: a document is about a subject when its Source is
linked `about` the subject's Entity.

Subjects run in parallel, up to the number of workers; each subject's dates run in order, since each session
starts from the value the one before left. A session that ends without its date done stops that subject's
run, because the dates after it would start from the wrong value. The pending list is the run's only state:
a run that stops, for whatever reason, is picked up by running it again. Sessions run in an empty folder,
with the web off and Stated's own fetching off, so what they can read is what the cutoff shows them.
After a subject's sessions, its derived features are computed mechanically, date by date.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

from .store import RecordStore
from .ui.harness import HarnessError, UsageLimit

RETRY_MINUTES = 15  # How long to wait at a usage limit when the harness does not say when it resets.
_sleep = time.sleep


@dataclass(frozen=True)
class SessionResult:
    subject_id: str
    subject: str
    date: str
    keys: tuple[str, ...]  # The features due on the date that this result covers.
    done: bool  # Each of them has a value or a check for the subject on the date.
    detail: str  # The agent's last message, why the session failed, or why no session was needed.
    session: bool  # Whether an agent session ran; False when nothing new had been published.


@dataclass(frozen=True)
class DerivedAt:
    subject_id: str
    date: str
    key: str
    value: object
    id: str | None  # The value written, if any.
    problem: str | None  # Why none was, if it could not be.


@dataclass(frozen=True)
class FeatureRun:
    keys: tuple[str, ...]
    sessions: tuple[SessionResult, ...]
    remaining: int  # Scheduled dates still pending over the subjects and range when the run ended.
    derived: tuple[DerivedAt, ...] = ()


def derive_features(db_path: str | Path, subject_ids, *, author_id: str, start: str | None = None,
                    end: str | None = None) -> tuple[DerivedAt, ...]:
    """Compute each subject's derived features at every date from `start` to `end` on which a value of a
    feature they read is dated, in date order, each from the inputs recorded as of that date. Running it
    again after more inputs are written brings the derived values in line: a date's newer value is the
    latest version for that date."""
    from .features import derive
    from .formulas import parse
    db_path = Path(db_path).resolve()
    with RecordStore(db_path) as db:
        subjects = [db.read(db.resolve_id(s)).id for s in dict.fromkeys(subject_ids)]
        current = {d.key: d for d in db.list_features()}
        read = {name for d in current.values() if d.formula for name in parse(d.formula)[1] if name in current}
        dates = {subject: [] for subject in subjects}
        if not read:
            return ()
        for subject in subjects:  # Each subject's dates on which an input changed, in order.
            dates[subject] = sorted({row[0] for row in db._db.execute(
                f"""SELECT v.as_of FROM records v JOIN links a ON a.from_key = v.record_key
                    WHERE v.kind = 'Variable' AND a.relation = 'about' AND a.to_key = ?
                      AND json_extract(v.attributes, '$.feature') IN ({','.join('?' * len(read))})
                      AND v.as_of BETWEEN ? AND ?""",
                (db._record_key(subject), *(current[k].id for k in sorted(read)), start or "0000-00-00",
                 end or "9999-99-99"))})
    results = []
    for subject in subjects:
        for day in dates[subject]:
            with RecordStore(db_path, as_of=day) as store:
                results += [DerivedAt(subject, day, d.key, d.value, d.id, d.problem)
                            for d in derive(store, subject, author_id=author_id)]
    return tuple(results)


def run_features(db_path: str | Path, features, subject_ids, *, harness, workers: int = 1,
                 start: str | None = None, end: str | None = None, author_id: str | None = None,
                 max_sessions: int | None = None, session_minutes: float = 20, skip_unchanged: bool = True,
                 on_result=lambda result: None) -> FeatureRun:
    """Run the pending dates of `features` over `subject_ids` from `start` to `end`, as pending_work reads
    them, with up to `workers` subjects at a time and at most `max_sessions` agent sessions. At each of a
    subject's dates, in order, a feature that has a value and no new document about the subject since it
    was last read is confirmed without a session, unless `skip_unchanged` is False; the others share one
    session. A usage limit is waited out and the session tried again."""
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 32:
        raise ValueError("workers must be an integer from 1 to 32")
    if max_sessions is not None and (not isinstance(max_sessions, int) or max_sessions < 1):
        raise ValueError("max_sessions must be a positive integer")
    if isinstance(features, str):
        features = [features]
    db_path = Path(db_path).resolve()
    subject_ids = list(subject_ids)
    with RecordStore(db_path) as db:
        definitions = list({d.id: d for d in (db.read_feature(f) for f in features)}.values())
        if not definitions:
            raise ValueError("name one or more features")
        works = [db.pending_work(d.id, subject_ids, start=start, end=end, limit=100000) for d in definitions]
    due: dict[str, dict[str, list]] = {}  # Subject → date → the definitions due then.
    names = {}
    for definition, work in zip(definitions, works):
        for task in work.tasks:
            names[task.subject_id] = task.subject
            due.setdefault(task.subject_id, {}).setdefault(task.date, []).append(definition)
    author = author_id or f"feature-run/{harness.name}"
    lock, results, derived, started = threading.Lock(), [], [], [0]

    def claim() -> bool:
        with lock:
            if max_sessions is not None and started[0] >= max_sessions:
                return False
            started[0] += 1
            return True

    def report(result) -> None:
        with lock:
            results.append(result)
        on_result(result)

    def subject_run(subject_id: str) -> None:
        for day, definitions_due in sorted(due[subject_id].items()):
            if skip_unchanged:
                definitions_due = _confirm_unchanged(db_path, subject_id, names[subject_id], day, definitions_due,
                                                     f"{author}/no-new-documents", report)
            if not definitions_due:
                continue
            if not claim():
                break
            result = _session(db_path, definitions_due, subject_id, names[subject_id], day, harness, author,
                              session_minutes)
            report(result)
            if not result.done:
                break
        made = derive_features(db_path, [subject_id], author_id=author, start=min(w.start for w in works),
                               end=max(w.end for w in works))
        with lock:
            derived.extend(made)
        for item in made:
            on_result(item)

    with ThreadPoolExecutor(workers) as pool:
        for future in [pool.submit(subject_run, subject) for subject in due]:
            future.result()
    with RecordStore(db_path) as db:
        remaining = sum(db.pending_work(d.id, subject_ids, start=start, end=w.end, limit=1).total
                        for d, w in zip(definitions, works))
    return FeatureRun(tuple(d.key for d in definitions), tuple(results), remaining, tuple(derived))


def _confirm_unchanged(db_path: Path, subject_id: str, subject: str, day: str, definitions, author: str, report):
    """Confirm, without a session, each definition whose latest value about the subject was last read on or
    after the latest publication date of a document about it; return the definitions that need a session."""
    from .features import _latest
    left = []
    with RecordStore(db_path, as_of=day) as db:
        published = db._db.execute(
            f"""SELECT max(substr(json_extract(s.attributes, '$.published'), 1, 10)) FROM records s
                JOIN links a ON a.from_key = s.record_key
                WHERE s.kind = 'Source' AND a.relation = 'about' AND a.to_key = ? AND {db._seen('s')}""",
            (db._record_key(subject_id),)).fetchone()[0]
        for definition in definitions:
            latest = _latest(db, definition.id, subject_id)
            read_on = None if latest is None else max([latest.as_of] + [row[0] for row in db._db.execute(
                "SELECT as_of FROM feature_checks WHERE value_key = ? AND as_of < ?",
                (db._record_key(latest.id), day))])
            if latest is None or (published is not None and published > read_on):
                left.append(definition)
                continue
            db.confirm_value(latest.id, author_id=author)
            report(SessionResult(subject_id, subject, day, (definition.key,), True,
                                 f"No new document about the subject since {read_on}; confirmed {latest.id}.", False))
    return left


def _session(db_path: Path, definitions, subject_id: str, subject: str, day: str, harness, author: str,
             minutes: float) -> SessionResult:
    """One fresh session at the date's cutoff for every feature due then, tried again after a usage limit;
    done when each has a value or a check for the subject on the date afterwards."""
    env = os.environ | {"STATED_DB": str(db_path), "STATED_AS_OF": day, "STATED_FETCH": "0",
                        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    while True:
        try:
            with tempfile.TemporaryDirectory() as folder:
                reply = harness.run(_prompt(definitions, subject_id, subject, day, author), cwd=folder, env=env,
                                    web=False, writable=(db_path.parent,), timeout=minutes * 60)
            detail = reply.text.strip()
            break
        except UsageLimit as limit:
            _sleep(limit.resets_at - time.time() + 30 if limit.resets_at and limit.resets_at > time.time()
                   else RETRY_MINUTES * 60)
        except HarnessError as error:
            detail = f"The session failed: {error}"
            break
    with RecordStore(db_path, as_of=day) as db:
        undone = [d.key for d in definitions
                  if db.pending_work(d.id, [subject_id], start=day, end=day).total]
    if undone and not detail.startswith("The session failed"):
        detail = f"The session ended without a value or a check for {', '.join(undone)} on {day}: {detail}"
    return SessionResult(subject_id, subject, day, tuple(d.key for d in definitions), not undone, detail[:2000], True)


def _prompt(definitions, subject_id: str, subject: str, day: str, author: str) -> str:
    listed = "\n".join(f"- {d.key} (definition {d.id})" for d in definitions)
    return (f"Use the stated-state skill for each feature below, one at a time. This session is at the cutoff "
            f"{day}: every `stated` command reads and writes as of that date. `stated feature ID` shows a "
            f"feature's instructions.\n\nSubject: {subject} (Entity {subject_id}).\nFeatures:\n{listed}\n\n"
            f"For each feature, write its value for this subject as of {day}, or, if its latest value still holds, "
            f"confirm it. Use author_id \"{author}\". Finish with one line per feature: its key, then the value's "
            f"ID and value, or the confirmed value's ID.")
