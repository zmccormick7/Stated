"""A local HTTP server for the Features view: feature definitions, their values and the evidence behind them,
read-only."""

from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import asdict
import json
from pathlib import Path
import threading
import webbrowser
from urllib.parse import unquote, urlparse

from ..harness import SKILLS, ClaudeCode, Codex, install_skills
from ..store import RecordStore

STATIC = Path(__file__).with_name("static")
HARNESSES = {"claude-code": ClaudeCode, "codex": Codex}
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


class App:
    """The state behind the handler: the workspace it reads."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).resolve()

    def status(self) -> dict:
        """What the page's foot shows: the folder and the layers."""
        with RecordStore(self.db_path) as db:
            layers = [{"name": l.name, "version": l.version, "writable": l.writable} for l in db.layers()]
        return {"workspace": str(self.db_path.parent), "database": str(self.db_path), "layers": layers}

    def features(self) -> list:
        """The current feature definitions, each with how many subjects have a value and the latest value's date."""
        with RecordStore(self.db_path) as db:
            return [asdict(c) for c in db.feature_coverage()]

    def feature(self, feature: str) -> dict:
        """A feature definition by key or ID, with every subject's latest value and the dates still due."""
        with RecordStore(self.db_path) as db:
            return asdict(db.feature_subjects(feature))

    def feature_series(self, feature: str, subject_id: str) -> dict:
        """Every value of a feature about one subject in date order, and the dates it was confirmed."""
        with RecordStore(self.db_path) as db:
            return asdict(db.feature_series(feature, subject_id))

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
            # When a cutoff first sees each record, so a feature value's evidence can be checked against its date.
            seen = db.seen_from([record.id, *found.records, *(c["source"] for c in citations if isinstance(c["source"], str))])
            for citation in citations:
                citation["seen_from"] = seen.get(citation["source"])
            connections = []
            for link in found.links:
                other = found.records[link.from_id if link.to_id == record.id else link.to_id]
                connections.append({"relation": link.relation, "direction": "in" if link.to_id == record.id else "out",
                                    "explanation": link.explanation, "record": {**_preview(other), "seen_from": seen.get(other.id)}})
        return {"id": record.id, "kind": record.kind, "text": record.text, "layer": record.layer,
                "author_id": record.author_id, "updated_at": record.updated_at, "as_of": record.as_of,
                "seen_from": seen.get(record.id),
                "attributes": {k: v for k, v in record.attributes.items() if k != "citations"},
                "citations": citations, "connections": connections, "has_more": found.has_more}


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
        if path == "/api/features":
            return self._json(self.app.features())
        if path.startswith("/api/features/"):
            parts = [unquote(part) for part in path[14:].split("/")]
            try:
                if len(parts) == 1:
                    return self._json(self.app.feature(parts[0]))
                if len(parts) == 3 and parts[1] == "subjects":
                    return self._json(self.app.feature_series(parts[0], parts[2]))
            except KeyError:
                return self._error(HTTPStatus.NOT_FOUND, "no such feature or subject")
            except ValueError as error:
                return self._error(HTTPStatus.BAD_REQUEST, str(error))
        if path.startswith("/api/records/"):
            try:
                return self._json(self.app.record(path[13:]))
            except (KeyError, ValueError):
                return self._error(HTTPStatus.NOT_FOUND, "no such record")
        self._error(HTTPStatus.NOT_FOUND, "not found")

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


def make_server(db_path, *, host="127.0.0.1", port=0) -> ThreadingHTTPServer:
    """A server bound to localhost; port 0 picks a free one. Call serve_forever() to run it."""
    return ThreadingHTTPServer((host, port), partial(Handler, App(db_path)))


DEFAULT_WORKSPACE = Path.home() / "Stated"


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


def serve(db_path=None, *, harness_name="claude-code", port=8765, base=None, contact=None, open_browser=True) -> int:
    """Run the Features view until interrupted, setting the workspace up on first run and installing the bundled
    skills for the harness that feature runs use; returns the exit status."""
    if harness_name not in HARNESSES:
        raise ValueError(f"unknown harness {harness_name}; choose from {', '.join(HARNESSES)}")
    harness = HARNESSES[harness_name]()
    if not SKILLS.is_dir():
        raise ValueError(f"the bundled skills are missing from {SKILLS}")
    db_path, notes = prepare_workspace(None if db_path is None else Path(db_path), base=None if base is None else Path(base))
    for note in notes:
        print(note)
    if contact is not None:
        if "@" not in contact:
            raise ValueError('--contact needs a name and an email, such as "Your Name you@example.com"')
        db_path.with_name("contact.txt").write_text(contact.strip() + "\n", encoding="utf-8")
        print(f"Saved your contact in {db_path.with_name('contact.txt')}; it is sent only to SEC.gov")
    if written := install_skills(harness):
        print(f"Installed the {' and '.join(written)} skill(s) for {harness.name} in {harness.skills_directories()[0]}")
    try:
        server = make_server(db_path, port=port)
    except OSError as error:
        if error.errno in (48, 98):  # Address in use, on macOS and Linux.
            raise ValueError(f"Port {port} is already in use, perhaps by another `stated ui`. "
                             f"Stop it, or run with --port {port + 1}.") from None
        raise
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Stated at {url} for {db_path.parent}")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
