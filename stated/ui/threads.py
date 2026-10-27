"""Threads of questions and their answers, and the run that turns a question into a checked answer."""

from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4

from ..answers import AnswerCheck, check_answer
from ..store import RecordStore
from .harness import HarnessError

_VERSION = 5

# What the user may give a turn, in minutes: what the agent is told, whether it may use the web, and
# whether an agent that finishes early is asked to go on.
EFFORTS = {
    1: ("You have about one minute. Answer from what the graph already holds: a few lookups, no new records "
        "and no outside sources.", False, False),
    5: ("You have about five minutes.", False, False),
    20: ("You have about twenty minutes. Where the graph falls short, go to outside sources on the web, ingest "
         "what you rely on with `stated ingest --url` and add what you learn to your layer.", True, True),
}
RUN_EFFORTS = (60, 180, 480)  # Longer than one session can hold: planned, worked an item at a time, then reported (runs.py).
DEFAULT_EFFORT = 5
AGENTS = (1, 2, 3, 5, 10)  # How many workers a run may have at a time.
DEFAULT_AGENTS = 3
USE_OF_TIME = 0.75  # An agent that has used less of its time than this is asked whether more work would help.


def time_limit(effort: int) -> float:
    """Seconds after which a turn is stopped: twice its effort, and never less than five minutes over."""
    return 60 * max(2 * effort, effort + 5)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class ThreadStore:
    """Conversation threads in their own SQLite file beside the workspace; the graph holds the knowledge."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        with self._db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and not db.execute("SELECT 1 FROM sqlite_master").fetchone():
                db.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
                db.execute(f"PRAGMA user_version = {_VERSION}")
            elif version != _VERSION:
                raise ValueError(f"Incompatible thread store version {version}; expected {_VERSION}. "
                                 f"Delete {self.path} to start a new one.")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_thread(self, question: str, *, harness: str) -> str:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a nonblank string")
        title = " ".join(question.split())
        thread_id = str(uuid4())
        with self._db() as db:
            db.execute("INSERT INTO threads (id, title, harness, created_at) VALUES (?, ?, ?, ?)",
                       (thread_id, title if len(title) <= 80 else title[:79] + "…", harness, _now()))
        return thread_id

    def threads(self) -> list[dict]:
        """Every thread, newest first."""
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT id, title, harness, created_at FROM threads ORDER BY created_at DESC, rowid DESC")]

    def thread(self, thread_id: str) -> dict:
        """A thread with its turns in order; a missing thread raises KeyError."""
        with self._db() as db:
            row = db.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if row is None:
                raise KeyError(thread_id)
            turns = db.execute("SELECT * FROM turns WHERE thread_id = ? ORDER BY id", (thread_id,)).fetchall()
            items = db.execute("""SELECT w.id, w.turn_id, w.title, w.text, w.status, w.result, w.progress FROM work_items w
                                  JOIN turns t ON t.id = w.turn_id WHERE t.thread_id = ? ORDER BY w.id""", (thread_id,)).fetchall()
            notes = db.execute("""SELECT n.id, n.turn_id, n.text, n.created_at FROM notes n
                                  JOIN turns t ON t.id = n.turn_id WHERE t.thread_id = ? ORDER BY n.id""", (thread_id,)).fetchall()
        return dict(row) | {"turns": [dict(turn) | {key: json.loads(turn[key]) if turn[key] else None for key in
                                                    ("progress", "answer", "written", "denied")}
                                      | {"items": [dict(item) for item in items if item["turn_id"] == turn["id"]],
                                         "notes": [dict(note) for note in notes if note["turn_id"] == turn["id"]]}
                                      for turn in turns]}

    def start_turn(self, thread_id: str, question: str, effort: int = DEFAULT_EFFORT, agents: int = 1) -> int:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a nonblank string")
        self.thread(thread_id)
        with self._db() as db:
            return db.execute("""INSERT INTO turns (thread_id, question, effort, agents, status, started_at)
                                 VALUES (?, ?, ?, ?, 'running', ?)""", (thread_id, question, effort, agents, _now())).lastrowid

    def add_items(self, turn_id: int, items: list[tuple[str, str]]) -> None:
        """Queue pieces of work for a run, in order, each a (title, text) pair."""
        with self._db() as db:
            db.executemany("INSERT INTO work_items (turn_id, title, text) VALUES (?, ?, ?)",
                           [(turn_id, title, text) for title, text in items])

    def next_item(self, turn_id: int) -> dict | None:
        """Claim the first piece of work nobody has started; None when the queue is empty."""
        with self._db() as db:
            row = db.execute("""UPDATE work_items SET status = 'running', started_at = ? WHERE id = (
                                    SELECT id FROM work_items WHERE turn_id = ? AND status = 'todo' ORDER BY id LIMIT 1)
                                RETURNING id, title, text""", (_now(), turn_id)).fetchone()
        return None if row is None else dict(row)

    def finish_item(self, item_id: int, *, result: str | None = None, error: str | None = None) -> None:
        with self._db() as db:
            db.execute("UPDATE work_items SET status = ?, result = ?, finished_at = ? WHERE id = ?",
                       ("done" if error is None else "failed", result if error is None else error, _now(), item_id))

    def unfinished(self) -> list[dict]:
        """Turns still marked running, as after the app was stopped mid-answer."""
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT id, thread_id, question, effort, agents, started_at FROM turns WHERE status = 'running' ORDER BY id")]

    def requeue_running(self, turn_id: int) -> int:
        """Put every piece of work a run had in hand back in the queue; returns how many."""
        with self._db() as db:
            return db.execute("UPDATE work_items SET status = 'todo', started_at = NULL WHERE turn_id = ? AND status = 'running'",
                              (turn_id,)).rowcount

    def requeue_item(self, item_id: int) -> None:
        """Put a claimed piece of work back at the front of the queue, as when a usage limit stopped it."""
        with self._db() as db:
            db.execute("UPDATE work_items SET status = 'todo', started_at = NULL WHERE id = ?", (item_id,))

    def items(self, turn_id: int) -> list[dict]:
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT id, title, text, status, result FROM work_items WHERE turn_id = ? ORDER BY id", (turn_id,))]

    def add_progress(self, turn_id: int, text: str, item_id: int | None = None) -> None:
        with self._db() as db:
            db.execute("UPDATE turns SET progress = json_insert(progress, '$[#]', ?) WHERE id = ?", (text, turn_id))
            if item_id is not None:
                db.execute("UPDATE work_items SET progress = ? WHERE id = ?", (text, item_id))

    def control(self, turn_id: int, action: str) -> None:
        """What the user may ask of a running run: pause, resume, or finish (go to the report now)."""
        changes = {"pause": "paused = 1", "resume": "paused = 0", "finish": "finish = 1"}
        if action not in changes:
            raise ValueError("action must be pause, resume or finish")
        with self._db() as db:
            row = db.execute("SELECT status, effort FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if row is None:
                raise KeyError(turn_id)
            if row["status"] != "running" or row["effort"] not in RUN_EFFORTS:
                raise ValueError("only a research run that is still going can be paused, resumed or finished")
            db.execute(f"UPDATE turns SET {changes[action]} WHERE id = ?", (turn_id,))

    def asked(self, turn_id: int) -> tuple[bool, bool]:
        """Whether the user has asked the run to pause, and whether to finish."""
        with self._db() as db:
            row = db.execute("SELECT paused, finish FROM turns WHERE id = ?", (turn_id,)).fetchone()
        return bool(row["paused"]), bool(row["finish"])

    def add_note(self, turn_id: int, text: str) -> None:
        """A note from the user to steer a running run; later sessions of the run see it."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("note must be a nonblank string")
        with self._db() as db:
            row = db.execute("SELECT status, effort FROM turns WHERE id = ?", (turn_id,)).fetchone()
            if row is None:
                raise KeyError(turn_id)
            if row["status"] != "running" or row["effort"] not in RUN_EFFORTS:
                raise ValueError("notes go to a research run that is still going")
            db.execute("INSERT INTO notes (turn_id, text, created_at) VALUES (?, ?, ?)", (turn_id, " ".join(text.split()), _now()))

    def notes(self, turn_id: int) -> list[str]:
        with self._db() as db:
            return [row["text"] for row in db.execute("SELECT text FROM notes WHERE turn_id = ? ORDER BY id", (turn_id,))]

    def finish_turn(self, turn_id: int, *, answer: dict | None = None, error: str | None = None,
                    written: dict | None = None, denied: tuple[str, ...] = ()) -> None:
        with self._db() as db:
            db.execute("""UPDATE turns SET status = ?, answer = ?, error = ?, written = ?, denied = ?, finished_at = ?
                          WHERE id = ?""", ("failed" if answer is None else "done",
                                            None if answer is None else json.dumps(answer), error,
                                            json.dumps(written or {}), json.dumps(list(denied)), _now(), turn_id))

    def _turn(self, turn_id: int) -> sqlite3.Row:
        with self._db() as db:
            return db.execute("""SELECT t.id, t.thread_id, t.question, t.effort, t.agents, t.started_at, th.session_id FROM turns t
                                 JOIN threads th ON th.id = t.thread_id WHERE t.id = ?""", (turn_id,)).fetchone()

    def _set_session(self, thread_id: str, session_id: str | None) -> None:
        with self._db() as db:
            db.execute("UPDATE threads SET session_id = coalesce(?, session_id) WHERE id = ?", (session_id, thread_id))


