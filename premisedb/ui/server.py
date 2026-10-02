"""A local HTTP server for the answer UI: threads of questions, answered by the user's own agent."""

from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlparse

from ..store import RecordStore
from .harness import ClaudeCode, Codex
from .threads import ThreadStore, run_turn

STATIC = Path(__file__).with_name("static")
HARNESSES = {"claude-code": ClaudeCode, "codex": Codex}
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


def missing_skills(harness) -> list[str]:
    """The bundled skills the harness cannot see; the README documents the copy that installs them."""
    return [name for name in ("premisedb-research", "premisedb-answer")
            if not any((directory / name / "SKILL.md").is_file() for directory in harness.skills_directories())]


class App:
    """The state behind the handler: the workspace, its thread store and the harness that answers."""

    def __init__(self, db_path: str | Path, *, harness):
        self.db_path = Path(db_path).resolve()
        self.store = ThreadStore(self.db_path.with_name(self.db_path.stem + ".threads.sqlite"))
        self.harness = harness

    def evaluate(self, model_id: str, overrides) -> dict:
        """A model's values with some Variables held at other values; the lever block calls this as it moves."""
        with RecordStore(self.db_path) as db:
            result = db.evaluate_model(model_id, overrides)
        return {"values": {v.key: v.value for v in result.variables},
                "changed": [v.key for v in result.variables if v.changed]}

    def ask(self, thread_id: str | None, question: str) -> dict:
        """Start a turn and answer it in the background; one turn at a time per thread."""
        if thread_id is None:
            thread_id = self.store.create_thread(question, harness=self.harness.name)
        elif any(turn["status"] == "running" for turn in self.store.thread(thread_id)["turns"]):
            raise ValueError("this thread is still answering the previous question")
        turn_id = self.store.start_turn(thread_id, question)
        threading.Thread(target=run_turn, args=(self.store, turn_id),
                         kwargs={"db_path": self.db_path, "harness": self.harness}, daemon=True).start()
        return {"thread_id": thread_id, "turn_id": turn_id}


class Handler(BaseHTTPRequestHandler):
    def __init__(self, app: App, *args, **kwargs):
        self.app = app
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):  # Quiet by default; the terminal shows only the address.
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._file("index.html")
        if path.startswith("/static/") and "/" not in path[8:] and ".." not in path:
            return self._file(path[8:])
        if path == "/api/threads":
            return self._json(self.app.store.threads())
        if path.startswith("/api/threads/"):
            try:
                return self._json(self.app.store.thread(path[13:]))
            except KeyError:
                return self._error(HTTPStatus.NOT_FOUND, "no such thread")
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("the request body must be a JSON object")
            if path == "/api/threads":
                self._json(self.app.ask(None, body.get("question")), HTTPStatus.CREATED)
            elif path.startswith("/api/threads/") and path.endswith("/turns"):
                self._json(self.app.ask(path[13:-6], body.get("question")), HTTPStatus.CREATED)
            elif path.startswith("/api/models/") and path.endswith("/evaluate"):
                self._json(self.app.evaluate(path[12:-9], body.get("overrides")))
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except KeyError as error:
            self._error(HTTPStatus.NOT_FOUND, f"not found: {error.args[0] if error.args else ''}")
        except (ValueError, TypeError) as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))

    def _file(self, name):
        path = STATIC / name
        if not path.is_file() or path.suffix not in _TYPES:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        self._send(HTTPStatus.OK, _TYPES[path.suffix], path.read_bytes())

    def _json(self, value, status=HTTPStatus.OK):
        self._send(status, "application/json", json.dumps(value).encode("utf-8"))

    def _error(self, status, message):
        self._json({"error": message}, status)

    def _send(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def make_server(db_path, *, harness, host="127.0.0.1", port=0) -> ThreadingHTTPServer:
    """A server bound to localhost; port 0 picks a free one. Call serve_forever() to run it."""
    return ThreadingHTTPServer((host, port), partial(Handler, App(db_path, harness=harness)))


def serve(db_path, *, harness_name="claude-code", port=8765, model=None, reasoning_effort=None) -> int:
    """Run the answer UI for a workspace until interrupted; returns the exit status."""
    if harness_name not in HARNESSES:
        raise ValueError(f"unknown harness {harness_name}; choose from {', '.join(HARNESSES)}")
    if harness_name != "codex" and (model or reasoning_effort):
        raise ValueError("--model and --effort apply to the codex harness")
    harness = Codex(model=model, reasoning_effort=reasoning_effort) if harness_name == "codex" else ClaudeCode()
    if missing := missing_skills(harness):
        root = Path(__file__).resolve().parents[2] / "skills"
        print(f"{harness.name} cannot see the {' and '.join(missing)} skill(s). Install them with:")
        for name in missing:
            print(f"  cp -R {root / name} {harness.skills_directories()[0]}/")
        return 1
    if advice := harness.permission_advice(Path(db_path).resolve().parent):
        print(advice)
        return 1
    server = make_server(db_path, harness=harness, port=port)
    print(f"PremiseDB answer UI for {Path(db_path).resolve()} at http://127.0.0.1:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
