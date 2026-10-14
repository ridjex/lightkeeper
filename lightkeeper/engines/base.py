"""The small boundary between a reviewer and a model CLI."""
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Diagnostics:
    stdout: str | bytes | None = None
    stderr: str | bytes | None = None
    exit_code: int | None = None
    command_without_secrets: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Answer:
    parsed: dict
    diagnostics: Diagnostics


@dataclass(frozen=True)
class Identity:
    ok: bool
    account: str | None = None
    detail: str = ""
    fix_command: str | None = None


class EngineError(RuntimeError):
    def __init__(self, message: str, diagnostics: Diagnostics | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or Diagnostics()


class AuthError(EngineError):
    def __init__(self, message: str, diagnostics: Diagnostics | None = None, fix_command: str | None = None):
        super().__init__(message, diagnostics)
        self.fix_command = fix_command


class QuotaError(EngineError):
    def __init__(self, message: str, diagnostics: Diagnostics | None = None, reset_at: float | None = None):
        super().__init__(message, diagnostics)
        self.reset_at = reset_at


class TransportError(EngineError):
    pass


class InputError(EngineError):
    """Retrying this input will never work."""


class AnswerError(EngineError):
    """The output is not a valid answer."""


class Engine(Protocol):
    label: str

    def ask(self, system: str, prompt: str, schema: dict, timeout_s: int, cwd: Path) -> Answer: ...

    def probe(self) -> Identity: ...


def environment(profile: str | None) -> dict[str, str]:
    # USER lets the CLI find keychain credentials. Never inherit hooks or nested-session settings.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "LANG")
           if k in os.environ}
    if profile:
        env["CLAUDE_CONFIG_DIR"] = profile
    if os.environ.get("ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
    return env