class Agent:
    """The harness at work on one turn: where it runs, what it may reach, and one clock for the whole turn."""

    def __init__(self, store: ThreadStore, turn_id: int, db_path: Path, harness, *, web: bool, seconds: float):
        self.store, self.turn_id, self.db_path, self.name = store, turn_id, db_path, harness.name
        self.denied: list[str] = []
        self.stop_at = time.monotonic() + seconds
        self._run = partial(harness.run, cwd=db_path.parent, web=web, on_progress=partial(store.add_progress, turn_id),
                            env=os.environ | {"STATED_DB": str(db_path), "STATED_FETCH": "1" if web else "0",
                                              "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"})

    def run(self, prompt: str, session_id: str | None = None, seconds: float | None = None, on_progress=None):
        """One prompt in a new or resumed session, stopped when the turn's time, or `seconds`, runs out."""
        left = self.stop_at - time.monotonic()
        reply = self._run(prompt, session_id=session_id, timeout=max(1.0, left if seconds is None else min(left, seconds)),
                          **({} if on_progress is None else {"on_progress": on_progress}))
        self.denied.extend(reply.denied)
        return reply

    def answer(self, prompt: str, session_id: str | None, may_decline: bool = False) -> tuple[AnswerCheck, str | None, bool]:
        """A prompt whose reply is an answer document and, when the document fails the check, one repair;
        the last value says whether the reply held a document at all, which a reply that may decline need not."""
        reply = self.run(prompt, session_id)
        session_id, check, document = reply.session_id or session_id, _check(self.db_path, reply.text), "{" in reply.text
        if not check.ok and (document or not may_decline):
            self.store.add_progress(self.turn_id, "Fixing the answer")
            reply = self.run("The answer document did not pass check-answer:\n- " + "\n- ".join(check.problems)
                             + "\nFix these and reply with the corrected JSON document alone.", session_id)
            session_id, check = reply.session_id or session_id, _check(self.db_path, reply.text)
        return check, session_id, document

    def fail(self, error: str) -> None:
        self.store.finish_turn(self.turn_id, error=error, denied=tuple(dict.fromkeys(self.denied)))

    def finish(self, turn, check: AnswerCheck) -> None:
        """Store the answer with a count of what the turn added to the workspace's own layer."""
        with RecordStore(self.db_path) as db:
            written = written_since(db, turn["started_at"])
        self.store.finish_turn(self.turn_id, answer=check.answer, written=written, denied=tuple(dict.fromkeys(self.denied)),
                               error=None if check.ok else "The agent's answer did not pass the check: " + "; ".join(check.problems))


