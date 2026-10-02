import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore
from premisedb.ui import ClaudeCode, HarnessError, Reply, ThreadStore, run_turn


class FakeHarness:
    name = "fake"

    def __init__(self, *replies, write=None):
        self.replies, self.calls, self.write = list(replies), [], write

    def run(self, prompt, *, cwd, env, session_id, on_progress):
        self.calls.append({"prompt": prompt, "cwd": cwd, "db": env["PREMISEDB_DB"], "session_id": session_id})
        on_progress("Bash: Recall what the graph holds")
        if self.write:
            self.write()
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Reply(reply, "session-1", ("WebFetch",))


class ThreadTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name).resolve()
        self.db_path = self.directory / "research.sqlite"
        with RecordStore(self.db_path) as db:
            self.finding = db.create("Finding", text="Two customers were 22% and 14% of revenue.", author_id="a").id
        self.store = ThreadStore(self.directory / "threads.sqlite")
        self.good = json.dumps({"title": "Two customers were 36% of revenue.", "body": "One was 22%. [1]",
                                "sources": [{"record": self.finding[:8]}]})

    def ask(self, harness, question="How concentrated are the customers?", thread=None):
        thread = thread or self.store.create_thread(question, harness=harness.name)
        run_turn(self.store, self.store.start_turn(thread, question), db_path=self.db_path, harness=harness)
        return self.store.thread(thread)

    def test_a_question_becomes_a_checked_answer_in_a_thread(self):
        def write():
            with RecordStore(self.db_path) as db:
                db.create("Question", text="Who are the two customers?", author_id="fake")

        harness = FakeHarness(f"Here it is:\n```json\n{self.good}\n```", self.good, write=write)
        thread = self.ask(harness, "How concentrated\nare the customers?")
        self.assertEqual((thread["title"], thread["harness"], thread["session_id"]),
                         ("How concentrated are the customers?", "fake", "session-1"))
        turn = thread["turns"][0]
        self.assertEqual((turn["status"], turn["error"], turn["progress"], turn["denied"]),
                         ("done", None, ["Bash: Recall what the graph holds"], ["WebFetch"]))
        self.assertEqual(turn["answer"]["sources"], [{"record": self.finding, "kind": "Finding"}])
        self.assertEqual(turn["written"], {"Question": 1})  # What the agent added during the turn.
        call = harness.calls[0]
        self.assertEqual((call["cwd"], call["db"], call["session_id"]), (self.directory, str(self.db_path), None))
        self.assertIn("the premisedb-answer skill", call["prompt"])  # The first prompt names the installed skills.
        self.assertTrue(call["prompt"].endswith("Question: How concentrated\nare the customers?"))

        thread = self.ask(harness, "And a year earlier?", thread=thread["id"])
        self.assertEqual([turn["status"] for turn in thread["turns"]], ["done", "done"])
        self.assertEqual(harness.calls[1]["session_id"], "session-1")  # Follow-ups resume the session.
        self.assertTrue(harness.calls[1]["prompt"].startswith("Answer this follow-up"))
        self.assertEqual([t["id"] for t in self.store.threads()], [thread["id"]])

    def test_a_failed_check_gets_one_repair_attempt(self):
        bad = json.dumps({"title": "Yes.", "body": "See [2]."})
        harness = FakeHarness(bad, self.good)
        turn = self.ask(harness)["turns"][0]
        self.assertEqual((turn["status"], turn["progress"][1]), ("done", "Fixing the answer"))
        self.assertIn("- body cites [2], which is not among the sources", harness.calls[1]["prompt"])
        self.assertEqual(harness.calls[1]["session_id"], "session-1")

        turn = self.ask(FakeHarness(bad, "I could not fix it."))["turns"][0]
        self.assertEqual((turn["status"], turn["answer"]), ("failed", None))
        self.assertIn("did not pass the check: the reply was not a JSON answer document", turn["error"])
        turn = self.ask(FakeHarness(HarnessError("OAuth session expired")))["turns"][0]
        self.assertEqual((turn["status"], turn["error"]), ("failed", "OAuth session expired"))

    def test_store_boundaries(self):
        with self.assertRaises(KeyError):
            self.store.thread("missing")
        with self.assertRaises(KeyError):
            self.store.start_turn("missing", "Why?")
        for question in ("", "  ", None):
            with self.subTest(question=question), self.assertRaisesRegex(ValueError, "nonblank"):
                self.store.create_thread(question, harness="fake")
        self.assertEqual(len(self.store.threads()), 0)
        with self.assertRaisesRegex(ValueError, "Incompatible thread store"):
            ThreadStore(self.db_path)  # A research database is not a thread store.


class ClaudeCodeTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def executable(self, *events, status=0):
        """A stand-in for the claude executable: records its arguments, then prints the given lines."""
        path = self.directory / "claude"
        lines = [json.dumps(event) if isinstance(event, dict) else event for event in events]
        path.write_text(f"#!{sys.executable}\nimport json, sys\nopen('argv.json', 'w').write(json.dumps(sys.argv[1:]))\n"
                        f"print({chr(10).join(lines)!r})\nsys.exit({status})\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def test_stream_events_become_progress_and_a_reply(self):
        tool = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "premisedb recall x", "description": "Recall  x"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/" + "a" * 200}}]}}
        result = {"type": "result", "is_error": False, "result": "{}", "session_id": "s-2",
                  "permission_denials": [{"tool_name": "WebFetch"}, {"tool_name": "WebFetch"}]}
        progress = []
        reply = ClaudeCode(self.executable({"type": "system", "session_id": "s-2"}, "a warning", tool, result)).run(
            "Why?", cwd=self.directory, env=dict(os.environ), session_id="s-1", on_progress=progress.append)
        self.assertEqual(reply, Reply("{}", "s-2", ("WebFetch",)))
        self.assertEqual(json.loads((self.directory / "argv.json").read_text()),
                         ["-p", "Why?", "--output-format", "stream-json", "--verbose", "--resume", "s-1"])
        self.assertEqual(progress[0], "Bash: Recall x")
        self.assertEqual((len(progress[1]), progress[1][-1]), (160, "…"))

    def test_failures_raise_harness_errors(self):
        failed = {"type": "result", "is_error": True, "result": "OAuth session expired"}
        for build, message in ((lambda: self.executable(failed), "OAuth session expired"),
                               (lambda: self.executable("boom", status=3), "exited with status 3: boom"),
                               (lambda: str(self.directory / "absent"), "Could not start")):
            with self.subTest(message=message), self.assertRaisesRegex(HarnessError, message):
                ClaudeCode(build()).run("Why?", cwd=self.directory)


if __name__ == "__main__":
    unittest.main()
