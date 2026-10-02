from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from premisedb import RecordStore
from premisedb.ui import Reply
from premisedb.ui.server import install_skills, make_server


class SlowHarness:
    """Answers after a short pause, so a turn can be seen running."""

    name = "fake"

    def __init__(self, answer):
        self.answer, self.release = answer, threading.Event()

    def run(self, prompt, *, cwd, env, session_id, on_progress):
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
        self.assertEqual(len(self.call("GET", "/api/threads")[1]), 1)
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
