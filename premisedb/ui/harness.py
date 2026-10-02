"""Run a prompt through the user's own coding agent, headless, and report what it does."""

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib


class HarnessError(RuntimeError):
    """The harness could not be started or ended without an answer."""


@dataclass(frozen=True)
class Reply:
    text: str  # The agent's final message.
    session_id: str | None  # Pass back to continue the same conversation.
    denied: tuple[str, ...]  # Tools refused under the workspace's permission settings.


class ClaudeCode:
    """Claude Code in headless mode, with the login and permission settings of the directory it runs in."""

    name = "claude-code"

    def __init__(self, executable: str = "claude"):
        self.executable = executable

    @staticmethod
    def skills_directories() -> tuple[Path, ...]:
        """Where Claude Code loads personal skills from, as the README's install step describes."""
        return (Path.home() / ".claude" / "skills",)

    @staticmethod
    def permission_advice(cwd, home: Path | None = None) -> str | None:
        """Headless, Claude Code cannot ask before running a command, so the workspace must be trusted and its
        settings must allow the premisedb CLI, or every call is refused. Returns what to do when they do not."""
        home, cwd = home or Path.home(), Path(cwd).resolve()
        files = [cwd / ".claude" / name for name in ("settings.json", "settings.local.json")] + [
            home / ".claude" / "settings.json"]
        for path in files:
            if any(isinstance(rule, str) and rule.startswith("Bash(premisedb")
                   for rule in _read_json(path).get("permissions", {}).get("allow", [])):
                break
        else:
            return (f"No settings file allows the premisedb CLI, so a headless Claude Code would refuse every call. "
                    f'Add to {files[0]}:\n  {{"permissions": {{"allow": ["Bash(premisedb:*)", "Read"]}}}}')
        if not _read_json(home / ".claude.json").get("projects", {}).get(str(cwd), {}).get("hasTrustDialogAccepted"):
            return (f"Claude Code has not been told to trust {cwd}, so it ignores the workspace's permission settings. "
                    f"Run `claude` there once and accept the trust dialog.")
        return None

    def run(self, prompt: str, *, cwd, env=None, session_id: str | None = None,
            on_progress=lambda text: None) -> Reply:
        command = [self.executable, "-p", prompt, "--output-format", "stream-json", "--verbose"]
        if session_id:
            command += ["--resume", session_id]
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        except OSError as error:
            raise HarnessError(f"Could not start {self.executable}: {error}") from None
        result, other = None, []
        with process:
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
        if result is None:
            detail = " ".join(line for line in other[-3:] if line) or "no result"
            raise HarnessError(f"{self.executable} exited with status {status}: {detail}")
        if result.get("is_error"):
            raise HarnessError(str(result.get("result") or "the agent reported an error"))
        denied = tuple(dict.fromkeys(str(d.get("tool_name")) for d in result.get("permission_denials") or []
                                     if isinstance(d, dict)))
        return Reply(str(result.get("result") or ""), result.get("session_id"), denied)


class Codex:
    """Codex in non-interactive mode (`codex exec --json`), with the user's own login and config. Commands
    run inside Codex's sandbox as the user's config sets it; a refused command fails rather than asking."""

    name = "codex"

    def __init__(self, executable: str = "codex", model: str | None = None, reasoning_effort: str | None = None):
        self.executable, self.model, self.reasoning_effort = executable, model, reasoning_effort

    @staticmethod
    def skills_directories() -> tuple[Path, ...]:
        """Where Codex loads personal skills from, as the README's install step describes."""
        return (Path.home() / ".agents" / "skills",)

    @staticmethod
    def permission_advice(cwd, home: Path | None = None) -> str | None:
        """Codex asks no one in exec mode; its sandbox decides. Under the default read-only sandbox SQLite
        cannot even open the workspace database, so the user's config must allow writes in the workspace."""
        path = (home or Path.home()) / ".codex" / "config.toml"
        try:
            mode = tomllib.loads(path.read_text(encoding="utf-8")).get("sandbox_mode")
        except (OSError, ValueError):
            mode = None
        if mode in ("workspace-write", "danger-full-access"):
            return None
        return (f"Codex's sandbox is {mode or 'read-only'}, under which premisedb cannot open the workspace database. "
                f'Add to {path}:\n  sandbox_mode = "workspace-write"')

    def run(self, prompt: str, *, cwd, env=None, session_id: str | None = None,
            on_progress=lambda text: None) -> Reply:
        options = ["--json", "--skip-git-repo-check"] + (["-m", self.model] if self.model else []) + (
            ["-c", f'model_reasoning_effort="{self.reasoning_effort}"'] if self.reasoning_effort else [])
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
            with process:
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
                    elif kind == "turn.completed":
                        finished = True
                    elif kind in ("turn.failed", "error"):
                        errors.append(str(event.get("message") or (event.get("error") or {}).get("message") or kind))
                stderr = process.stderr.read()
            if not finished:
                detail = "; ".join(errors) or stderr.strip().splitlines()[-1:] and stderr.strip().splitlines()[-1] or "no result"
                raise HarnessError(f"{self.executable} exited with status {process.returncode}: {detail}")
            text = last.read_text(encoding="utf-8") if last.is_file() else ""
        return Reply(text, thread, ())


def _shell(command: str) -> str:
    """A progress line for a Codex shell command, without the wrapper it runs commands through."""
    for prefix in ("/bin/zsh -lc ", "/bin/bash -lc ", "bash -lc ", "zsh -lc "):
        if command.startswith(prefix):
            command = command[len(prefix):].strip("'\"")
    text = " ".join(f"Shell: {command}".split())
    return text if len(text) <= 160 else text[:159] + "…"


def _describe(tool_use: dict) -> str:
    """One line for the progress display: what the agent says it is doing, or the call itself."""
    given = tool_use.get("input") or {}
    detail = next((given[key] for key in ("description", "command", "skill", "file_path", "pattern", "query", "url")
                   if isinstance(given.get(key), str) and given[key].strip()), "")
    text = " ".join(f"{tool_use.get('name', 'tool')}: {detail}".split()).rstrip(": ")
    return text if len(text) <= 160 else text[:159] + "…"


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
