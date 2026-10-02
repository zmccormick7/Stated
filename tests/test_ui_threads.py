import json
import os
from pathlib import Path
import stat
import sys
import threading
import time
from tempfile import TemporaryDirectory
import unittest

from premisedb import RecordStore
from premisedb.ui import ClaudeCode, Codex, HarnessError, Reply, ThreadStore, UsageLimit, run_turn
from premisedb.ui import runs, threads
from premisedb.ui.threads import time_limit


class FakeHarness:
    name = "fake"

    def __init__(self, *replies, write=None):
        self.replies, self.calls, self.write = list(replies), [], write

    def run(self, prompt, *, cwd, env, session_id, web, timeout, on_progress):
        self.calls.append({"prompt": prompt, "cwd": cwd, "db": env["PREMISEDB_DB"], "session_id": session_id,
                           "web": web, "timeout": timeout, "fetch": env["PREMISEDB_FETCH"]})
        on_progress("Bash: Recall what the graph holds")
        if self.write:
            self.write()
        reply = self.replies.pop(0) if self.replies else "DONE"
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

    def ask(self, harness, question="How concentrated are the customers?", thread=None, effort=5):
        thread = thread or self.store.create_thread(question, harness=harness.name)
        run_turn(self.store, self.store.start_turn(thread, question, effort), db_path=self.db_path, harness=harness)
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
        self.assertEqual(turn["answer"]["sources"], [{"record": self.finding, "kind": "Finding",
                                                      "text": "Two customers were 22% and 14% of revenue."}])
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

    def test_effort_sets_what_the_agent_is_told_the_web_and_the_time_limit(self):
        bad = json.dumps({"title": "Yes.", "body": "See [2]."})
        for effort, told, web in ((1, "about one minute. Answer from what the graph already holds", False),
                                  (5, "about five minutes.", False), (20, "outside sources on the web", True)):
            with self.subTest(effort=effort):
                harness = FakeHarness(bad, self.good)
                thread = self.ask(harness, effort=effort)
                first, repair = harness.calls[:2]
                self.assertEqual(len(harness.calls), 3 if effort == 20 else 2)  # Only 20 minutes asks for more.
                self.assertIn(told, first["prompt"])
                self.assertEqual((first["web"], repair["web"]), (web, web))
                self.assertEqual(first["fetch"], "1" if web else "0")  # ingest --url follows the web.
                # One clock for the whole turn: the repair gets what is left of it.
                self.assertTrue(time_limit(effort) - 5 < repair["timeout"] <= first["timeout"] <= time_limit(effort))
                self.ask(harness := FakeHarness(self.good), "And before?", thread=thread["id"], effort=effort)
                self.assertIn(told, harness.calls[0]["prompt"])  # A follow-up carries its own effort.
        self.assertEqual([time_limit(effort) for effort in (1, 5, 20)], [360, 600, 2400])
        self.ask(harness := FakeHarness(self.good))
        self.assertIn("about five minutes", harness.calls[0]["prompt"])  # The default.

    def test_an_agent_that_finishes_early_is_asked_what_would_improve_the_answer(self):
        better = json.dumps(json.loads(self.good) | {"body": "One was 22% and the other 14%. [1]"})
        bad = json.dumps({"title": "Yes.", "body": "See [2]."})
        harness = FakeHarness(self.good, better, "DONE")
        turn = self.ask(harness, effort=20)["turns"][0]
        self.assertEqual((turn["status"], turn["answer"]["body"]), ("done", "One was 22% and the other 14%. [1]"))
        self.assertEqual(turn["progress"].count("Looking for what would improve the answer"), 2)
        self.assertEqual([call["session_id"] for call in harness.calls], [None, "session-1", "session-1"])
        self.assertIn("You have used under a minute of your 20 minutes", harness.calls[1]["prompt"])
        self.assertIn("reply with the single word DONE", harness.calls[1]["prompt"])
        # The answer in hand stands when the agent repeats it, breaks it beyond repair, or the harness fails.
        for replies, calls in (((self.good, self.good), 2), ((self.good, bad, bad), 3),
                               ((self.good, HarnessError("usage limit reached")), 2)):
            with self.subTest(replies=replies):
                harness = FakeHarness(*replies)
                turn = self.ask(harness, effort=20)["turns"][0]
                self.assertEqual((turn["status"], turn["error"], turn["answer"]["body"], len(harness.calls)),
                                 ("done", None, "One was 22%. [1]", calls))
        original, threads.USE_OF_TIME = threads.USE_OF_TIME, 0  # Time mostly used: nothing more is asked.
        self.addCleanup(setattr, threads, "USE_OF_TIME", original)
        self.ask(harness := FakeHarness(self.good), effort=20)
        self.assertEqual(len(harness.calls), 1)

    def test_an_hour_is_a_run_of_planned_work_and_a_report(self):
        plan = json.dumps({"items": ["Who are the two customers?", " Read the\n10-K note ", "", 7]})
        worked = json.dumps({"summary": "They are unnamed.", "new_items": ["Check the 10-Q", "Ask the 8-K", "A third"]})
        harness = FakeHarness(plan, worked, HarnessError("the page would not load"), "Plain words, no JSON.", "Nothing new.",
                              json.dumps({"items": []}), self.good)
        thread = self.ask(harness, "Go deep on the customers.", effort=60)
        turn = thread["turns"][0]
        self.assertEqual((turn["status"], turn["error"], turn["effort"], turn["answer"]["title"]),
                         ("done", None, 60, "Two customers were 36% of revenue."))
        self.assertEqual([(item["text"], item["status"], item["result"]) for item in turn["items"]], [
            ("Who are the two customers?", "done", "They are unnamed."),
            ("Read the 10-K note", "failed", "the page would not load"),
            ("Check the 10-Q", "done", "Plain words, no JSON."),  # A worker may add two items, no more.
            ("Ask the 8-K", "done", "Nothing new.")])
        plan_call, first, *_, replan, report = harness.calls
        self.assertEqual(len(harness.calls), 7)
        self.assertIn("Plan research for the brief below", plan_call["prompt"])
        self.assertIn("3 to 6 items.\n\nBrief: Go deep on the customers.", plan_call["prompt"])
        self.assertIn("Your item: Who are the two customers?", first["prompt"])
        self.assertIn("- [todo] Read the 10-K note", first["prompt"])  # A worker sees the other items.
        self.assertEqual((first["session_id"], first["web"], first["fetch"], first["timeout"]), (None, True, "1", 1800))
        self.assertIn("- [done] Who are the two customers? — They are unnamed.", replan["prompt"])
        self.assertIn("- [failed] Read the 10-K note", replan["prompt"])
        self.assertIn("Work done:\n- [done] Who are the two customers? — They are unnamed.", report["prompt"])
        self.assertNotIn("Read the 10-K note", report["prompt"])
        self.assertEqual(report["session_id"], None)  # The report starts fresh, from the graph.
        self.assertEqual(turn["progress"][::2][:2], ["Planning the research", "Working on: Who are the two customers?"])
        self.assertIn("Writing the report", turn["progress"])
        # A follow-up plans in the session that wrote the report.
        harness = FakeHarness(json.dumps({"items": ["More"]}), worked, HarnessError("usage limit"), HarnessError("usage limit"), self.good)
        turn = self.ask(harness, "And suppliers?", thread=thread["id"], effort=60)["turns"][1]
        self.assertEqual((harness.calls[0]["session_id"], turn["status"], len(harness.calls)), ("session-1", "done", 5))
        self.assertEqual([item["status"] for item in turn["items"]], ["done", "failed", "failed"])  # Two failures end the work.

    def test_a_run_with_three_agents_works_three_pieces_at_a_time(self):
        good, prompts, together = self.good, [], threading.Barrier(3, timeout=5)

        class Crew:
            name = "fake"

            def run(self, prompt, *, cwd, env, session_id, web, timeout, on_progress):
                prompts.append(prompt)
                if prompt.startswith("Plan research"):
                    return Reply(json.dumps({"items": ["A", "B", "C", "D"]}), "s", ())
                if prompt.startswith("You are one worker"):
                    item = prompt.split("Your item: ")[1][0]
                    if item in "ABC":
                        together.wait()  # Passes only when three workers are in flight at once.
                    return Reply(json.dumps({"summary": f"Did {item}."}), "s", ())
                return Reply('{"items": []}' if "has done the work listed" in prompt else good, "s", ())

        thread = self.store.create_thread("Go deep.", harness="fake")
        run_turn(self.store, self.store.start_turn(thread, "Go deep.", 60, 3), db_path=self.db_path, harness=Crew())
        turn = self.store.thread(thread)["turns"][0]
        self.assertEqual((turn["status"], turn["agents"]), ("done", 3))
        self.assertEqual([(item["status"], item["result"]) for item in turn["items"]],
                         [("done", f"Did {item}.") for item in "ABCD"])
        self.assertIn("3 at a time, so items must not overlap", prompts[0])
        self.assertIn("3 to 6 items.", prompts[0])

    def test_a_run_waits_out_a_usage_limit(self):
        slept = []
        self.addCleanup(setattr, runs, "_sleep", runs._sleep)
        runs._sleep = slept.append
        did = json.dumps({"summary": "Done."})
        harness = FakeHarness(UsageLimit("usage limit reached"), json.dumps({"items": ["A", "B"]}),
                              UsageLimit("usage limit reached", time.time() + 120), did, did, json.dumps({"items": []}),
                              UsageLimit("usage limit reached"), self.good)
        turn = self.ask(harness, "Go deep.", effort=180)["turns"][0]
        self.assertEqual((turn["status"], [item["status"] for item in turn["items"]], len(harness.calls)),
                         ("done", ["done", "done"], 8))
        self.assertEqual((slept[0], slept[2]), (900, 900))  # No reset time given: try again in fifteen minutes.
        self.assertTrue(140 < slept[1] <= 150)  # A reset time given: wait until just after it.
        waits = [line for line in turn["progress"] if line.startswith("Waiting for your usage limit to reset")]
        self.assertEqual(len(waits), 3)
        self.assertRegex(waits[1], r"\(about \d\d:\d\d\)$")
        self.assertIn("Your item: A", harness.calls[3]["prompt"])  # The piece the limit stopped is tried again first.
        self.assertIn("6 to 12 items.", harness.calls[1]["prompt"])
        # A limit that lifts after the working time ends the work; with nothing done, the run fails with it.
        harness = FakeHarness(json.dumps({"items": ["A"]}), UsageLimit("usage limit reached", time.time() + 4 * 3600))
        turn = self.ask(harness, "Go deep.", effort=180)["turns"][0]
        self.assertEqual((turn["status"], turn["error"], turn["items"][0]["status"], len(slept)),
                         ("failed", "usage limit reached", "todo", 3))

    def test_a_run_with_no_plan_or_no_finished_work_fails_plainly(self):
        for replies, error in (((json.dumps({"items": []}),), "The agent did not return a plan for this research."),
                               ((HarnessError("OAuth session expired"),), "OAuth session expired"),
                               ((json.dumps({"items": ["A", "B", "C"]}), HarnessError("usage limit"), HarnessError("usage limit")),
                                "usage limit")):
            with self.subTest(error=error):
                turn = self.ask(harness := FakeHarness(*replies), effort=60)["turns"][0]
                self.assertEqual((turn["status"], turn["error"], len(harness.calls)), ("failed", error, len(replies)))
        original, runs.REPORT_MINUTES = runs.REPORT_MINUTES, 60  # No working time left once the plan is in.
        self.addCleanup(setattr, runs, "REPORT_MINUTES", original)
        turn = self.ask(harness := FakeHarness(json.dumps({"items": ["A"]})), effort=60)["turns"][0]
        self.assertEqual((turn["error"], [item["status"] for item in turn["items"]], len(harness.calls)),
                         ("The time ran out before any of the work was done.", ["todo"], 1))

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
                         ["-p", "Why?", "--output-format", "stream-json", "--verbose",
                          "--allowedTools", "Bash(premisedb:*)", "Read", "Write", "--resume", "s-1"])
        self.assertEqual(progress[0], "Bash: Recall x")
        self.assertEqual((len(progress[1]), progress[1][-1]), (160, "…"))
        ClaudeCode(self.executable(result)).run("Why?", cwd=self.directory, web=True)
        self.assertEqual(json.loads((self.directory / "argv.json").read_text())[5:],
                         ["--allowedTools", "Bash(premisedb:*)", "Read", "Write", "WebSearch", "WebFetch"])

    def test_an_agent_past_its_time_is_stopped(self):
        path = self.directory / "claude"
        path.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        with self.assertRaisesRegex(HarnessError, "past the time it was given"):
            ClaudeCode(str(path)).run("Why?", cwd=self.directory, timeout=0.3)

    def test_failures_raise_harness_errors(self):
        failed = {"type": "result", "is_error": True, "result": "OAuth session expired"}
        for build, message in ((lambda: self.executable(failed), "OAuth session expired"),
                               (lambda: self.executable("boom", status=3), "exited with status 3: boom"),
                               (lambda: str(self.directory / "absent"), "Could not start")):
            with self.subTest(message=message), self.assertRaisesRegex(HarnessError, message) as raised:
                ClaudeCode(build()).run("Why?", cwd=self.directory)
            self.assertNotIsInstance(raised.exception, UsageLimit)
        for message, resets_at in (("Claude AI usage limit reached|1790000000", 1790000000.0),
                                   ("5-hour limit reached ∙ resets 3pm", None), ("You've hit your session limit", None)):
            with self.subTest(message=message), self.assertRaises(UsageLimit) as raised:
                ClaudeCode(self.executable({"type": "result", "is_error": True, "result": message})).run("Why?", cwd=self.directory)
            self.assertEqual(raised.exception.resets_at, resets_at)



class CodexTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def executable(self, *events, last=None, status=0):
        """A stand-in for codex: records its arguments, prints the events, writes the -o file."""
        path = self.directory / "codex"
        lines = [json.dumps(event) for event in events]
        path.write_text(f"#!{sys.executable}\nimport json, sys\nargv = sys.argv[1:]\n"
                        f"open('argv.json', 'w').write(json.dumps(argv))\n"
                        f"print({chr(10).join(lines)!r})\n"
                        f"if {last!r} is not None: open(argv[argv.index('-o') + 1], 'w').write({last!r})\n"
                        f"sys.exit({status})\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def test_events_become_progress_and_the_last_message_is_the_reply(self):
        events = [{"type": "thread.started", "thread_id": "t-1"}, {"type": "turn.started"},
                  {"type": "item.started", "item": {"type": "command_execution", "command": "/bin/zsh -lc 'premisedb recall x'"}},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}},
                  {"type": "turn.completed", "usage": {}}]
        progress = []
        harness = Codex(self.executable(*events, last='{"title": "x"}'), model="gpt-6.1-sol", reasoning_effort="high")
        reply = harness.run("Why?", cwd=self.directory, env=dict(os.environ), on_progress=progress.append)
        self.assertEqual((reply, progress), (Reply('{"title": "x"}', "t-1", ()), ["Shell: premisedb recall x"]))
        argv = json.loads((self.directory / "argv.json").read_text())
        self.assertEqual(argv[:10], ["exec", "--json", "--skip-git-repo-check", "-s", "workspace-write", "-m",
                                     "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"', "-o"])
        self.assertEqual(argv[11:], ["Why?"])
        Codex(self.executable(*events, last="ok")).run("More?", cwd=self.directory, session_id="t-1")
        argv = json.loads((self.directory / "argv.json").read_text())
        self.assertEqual(argv[-3:], ["resume", "t-1", "More?"])
        searched = {"type": "item.completed", "item": {"type": "web_search", "query": "PowerFrame 10-K"}}
        Codex(self.executable(searched, *events, last="ok")).run("Why?", cwd=self.directory, web=True,
                                                                 on_progress=progress.append)
        argv = json.loads((self.directory / "argv.json").read_text())
        self.assertEqual(argv[5:9], ["-c", 'web_search="live"', "-c", "sandbox_workspace_write.network_access=true"])
        self.assertEqual(progress[1], "Web: PowerFrame 10-K")

    def test_failures_raise_harness_errors(self):
        for build, message in ((lambda: self.executable({"type": "thread.started", "thread_id": "t"},
                                                         {"type": "turn.failed", "message": "quota exhausted"}, status=1),
                                "status 1: quota exhausted"),
                               (lambda: str(self.directory / "absent"), "Could not start")):
            with self.subTest(message=message), self.assertRaisesRegex(HarnessError, message):
                Codex(build()).run("Why?", cwd=self.directory)
        self.assertEqual(Codex.skills_directories(), (Path.home() / ".agents" / "skills",))


if __name__ == "__main__":
    unittest.main()