def written_since(db: RecordStore, since: str) -> dict[str, int]:
    """Records added or edited in the workspace's own layer since a time, counted by kind."""
    written, own, after = {}, db.layers()[0].name, None
    while True:
        page = db.list_records(since=since, after=after, limit=1000)
        for record in page.records:
            if record.layer == own:
                written[record.kind] = written.get(record.kind, 0) + 1
        if not page.has_more:
            return written
        after = page.records[-1].id


def run_turn(store: ThreadStore, turn_id: int, *, db_path: str | Path, harness) -> None:
    """Answer a started turn: run its question through the harness in the workspace, check the answer
    document against the graph, give the agent one chance to fix what the check reports, and store the
    result. The agent works with the user's own settings and may add to the graph as it goes. The turn's
    effort, in minutes, sets what the agent is told about time, whether it may use the web, and when it is stopped.
    An agent has no clock, so at the longer efforts one that finishes early is told how much time is left
    and asked what would most improve the answer, until it has nothing to add or the time is mostly used.
    The efforts in RUN_EFFORTS are research runs, which runs.py carries out."""
    turn, db_path = store._turn(turn_id), Path(db_path).resolve()
    effort = turn["effort"]
    if effort in RUN_EFFORTS:
        from .runs import run_research
        return run_research(store, turn, db_path=db_path, harness=harness, effort=effort)
    budget, web, goes_on = EFFORTS[effort]
    started = time.monotonic()
    agent = Agent(store, turn_id, db_path, harness, web=web, seconds=time_limit(effort))
    try:
        check, session_id, _ = agent.answer(_prompt(turn["question"], harness.name, budget, first=turn["session_id"] is None),
                                            turn["session_id"])
        store._set_session(turn["thread_id"], session_id)
    except HarnessError as error:
        return agent.fail(str(error))
    while goes_on and check.ok and session_id and time.monotonic() - started < USE_OF_TIME * effort * 60:
        store.add_progress(turn_id, "Looking for what would improve the answer")
        try:
            again, session_id, document = agent.answer(_go_on(time.monotonic() - started, effort), session_id, True)
        except HarnessError:  # The answer in hand stands.
            break
        if not document or not again.ok or again.answer == check.answer:
            break
        check = again
    agent.finish(turn, check)


