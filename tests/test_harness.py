from datetime import datetime
import json
import os
from pathlib import Path
import stat
import sys
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from stated.harness import ClaudeCode, Codex, HarnessError, UsageLimit, _failure, install_skills


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

    def test_the_final_result_is_the_reply(self):
        tool = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}}
        result = {"type": "result", "is_error": False, "result": "done", "session_id": "s-2"}
        reply = ClaudeCode(self.executable({"type": "system"}, "a warning", tool, result)).run(
            "Write the value.", cwd=self.directory, env=dict(os.environ))
        self.assertEqual(reply, "done")
        self.assertEqual(json.loads((self.directory / "argv.json").read_text()),
                         ["-p", "Write the value.", "--output-format", "stream-json", "--verbose",
                          "--allowedTools", "Bash(stated:*)", "Read", "Write"])

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
                                   ("You've hit your session limit", None), ("Weekly limit reached", None)):
            with self.subTest(message=message), self.assertRaises(UsageLimit) as raised:
                ClaudeCode(self.executable({"type": "result", "is_error": True, "result": message})).run("Why?", cwd=self.directory)
            self.assertEqual(raised.exception.resets_at, resets_at)
        # The form seen live, with a clock time and zone: the next such time, in that zone.
        denver = ZoneInfo("America/Denver")
        at = datetime(2026, 10, 2, 14, 20, tzinfo=denver)
        seen = _failure("You've hit your session limit · resets 3:10pm (America/Denver)", at)
        self.assertEqual(datetime.fromtimestamp(seen.resets_at, denver), datetime(2026, 10, 2, 15, 10, tzinfo=denver))
        later = _failure("5-hour limit reached ∙ resets 1pm (America/Denver)", at)  # Already past today: tomorrow.
        self.assertEqual(datetime.fromtimestamp(later.resets_at, denver), datetime(2026, 10, 3, 13, 0, tzinfo=denver))
        self.assertIsInstance(_failure("limit reached, resets 3pm (Mars/Olympus)", at).resets_at, float)


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

    def test_the_last_message_is_the_reply(self):
        events = [{"type": "thread.started", "thread_id": "t-1"}, {"type": "turn.started"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
                  {"type": "turn.completed", "usage": {}}]
        harness = Codex(self.executable(*events, last="done"), model="gpt-6.1-sol", reasoning_effort="high")
        reply = harness.run("Write the value.", cwd=self.directory, env=dict(os.environ), writable=(self.directory,))
        self.assertEqual(reply, "done")
        argv = json.loads((self.directory / "argv.json").read_text())
        self.assertEqual(argv[:9], ["exec", "--json", "--skip-git-repo-check", "-s", "workspace-write", "-m",
                                    "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"'])
        self.assertEqual((argv[9:11], argv[11], argv[13:]), (["--add-dir", str(self.directory)], "-o", ["Write the value."]))

    def test_failures_raise_harness_errors(self):
        for build, message in ((lambda: self.executable({"type": "thread.started", "thread_id": "t"},
                                                         {"type": "turn.failed", "message": "quota exhausted"}, status=1),
                                "status 1: quota exhausted"),
                               (lambda: str(self.directory / "absent"), "Could not start")):
            with self.subTest(message=message), self.assertRaisesRegex(HarnessError, message):
                Codex(build()).run("Why?", cwd=self.directory)
        self.assertEqual(Codex.skills_directories(), (Path.home() / ".agents" / "skills",))


class SkillInstallTests(unittest.TestCase):
    class Harness:
        def __init__(self, directory):
            self.directory = directory

        def skills_directories(self):
            return (self.directory,)

    def test_skills_are_installed_and_updated(self):
        with TemporaryDirectory() as directory:
            source, target = Path(directory) / "skills", Path(directory) / "harness-skills"
            (source / "stated-state").mkdir(parents=True)
            (source / "stated-state" / "SKILL.md").write_text("v1")
            harness = self.Harness(target)
            self.assertEqual(install_skills(harness, source), ["stated-state"])
            self.assertEqual((target / "stated-state" / "SKILL.md").read_text(), "v1")
            self.assertEqual(install_skills(harness, source), [])  # Up to date: nothing written.
            (source / "stated-state" / "SKILL.md").write_text("v2")
            self.assertEqual(install_skills(harness, source), ["stated-state"])
            self.assertEqual((target / "stated-state" / "SKILL.md").read_text(), "v2")

    def test_retired_skills_are_removed(self):
        with TemporaryDirectory() as directory:
            source, target = Path(directory) / "skills", Path(directory) / "harness-skills"
            (source / "stated-state").mkdir(parents=True)
            (source / "stated-state" / "SKILL.md").write_text("v1")
            for name in ("premisedb-answer", "stated-answer", "someone-elses-skill"):
                (target / name).mkdir(parents=True)
                (target / name / "SKILL.md").write_text("old")
            install_skills(self.Harness(target), source)
            self.assertEqual(sorted(p.name for p in target.iterdir()), ["someone-elses-skill", "stated-state"])


if __name__ == "__main__":
    unittest.main()
