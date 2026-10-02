"""Run a prompt through the user's own coding agent, headless, and report what it does."""

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess


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
