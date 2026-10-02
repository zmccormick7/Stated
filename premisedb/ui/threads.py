"""Threads of questions and their answers, and the run that turns a question into a checked answer."""

from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
import json
import os
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

from ..answers import AnswerCheck, check_answer
from ..store import RecordStore
from .harness import HarnessError

_VERSION = 1


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
        return dict(row) | {"turns": [dict(turn) | {key: json.loads(turn[key]) if turn[key] else None for key in
                                                    ("progress", "answer", "written", "denied")} for turn in turns]}

    def start_turn(self, thread_id: str, question: str) -> int:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a nonblank string")
        self.thread(thread_id)
        with self._db() as db:
            return db.execute("INSERT INTO turns (thread_id, question, status, started_at) VALUES (?, ?, 'running', ?)",
                              (thread_id, question, _now())).lastrowid

    def add_progress(self, turn_id: int, text: str) -> None:
        with self._db() as db:
            db.execute("UPDATE turns SET progress = json_insert(progress, '$[#]', ?) WHERE id = ?", (text, turn_id))

    def finish_turn(self, turn_id: int, *, answer: dict | None = None, error: str | None = None,
                    written: dict | None = None, denied: tuple[str, ...] = ()) -> None:
        with self._db() as db:
            db.execute("""UPDATE turns SET status = ?, answer = ?, error = ?, written = ?, denied = ?, finished_at = ?
                          WHERE id = ?""", ("failed" if answer is None else "done",
                                            None if answer is None else json.dumps(answer), error,
                                            json.dumps(written or {}), json.dumps(list(denied)), _now(), turn_id))

    def _turn(self, turn_id: int) -> sqlite3.Row:
        with self._db() as db:
            return db.execute("""SELECT t.thread_id, t.question, t.started_at, th.session_id FROM turns t
                                 JOIN threads th ON th.id = t.thread_id WHERE t.id = ?""", (turn_id,)).fetchone()

    def _set_session(self, thread_id: str, session_id: str | None) -> None:
        with self._db() as db:
            db.execute("UPDATE threads SET session_id = coalesce(?, session_id) WHERE id = ?", (session_id, thread_id))


def run_turn(store: ThreadStore, turn_id: int, *, db_path: str | Path, harness) -> None:
    """Answer a started turn: run its question through the harness in the workspace, check the answer
    document against the graph, give the agent one chance to fix what the check reports, and store the
    result. The agent works with the workspace's own settings and may add to the graph as it goes."""
    turn, db_path = store._turn(turn_id), Path(db_path).resolve()
    run = partial(harness.run, cwd=db_path.parent, on_progress=partial(store.add_progress, turn_id), env=os.environ | {
        "PREMISEDB_DB": str(db_path), "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"})
    denied: tuple[str, ...] = ()
    try:
        reply = run(_prompt(turn["question"], harness.name, first=turn["session_id"] is None),
                    session_id=turn["session_id"])
        store._set_session(turn["thread_id"], reply.session_id)
        check, denied = _check(db_path, reply.text), reply.denied
        if not check.ok:
            store.add_progress(turn_id, "Fixing the answer")
            reply = run("The answer document did not pass check-answer:\n- " + "\n- ".join(check.problems)
                        + "\nFix these and reply with the corrected JSON document alone.",
                        session_id=reply.session_id or turn["session_id"])
            check, denied = _check(db_path, reply.text), tuple(dict.fromkeys(denied + reply.denied))
    except HarnessError as error:
        store.finish_turn(turn_id, error=str(error), denied=denied)
        return
    with RecordStore(db_path) as db:
        written, own = {}, db.layers()[0].name
        for record in db.list_records(since=turn["started_at"], limit=1000).records:
            if record.layer == own:
                written[record.kind] = written.get(record.kind, 0) + 1
    store.finish_turn(turn_id, answer=check.answer, written=written, denied=denied, error=None if check.ok else (
        "The agent's answer did not pass the check: " + "; ".join(check.problems)))


def _prompt(question: str, author: str, *, first: bool) -> str:
    """The skills are installed in the harness, as the README describes; the prompt only names them."""
    if not first:
        return f"Answer this follow-up as an answer document in the same format, checked with check-answer.\n\n{question}"
    return ("Use the premisedb-research skill for the research and the premisedb-answer skill for the reply. "
            f"The research database is the one PREMISEDB_DB names; use the author ID {author} for anything you write."
            f"\n\nQuestion: {question}")


def _check(db_path: Path, text: str) -> AnswerCheck:
    """Check the JSON document in the agent's final message, tolerating a code fence or prose around it."""
    start, end = text.find("{"), text.rfind("}")
    try:
        document = json.loads(text[start:end + 1]) if start != -1 else None
    except ValueError:
        document = None
    if document is None:
        return AnswerCheck(False, ("the reply was not a JSON answer document",), None)
    with RecordStore(db_path) as db:
        return check_answer(db, document)
