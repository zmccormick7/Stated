"""A feature run: the sessions `pending` lists for one feature and a list of subjects, each a fresh agent
session opened at its date's cutoff that writes the feature's value for one subject or confirms that the
latest value still holds.

Subjects run in parallel, up to the number of workers; each subject's dates run in order, since each session
starts from the value the one before left. A session that ends without its date done stops that subject's
run, because the dates after it would start from the wrong value. The pending list is the run's only state:
a run that stops, for whatever reason, is picked up by running it again. Sessions run in an empty folder,
with the web off and PremiseDB's own fetching off, so what they can read is what the cutoff shows them.
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
    done: bool  # The date has a value or a check for the subject.
    detail: str  # The agent's last message, or why the session failed.


@dataclass(frozen=True)
class FeatureRun:
    feature_id: str
    key: str
    sessions: tuple[SessionResult, ...]
    remaining: int  # Scheduled dates still pending over the subjects and range when the run ended.


def run_features(db_path: str | Path, feature: str, subject_ids, *, harness, workers: int = 1,
                 start: str | None = None, end: str | None = None, author_id: str | None = None,
                 max_sessions: int | None = None, session_minutes: float = 20,
                 on_result=lambda result: None) -> FeatureRun:
    """Run the pending sessions for `feature` over `subject_ids` from `start` to `end`, as pending_work
    reads them, with up to `workers` subjects at a time and at most `max_sessions` sessions. A usage limit
    is waited out and the session tried again."""
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 32:
        raise ValueError("workers must be an integer from 1 to 32")
    if max_sessions is not None and (not isinstance(max_sessions, int) or max_sessions < 1):
        raise ValueError("max_sessions must be a positive integer")
    db_path = Path(db_path).resolve()
    subject_ids = list(subject_ids)
    with RecordStore(db_path) as db:
        definition = db.read_feature(feature)
        work = db.pending_work(definition.id, subject_ids, start=start, end=end, limit=100000)
    by_subject: dict[str, list] = {}
    for task in work.tasks:
        by_subject.setdefault(task.subject_id, []).append(task)
    author = author_id or f"feature-run/{harness.name}"
    lock, results, started = threading.Lock(), [], [0]

    def claim() -> bool:
        with lock:
            if max_sessions is not None and started[0] >= max_sessions:
                return False
            started[0] += 1
            return True

    def subject_run(tasks) -> None:
        for task in tasks:
            if not claim():
                return
            result = _session(db_path, definition, task, harness, author, session_minutes)
            with lock:
                results.append(result)
            on_result(result)
            if not result.done:
                return

    with ThreadPoolExecutor(workers) as pool:
        for future in [pool.submit(subject_run, tasks) for tasks in by_subject.values()]:
            future.result()
    with RecordStore(db_path) as db:
        remaining = db.pending_work(definition.id, subject_ids, start=start, end=work.end, limit=1).total
    return FeatureRun(definition.id, definition.key, tuple(results), remaining)


def _session(db_path: Path, definition, task, harness, author: str, minutes: float) -> SessionResult:
    """One fresh session at the task's cutoff, tried again after a usage limit; done when the date has a
    value or a check for the subject afterwards."""
    env = os.environ | {"PREMISEDB_DB": str(db_path), "PREMISEDB_AS_OF": task.date, "PREMISEDB_FETCH": "0",
                        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    while True:
        try:
            with tempfile.TemporaryDirectory() as folder:
                reply = harness.run(_prompt(definition, task, author), cwd=folder, env=env, web=False,
                                    writable=(db_path.parent,), timeout=minutes * 60)
            detail = reply.text.strip()
            break
        except UsageLimit as limit:
            _sleep(limit.resets_at - time.time() + 30 if limit.resets_at and limit.resets_at > time.time()
                   else RETRY_MINUTES * 60)
        except HarnessError as error:
            detail = f"The session failed: {error}"
            break
    with RecordStore(db_path, as_of=task.date) as db:
        done = db.pending_work(definition.id, [task.subject_id], start=task.date, end=task.date).total == 0
    if not done and not detail.startswith("The session failed"):
        detail = f"The session ended without a value or a check for {task.date}: {detail}"
    return SessionResult(task.subject_id, task.subject, task.date, done, detail[:2000])


def _prompt(definition, task, author: str) -> str:
    return (f"Use the premisedb-state skill. This session is at the cutoff {task.date}: every premisedb command "
            f"reads and writes as of that date.\n\n"
            f"Feature: {definition.key} (definition {definition.id}); `premisedb feature {definition.id}` shows its "
            f"instructions.\nSubject: {task.subject} (Entity {task.subject_id}).\n\n"
            f"Write this feature's value for this subject as of {task.date}, or, if the latest value still holds, "
            f"confirm it. Use author_id \"{author}\". Finish with one line: the value's ID and value, or the "
            f"confirmed value's ID.")
