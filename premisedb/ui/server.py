"""A local HTTP server for the answer UI: threads of questions, answered by the user's own agent."""

from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sys
import threading
import webbrowser
from urllib.parse import urlparse

from ..store import RecordStore
from .harness import ClaudeCode, Codex
from .threads import ThreadStore, run_turn

STATIC = Path(__file__).with_name("static")
HARNESSES = {"claude-code": ClaudeCode, "codex": Codex}
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


SKILLS = Path(__file__).resolve().parents[1] / "skills"


def install_skills(harness, source: Path = SKILLS) -> list[str]:
    """Copy the bundled skills into the harness's skills directory, replacing older copies; returns the
    skills that were written or updated. The same file copy the README describes, done by the program."""
    written = []
    for directory in harness.skills_directories():
        for skill in sorted(source.glob("*/SKILL.md")):
            target = directory / skill.parent.name / "SKILL.md"
            if not target.is_file() or target.read_bytes() != skill.read_bytes():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(skill, target)
                written.append(skill.parent.name)
    return written


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

    def status(self) -> dict:
        """What the page shows before the first question: the folder, the layers and whether the agent is there."""
        with RecordStore(self.db_path) as db:
            layers = [{"name": l.name, "version": l.version, "writable": l.writable} for l in db.layers()]
        return {"workspace": str(self.db_path.parent), "database": str(self.db_path), "layers": layers,
                "harness": self.harness.name,
                "harness_found": shutil.which(getattr(self.harness, "executable", self.harness.name)) is not None}

    def record(self, record_id: str) -> dict:
        """A record for the reader: its text and attributes, the passages it cites, and one hop of links."""
        with RecordStore(self.db_path) as db:
            record = db.read(db.resolve_id(record_id))
            citations = []
            for citation in record.attributes.get("citations") or []:
                if not isinstance(citation, dict):
                    continue
                quote = citation.get("quote")
                if not isinstance(quote, str):
                    try:
                        quote = db.read_source_text(citation.get("source_id"), start_byte=citation.get("start_byte"),
                                                    end_byte=citation.get("end_byte"))
                    except (KeyError, ValueError):
                        continue
                try:
                    title = db.read(citation["source_id"]).text.split(". ")[0][:120]
                except (KeyError, TypeError):
                    title = None
                citations.append({"quote": quote, "source": citation.get("source_id"), "title": title})
            found = db.connections(record.id, limit=50)
            connections = []
            for link in found.links:
                other = found.records[link.from_id if link.to_id == record.id else link.to_id]
                connections.append({"relation": link.relation, "direction": "in" if link.to_id == record.id else "out",
                                    "explanation": link.explanation, "record": _preview(other)})
        return {"id": record.id, "kind": record.kind, "text": record.text, "layer": record.layer,
                "author_id": record.author_id, "updated_at": record.updated_at,
                "attributes": {k: v for k, v in record.attributes.items() if k != "citations"},
                "citations": citations, "connections": connections, "has_more": found.has_more}

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


def _preview(record) -> dict:
    """What a linked record shows as a chip: its kind, the start of its text, and its status, key and value."""
    text = " ".join(record.text.split())
    keep = {k: record.attributes[k] for k in ("status", "basis", "key", "value") if k in record.attributes}
    return {"id": record.id, "kind": record.kind, "text": text if len(text) <= 200 else text[:199] + "…", **keep}


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
        if path == "/api/status":
            return self._json(self.app.status())
        if path == "/api/threads":
            return self._json(self.app.store.threads())
        if path.startswith("/api/threads/"):
            try:
                return self._json(self.app.store.thread(path[13:]))
            except KeyError:
                return self._error(HTTPStatus.NOT_FOUND, "no such thread")
        if path.startswith("/api/records/"):
            try:
                return self._json(self.app.record(path[13:]))
            except (KeyError, ValueError):
                return self._error(HTTPStatus.NOT_FOUND, "no such record")
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


DEFAULT_WORKSPACE = Path.home() / "PremiseDB"


def prepare_workspace(db_path: Path | None, *, base: Path | None = None) -> tuple[Path, list[str]]:
    """The database the UI will use, created on first run, with the base layer attached when a file is
    given or found beside it. Returns the path and lines describing what happened."""
    notes = []
    if db_path is None:
        db_path = Path("research.sqlite") if Path("research.sqlite").is_file() else DEFAULT_WORKSPACE / "research.sqlite"
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with RecordStore(db_path, layer="fund"):
            pass
        notes.append(f"Created your research folder at {db_path.parent}")
    with RecordStore(db_path) as db:
        attached = [l for l in db.layers() if not l.writable]
        found = db_path.with_name("base.sqlite")
        if base is None and not attached and found.is_file():
            base = found
        if base is not None:
            result = db.attach_layer(base)
            notes.append(f"Attached the {result.name} layer, version {result.version}, from {Path(base).resolve()}")
        elif not attached:
            notes.append("No base layer is attached yet: run with --base FILE once, or put the file beside the database "
                         "as base.sqlite")
    return db_path, notes


def serve(db_path=None, *, harness_name="claude-code", port=8765, model=None, reasoning_effort=None, base=None,
          open_browser=True) -> int:
    """Run the answer UI until interrupted, setting the workspace up on first run; returns the exit status."""
    if harness_name not in HARNESSES:
        raise ValueError(f"unknown harness {harness_name}; choose from {', '.join(HARNESSES)}")
    if harness_name != "codex" and (model or reasoning_effort):
        raise ValueError("--model and --effort apply to the codex harness")
    harness = Codex(model=model, reasoning_effort=reasoning_effort) if harness_name == "codex" else ClaudeCode()
    if not SKILLS.is_dir():
        raise ValueError(f"the bundled skills are missing from {SKILLS}")
    db_path, notes = prepare_workspace(None if db_path is None else Path(db_path), base=None if base is None else Path(base))
    for note in notes:
        print(note)
    if written := install_skills(harness):
        print(f"Installed the {' and '.join(written)} skill(s) for {harness.name} in {harness.skills_directories()[0]}")
    if shutil.which(harness.executable) is None:  # Both real harnesses have one.
        print(f"{harness.name} is not installed ({harness.executable} not found on PATH); install it and sign in, "
              f"then ask a question.", file=sys.stderr)
    try:
        server = make_server(db_path, harness=harness, port=port)
    except OSError as error:
        if error.errno in (48, 98):  # Address in use, on macOS and Linux.
            raise ValueError(f"Port {port} is already in use, perhaps by another premisedb ui. "
                             f"Stop it, or run with --port {port + 1}.") from None
        raise
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"PremiseDB at {url} for {db_path.parent}")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
