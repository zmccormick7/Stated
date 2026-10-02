from http.client import HTTPConnection
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from premisedb import RecordStore
from premisedb.ui import Reply
from premisedb.ui.server import install_skills, make_server, prepare_workspace


class SlowHarness:
    """Answers after a short pause, so a turn can be seen running."""

    name = "fake"

    def __init__(self, answer):
        self.answer, self.release = answer, threading.Event()

    def run(self, prompt, *, cwd, env, session_id, web, timeout, on_progress):
        self.web = web
        on_progress("Bash: premisedb recall")
        self.release.wait(5)
        return Reply(self.answer, "session-1", ())

    skills_directory = None

    def skills_directories(self):
        return (self.skills_directory,) if self.skills_directory else ()


class ServerTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db_path = Path(directory.name) / "research.sqlite"
        with RecordStore(db_path) as db:
            finding = db.create("Finding", text="Two customers were 22% and 14% of revenue.", author_id="a").id
            self.finding = finding
            self.entity = db.create("Entity", text="PowerFrame Inc. (PowerFrame)", author_id="a").id
            db.create_link(finding, self.entity, "about", author_id="a")
            source = db.ingest_source(description="PowerFrame 10-K", document_text="Sales to one customer were 22%.",
                                      author_id="a").id
            db.patch(finding, author_id="a", citations=[{"source_id": source, "quote": "one customer were 22%"}])
            self.model = db.create("Model", text="Revenue model", author_id="a").id
            for attributes in ({"key": "growth", "role": "input", "value": 0.4},
                               {"key": "revenue", "role": "output", "value": 541.3, "formula": "386.6 * (1 + growth)"}):
                db.create_link(db.create("Variable", text=attributes["key"], author_id="a", attributes=attributes).id,
                               self.model, "in_model", author_id="a")
        self.harness = SlowHarness(json.dumps({"title": "Two customers were 36% of revenue.", "body": "One was 22%. [1]",
                                               "sources": [{"record": finding}]}))
        self.server = make_server(db_path, harness=self.harness)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def call(self, method, path, body=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, None if body is None else json.dumps(body), {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, (json.loads(data) if response.getheader("Content-Type") == "application/json" else data)

    def wait_done(self, thread_id):
        for _ in range(100):
            status, thread = self.call("GET", f"/api/threads/{thread_id}")
            if all(turn["status"] != "running" for turn in thread["turns"]):
                return thread
            time.sleep(0.05)
        self.fail("the turn did not finish")

    def test_ask_runs_in_the_background_and_the_page_is_served(self):
        self.assertEqual(self.call("GET", "/api/threads"), (200, []))
        status, started = self.call("POST", "/api/threads", {"question": "How concentrated are the customers?"})
        self.assertEqual(status, 201)
        status, thread = self.call("GET", f"/api/threads/{started['thread_id']}")
        self.assertEqual((status, thread["title"], thread["turns"][0]["status"]),
                         (200, "How concentrated are the customers?", "running"))
        self.assertEqual(self.call("POST", f"/api/threads/{started['thread_id']}/turns", {"question": "More?"}),
                         (400, {"error": "this thread is still answering the previous question"}))
        self.harness.release.set()
        turn = self.wait_done(started["thread_id"])["turns"][0]
        self.assertEqual((turn["status"], turn["answer"]["title"]), ("done", "Two customers were 36% of revenue."))
        status, started = self.call("POST", f"/api/threads/{started['thread_id']}/turns", {"question": "And before?"})
        self.assertEqual(status, 201)
        self.assertEqual(len(self.wait_done(started["thread_id"])["turns"]), 2)
        self.assertFalse(self.harness.web)  # Five minutes unless the user says otherwise: no web.
        status, started = self.call("POST", "/api/threads", {"question": "Who else sells to them?", "effort": 20})
        self.wait_done(started["thread_id"])
        self.assertTrue(self.harness.web)
        for effort in (7, "5", True):
            self.assertEqual(self.call("POST", "/api/threads", {"question": "Why?", "effort": effort}),
                             (400, {"error": "effort must be one of 1, 5, 20 minutes"}))
        self.assertEqual(len(self.call("GET", "/api/threads")[1]), 2)
        for method, path, body, status in (("GET", "/api/threads/missing", None, 404),
                                           ("POST", "/api/threads/missing/turns", {"question": "x"}, 404),
                                           ("POST", "/api/threads", {"question": ""}, 400),
                                           ("POST", "/api/threads", ["x"], 400),
                                           ("GET", "/static/../schema.sql", None, 404),
                                           ("GET", "/nowhere", None, 404)):
            with self.subTest(path=path):
                self.assertEqual(self.call(method, path, body)[0], status)
        status, page = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/static/app.js", page)
        self.assertEqual(self.call("GET", "/static/app.js")[0], 200)

    def test_a_lever_evaluates_the_model_through_the_api(self):
        status, result = self.call("POST", f"/api/models/{self.model[:8]}/evaluate", {"overrides": {"growth": 0.2}})
        self.assertEqual((status, result["changed"]), (200, ["growth", "revenue"]))
        self.assertAlmostEqual(result["values"]["revenue"], 463.92)
        self.assertEqual(self.call("POST", f"/api/models/{self.model}/evaluate", {"overrides": {"tariff": 1}})[0], 400)
        self.assertEqual(self.call("POST", "/api/models/missing/evaluate", {"overrides": {}})[0], 404)

    def test_a_record_is_served_with_its_citations_and_connections(self):
        status, record = self.call("GET", f"/api/records/{self.finding[:8]}")
        self.assertEqual((status, record["id"], record["kind"], record["layer"]), (200, self.finding, "Finding", "workspace"))
        self.assertEqual(record["citations"], [{"quote": "one customer were 22%", "source": record["citations"][0]["source"],
                                                "title": "PowerFrame 10-K"}])
        self.assertEqual([(c["relation"], c["direction"], c["record"]["kind"]) for c in record["connections"]],
                         [("about", "out", "Entity"), ("supported_by", "out", "Source")])
        self.assertEqual(record["connections"][0]["record"], {"id": self.entity, "kind": "Entity",
                                                              "text": "PowerFrame Inc. (PowerFrame)"})
        self.assertNotIn("citations", record["attributes"])
        status, entity = self.call("GET", f"/api/records/{self.entity}")
        self.assertEqual((entity["connections"][0]["direction"], entity["connections"][0]["record"]["id"]), ("in", self.finding))
        self.assertEqual(self.call("GET", "/api/records/missing")[0], 404)

    def test_status_describes_the_workspace(self):
        status, result = self.call("GET", "/api/status")
        self.assertEqual((status, result["harness"], result["harness_found"]), (200, "fake", False))
        self.assertEqual([(layer["name"], layer["writable"]) for layer in result["layers"]], [("workspace", True)])
        self.assertTrue(result["database"].endswith("research.sqlite"))

    def test_first_run_creates_the_database_and_attaches_a_base(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            with RecordStore(folder / "publisher.sqlite", layer="base") as db:
                db.create("Entity", text="PowerFrame", author_id="a")
                db.export_layer(folder / "base.sqlite", version="2026.10.01")
            db_path, notes = prepare_workspace(folder / "fund" / "research.sqlite")
            self.assertTrue(db_path.is_file())
            self.assertEqual(len(notes), 2)
            self.assertIn("Created your research folder", notes[0])
            self.assertIn("No base layer is attached yet", notes[1])
            db_path, notes = prepare_workspace(db_path, base=folder / "base.sqlite")
            self.assertIn("Attached the base layer, version 2026.10.01", notes[0])
            with RecordStore(db_path) as db:
                self.assertEqual([(l.name, l.version, l.writable) for l in db.layers()],
                                 [("fund", "", True), ("base", "2026.10.01", False)])
            # A base.sqlite beside a new database is attached without being named.
            (folder / "other").mkdir()
            shutil.copyfile(folder / "base.sqlite", folder / "other" / "base.sqlite")
            db_path, notes = prepare_workspace(folder / "other" / "research.sqlite")
            self.assertIn("Attached the base layer", notes[1])
            self.assertEqual(prepare_workspace(db_path)[1], [])  # Nothing to do the second time.

    def test_skills_are_installed_and_updated(self):
        with TemporaryDirectory() as directory:
            source, target = Path(directory) / "skills", Path(directory) / "harness-skills"
            (source / "premisedb-answer").mkdir(parents=True)
            (source / "premisedb-answer" / "SKILL.md").write_text("v1")
            self.harness.skills_directory = target
            self.assertEqual(install_skills(self.harness, source), ["premisedb-answer"])
            self.assertEqual((target / "premisedb-answer" / "SKILL.md").read_text(), "v1")
            self.assertEqual(install_skills(self.harness, source), [])  # Up to date: nothing written.
            (source / "premisedb-answer" / "SKILL.md").write_text("v2")
            self.assertEqual(install_skills(self.harness, source), ["premisedb-answer"])
            self.assertEqual((target / "premisedb-answer" / "SKILL.md").read_text(), "v2")


if __name__ == "__main__":
    unittest.main()
