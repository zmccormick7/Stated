"""Run a prompt through the user's own coding agent, headless, and install the bundled skills it follows."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
from zoneinfo import ZoneInfo


class HarnessError(RuntimeError):
    """The harness could not be started or ended without an answer."""


class UsageLimit(HarnessError):
    """The user's subscription limit was reached. `resets_at` is the Unix time it lifts, when the harness says."""

    def __init__(self, message: str, resets_at: float | None = None):
        super().__init__(message)
        self.resets_at = resets_at


_LIMIT = re.compile(r"(usage|session|weekly|hourly|5-hour|rate)[ -]limit|limit reached|hit your [\w -]*limit|quota", re.I)


_RESETS = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d\d))?\s*([ap]m)(?:\s+\(([\w/+-]+)\))?", re.I)


def _failure(message: str, now: datetime | None = None) -> HarnessError:
    """The error for a harness's own account of what went wrong, told apart when it is a usage limit."""
    if not _LIMIT.search(message):
        return HarnessError(message)
    if epoch := re.search(r"\|(\d{10})\b", message):  # One Claude Code form: the reset as a Unix time.
        return UsageLimit(message, float(epoch.group(1)))
    if clock := _RESETS.search(message):  # Another: "resets 3:10pm (America/Denver)".
        hour, minute, half, zone = clock.groups()
        try:
            tz = ZoneInfo(zone) if zone else None
        except (KeyError, ValueError):
            tz = None
        now = now or datetime.now(tz).astimezone(tz)
        hour = int(hour) % 12 + (12 if half.lower() == "pm" else 0)
        resets = now.replace(hour=hour, minute=int(minute or 0), second=0, microsecond=0)
        if resets < now:
            resets += timedelta(days=1)
        return UsageLimit(message, resets.timestamp())
    return UsageLimit(message, None)


ALLOWED_TOOLS = ("Bash(stated:*)", "Read", "Write")

SKILLS = Path(__file__).resolve().parent / "skills"
# Skills no longer bundled, under their current names and the names from before the product was renamed
# Stated; an install removes these copies so agents see one set.
RETIRED_SKILLS = ("stated-answer", "premisedb-answer", "premisedb-feature-design", "premisedb-go-deeper",
                  "premisedb-research", "premisedb-state")


def install_skills(harness, source: Path = SKILLS) -> list[str]:
    """Copy the bundled skills into the harness's skills directory, replacing older copies; returns the
    skills that were written or updated. The same file copy the README describes, done by the program."""
    written = []
    for directory in harness.skills_directories():
        for name in RETIRED_SKILLS:
            if (directory / name / "SKILL.md").is_file():
                shutil.rmtree(directory / name)
        for skill in sorted(source.glob("*/SKILL.md")):
            target = directory / skill.parent.name / "SKILL.md"
            if not target.is_file() or target.read_bytes() != skill.read_bytes():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(skill, target)
                written.append(skill.parent.name)
    return written


class _Deadline:
    """Kills a process that runs past its time; `passed` says whether it had to."""

    def __init__(self, process, timeout: float | None):
        self.passed, self._process = False, process
        self._timer = threading.Timer(timeout, self._stop) if timeout is not None else None

    def _stop(self):
        self.passed = True
        self._process.kill()

    def __enter__(self):
        if self._timer:
            self._timer.start()
        return self

    def __exit__(self, *exc):
        if self._timer:
            self._timer.cancel()


class ClaudeCode:
    """Claude Code in headless mode, with the user's own login and settings plus the permissions the
    product itself needs."""

    name = "claude-code"

    def __init__(self, executable: str = "claude"):
        self.executable = executable

    @staticmethod
    def skills_directories() -> tuple[Path, ...]:
        """Where Claude Code loads personal skills from, as the README's install step describes."""
        return (Path.home() / ".claude" / "skills",)

    def run(self, prompt: str, *, cwd, env=None, timeout: float | None = None, writable=()) -> str:
        """The agent's final message. `writable` is for sandboxed harnesses; Claude Code's own CLI tool
        writes the database itself. The least the product needs: its own CLI and files in the workspace.
        Everything else follows the user's own settings, so a tool they have not allowed is refused as usual."""
        command = [self.executable, "-p", prompt, "--output-format", "stream-json", "--verbose",
                   "--allowedTools", *ALLOWED_TOOLS]
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        except OSError as error:
            raise HarnessError(f"Could not start {self.executable}: {error}") from None
        result, other = None, []
        with process, _Deadline(process, timeout) as deadline:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    other.append(line.strip())
                    continue
                if isinstance(event, dict) and event.get("type") == "result":
                    result = event
        status = process.returncode
        if deadline.passed:
            raise HarnessError(_OVERTIME)
        if result is None:
            detail = " ".join(line for line in other[-3:] if line) or "no result"
            raise _failure(f"{self.executable} exited with status {status}: {detail}")
        if result.get("is_error"):
            raise _failure(str(result.get("result") or "the agent reported an error"))
        return str(result.get("result") or "")


class Codex:
    """Codex in non-interactive mode (`codex exec --json`), with the user's own login and config and a
    sandbox that can write in the workspace. A command the sandbox refuses fails rather than asking."""

    name = "codex"

    def __init__(self, executable: str = "codex", model: str | None = None, reasoning_effort: str | None = None):
        self.executable, self.model, self.reasoning_effort = executable, model, reasoning_effort

    @staticmethod
    def skills_directories() -> tuple[Path, ...]:
        """Where Codex loads personal skills from, as the README's install step describes."""
        return (Path.home() / ".agents" / "skills",)

    def run(self, prompt: str, *, cwd, env=None, timeout: float | None = None, writable=()) -> str:
        """The agent's final message. workspace-write is the least that lets SQLite open the workspace
        database (in `cwd`, or in a folder given in `writable`); the rest is the user's config."""
        options = ["--json", "--skip-git-repo-check", "-s", "workspace-write"] + (["-m", self.model] if self.model else []) + (
            ["-c", f'model_reasoning_effort="{self.reasoning_effort}"'] if self.reasoning_effort else []) + [
            part for folder in writable for part in ("--add-dir", str(folder))]
        with tempfile.TemporaryDirectory() as directory:
            last = Path(directory) / "last.txt"
            command = [self.executable, "exec", *options, "-o", str(last), prompt]
            try:
                process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, encoding="utf-8")
            except OSError as error:
                raise HarnessError(f"Could not start {self.executable}: {error}") from None
            finished, errors = False, []
            with process, _Deadline(process, timeout) as deadline:
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("type")
                    if kind == "turn.completed":
                        finished = True
                    elif kind in ("turn.failed", "error"):
                        errors.append(str(event.get("message") or (event.get("error") or {}).get("message") or kind))
                stderr = process.stderr.read()
            if deadline.passed:
                raise HarnessError(_OVERTIME)
            if not finished:
                detail = "; ".join(errors) or stderr.strip().splitlines()[-1:] and stderr.strip().splitlines()[-1] or "no result"
                raise _failure(f"{self.executable} exited with status {process.returncode}: {detail}")
            return last.read_text(encoding="utf-8") if last.is_file() else ""


_OVERTIME = "The agent ran well past the time it was given and was stopped."