def _go_on(elapsed: float, effort: int) -> str:
    """What an agent that finished early is told: the time, where answers are usually thin, and that
    stopping is allowed."""
    used = "under a minute" if elapsed < 60 else f"about {round(elapsed / 60)}"
    return (f"You have used {used} of your {effort} minutes; you cannot see a clock, so this is how you know. "
            "The time is there to be used where it would make the answer better. Look at the answer as its reader "
            "would and name what is weakest in it: a figure taken from a summary and not checked against the "
            "source's own text, a primary source you have not read, an earlier period or a counterparty that would "
            "put the figure in context, an open Question in the graph that bears on it, a claim that rests on one "
            "source, or a chart, lever or flow that would show it better than prose. If one of these would change or "
            "strengthen the answer, do that work, add what you learn to your layer, and reply with the updated answer "
            "document alone, checked with check-answer. If the answer is already as good as more time would make "
            "it, reply with the single word DONE.")


def _prompt(question: str, author: str, budget: str, *, first: bool) -> str:
    """The skills are installed in the harness, as the README describes; the prompt only names them."""
    if not first:
        return (f"Answer this follow-up as an answer document in the same format, checked with check-answer. {budget}"
                f"\n\n{question}")
    return ("Use the stated-research skill for the research and the stated-answer skill for the reply. "
            f"The research database is the one STATED_DB names; use the author ID {author} for anything you write. "
            f"{budget}\n\nQuestion: {question}")


def document(text: str) -> dict | None:
    """The JSON object in an agent's final message, tolerating a code fence or prose around it."""
    start, end = text.find("{"), text.rfind("}")
    try:
        found = json.loads(text[start:end + 1]) if start != -1 else None
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def _check(db_path: Path, text: str) -> AnswerCheck:
    """Check the answer document in the agent's final message against the graph."""
    found = document(text)
    if found is None:
        return AnswerCheck(False, ("the reply was not a JSON answer document",), None)
    with RecordStore(db_path) as db:
        return check_answer(db, found)
