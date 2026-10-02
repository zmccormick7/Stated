"""Run a prompt through the user's own coding agent, headless, and report what it does."""

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading


class HarnessError(RuntimeError):
    """The harness could not be started or ended without an answer."""


class UsageLimit(HarnessError):
    """The user's subscription limit was reached. `resets_at` is the Unix time it lifts, when the harness says."""

    def __init__(self, message: str, resets_at: float | None = None):
        super().__init__(message)
        self.resets_at = resets_at


_LIMIT = re.compile(r"(usage|session|weekly|hourly|5-hour|rate)[ -]limit|limit reached|hit your [\w -]*limit|quota", re.I)


def _failure(message: str) -> HarnessError:
    """The error for a harness's own account of what went wrong, told apart when it is a usage limit."""
    if not _LIMIT.search(message):
        return HarnessError(message)
    epoch = re.search(r"\|(\d{10})\b", message)  # Claude Code appends the reset time to some limit messages.
    return UsageLimit(message, float(epoch.group(1)) if epoch else None)


@dataclass(frozen=True)
class Reply:
    text: str  # The agent's final message.
    session_id: str | None  # Pass back to continue the same conversation.
    denied: tuple[str, ...]  # Tools refused under the workspace's permission settings.


ALLOWED_TOOLS = ("Bash(premisedb:*)", "Read", "Write")
WEB_TOOLS = ("WebSearch", "WebFetch")


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

    def run(self, prompt: str, *, cwd, env=None, session_id: str | None = None, web: bool = False,
            timeout: float | None = None, on_progress=lambda text: None) -> Reply:
        # The least the product needs: its own CLI, files in the workspace, and the web when the turn's
        # effort calls for outside sources. Everything else follows the user's own settings, so a tool
        # they have not allowed is refused as usual.
        command = [self.executable, "-p", prompt, "--output-format", "stream-json", "--verbose",
                   "--allowedTools", *ALLOWED_TOOLS, *(WEB_TOOLS if web else ())]
        if session_id:
            command += ["--resume", session_id]
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
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "assistant":
                    for item in (event.get("message") or {}).get("content") or []:
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            on_progress(_describe(item))
                elif event.get("type") == "result":
                    result = event
        status = process.returncode
        if deadline.passed:
            raise HarnessError(_OVERTIME)
        if result is None:
            detail = " ".join(line for line in other[-3:] if line) or "no result"
            raise _failure(f"{self.executable} exited with status {status}: {detail}")
        if result.get("is_error"):
            raise _failure(str(result.get("result") or "the agent reported an error"))
        denied = tuple(dict.fromkeys(str(d.get("tool_name")) for d in result.get("permission_denials") or []
                                     if isinstance(d, dict)))
        return Reply(str(result.get("result") or ""), result.get("session_id"), denied)


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

    def run(self, prompt: str, *, cwd, env=None, session_id: str | None = None, web: bool = False,
            timeout: float | None = None, on_progress=lambda text: None) -> Reply:
        # workspace-write: the least that lets SQLite open the workspace database; the rest is the user's config.
        options = ["--json", "--skip-git-repo-check", "-s", "workspace-write"] + (["-m", self.model] if self.model else []) + (
            ["-c", f'model_reasoning_effort="{self.reasoning_effort}"'] if self.reasoning_effort else []) + (
            ["-c", 'web_search="live"', "-c", "sandbox_workspace_write.network_access=true"] if web else [])
        with tempfile.TemporaryDirectory() as directory:
            last = Path(directory) / "last.txt"
            command = [self.executable, "exec", *options, "-o", str(last)] + (
                ["resume", session_id, prompt] if session_id else [prompt])
            try:
                process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, encoding="utf-8")
            except OSError as error:
                raise HarnessError(f"Could not start {self.executable}: {error}") from None
            thread, finished, errors = session_id, False, []
            with process, _Deadline(process, timeout) as deadline:
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind, item = event.get("type"), event.get("item") or {}
                    if kind == "thread.started":
                        thread = event.get("thread_id") or thread
                    elif kind == "item.started" and item.get("type") == "command_execution":
                        on_progress(_shell(item.get("command") or ""))
                    elif kind == "item.completed" and item.get("type") == "web_search" and item.get("query"):
                        on_progress(_shorten(f"Web: {item['query']}"))
                    elif kind == "turn.completed":
                        finished = True
                    elif kind in ("turn.failed", "error"):
                        errors.append(str(event.get("message") or (event.get("error") or {}).get("message") or kind))
                stderr = process.stderr.read()
            if deadline.passed:
                raise HarnessError(_OVERTIME)
            if not finished:
                detail = "; ".join(errors) or stderr.strip().splitlines()[-1:] and stderr.strip().splitlines()[-1] or "no result"
                raise _failure(f"{self.executable} exited with status {process.returncode}: {detail}")
            text = last.read_text(encoding="utf-8") if last.is_file() else ""
        return Reply(text, thread, ())


_OVERTIME = "The agent ran well past the time it was given and was stopped."


def _shorten(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= 160 else text[:159] + "…"


def _shell(command: str) -> str:
    """A progress line for a Codex shell command, without the wrapper it runs commands through."""
    for prefix in ("/bin/zsh -lc ", "/bin/bash -lc ", "bash -lc ", "zsh -lc "):
        if command.startswith(prefix):
            command = command[len(prefix):].strip("'\"")
    return _shorten(f"Shell: {command}")


def _describe(tool_use: dict) -> str:
    """One line for the progress display: what the agent says it is doing, or the call itself."""
    given = tool_use.get("input") or {}
    detail = next((given[key] for key in ("description", "command", "skill", "file_path", "pattern", "query", "url")
                   if isinstance(given.get(key), str) and given[key].strip()), "")
    return _shorten(" ".join(f"{tool_use.get('name', 'tool')}: {detail}".split()).rstrip(": "))

