"""Claude's isolated CLI invocation, result envelope and auth status."""
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .base import (Answer, AnswerError, AuthError, Diagnostics, Identity, InputError, QuotaError,
                   TransportError, environment)

INPUT_FAILURES = ("max_turns", "prompt is too long", "too many tokens", "budget", "structured output", "structured_output")


def reset_time(detail: str, now: float) -> float | None:
    """Accept explicit ISO dates or the CLI's 'resets 3pm (Area/City)' hint; never guess a timezone."""
    iso = re.search(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:\d\d)", detail)
    if iso:
        try:
            return datetime.fromisoformat(iso[0].replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    hint = re.search(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", detail, re.I)
    if hint:
        hour, minute, period, zone = hint.groups()
        if not 1 <= int(hour) <= 12:
            return None
        try:
            local = datetime.fromtimestamp(now, ZoneInfo(zone))
            reset = local.replace(hour=int(hour) % 12 + (12 if period.lower() == "pm" else 0),
                                  minute=int(minute or 0), second=0, microsecond=0)
            if reset.timestamp() <= now:
                reset += timedelta(days=1)
            return reset.timestamp()
        except (ValueError, ZoneInfoNotFoundError):
            return None
    return None


def failure(detail: str, diagnostics: Diagnostics, fix_command: str | None, now: float, *, allow_input=True):
    lowered = detail.lower()
    if any(s in lowered for s in ("failed to authenticate", "oauth", "not logged in", "authentication", "auth_error",
                                  "invalid api key", "invalid auth token", "fix external api key", "fix external auth token")):
        return AuthError(detail, diagnostics, fix_command)
    if any(s in lowered for s in ("rate_limit", "rate limit", "rate-limit", "quota", "usage limit", "you've hit your limit")):
        return QuotaError(detail, diagnostics, reset_at=reset_time(detail, now))
    if allow_input and any(s in lowered for s in INPUT_FAILURES):
        return InputError(f"claude gave up on this input: {detail[:300]}", diagnostics)
    return TransportError(f"claude reported an error: {detail[:300]}", diagnostics)


def parse_envelope(stdout: str, diagnostics: Diagnostics | None = None, *, fix_command: str | None = None,
                   now: float | None = None) -> dict:
    diagnostics = diagnostics or Diagnostics(stdout=stdout)
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AnswerError(f"claude output is not JSON: {exc}", diagnostics) from exc
    if not isinstance(env, dict):
        raise AnswerError("claude output is not a JSON object", diagnostics)
    subtype = str(env.get("subtype") or "")
    if env.get("is_error") or env.get("terminal_reason") not in (None, "completed") or subtype.startswith("error"):
        detail = f"{env.get('result') or ''} [{subtype} {env.get('terminal_reason', '')}]".strip()
        exc = failure(detail, diagnostics, fix_command, time.time() if now is None else now)
        reset = env.get("reset_at")
        if isinstance(exc, QuotaError) and isinstance(reset, (int, float)) and not isinstance(reset, bool) and math.isfinite(reset):
            exc.reset_at = float(reset)
        raise exc
    out = env.get("structured_output")
    if not isinstance(out, dict):
        raise AnswerError("claude answered without structured_output", diagnostics)
    return out


class ClaudeEngine:
    def __init__(self, model: str, profile: str | None = None, effort: str = "medium", runner=None,
                 clock=time.time):
        self.model, self.profile, self.effort = model, profile, effort
        self._runner, self.clock = runner or self._run, clock
        self._process = None
        self._process_lock = threading.Lock()
        self._cancelled = False
        self._cancel_deadline = None
        self._group_killed = False
        self._process_done = threading.Event()

    def cancel(self):
        """Stop the active CLI, including a probe; cancellation is terminal for this engine."""
        with self._process_lock:
            if self._cancelled:
                return
            self._cancel_deadline = time.monotonic() + 2
            self._cancelled = True
            proc = self._process
            if proc is None:
                return
            self._signal_group(proc, signal.SIGTERM)
        if not self._process_done.wait(max(0, self._cancel_deadline - time.monotonic())):
            self._kill_group(proc)

    def _kill_group(self, proc):
        with self._process_lock:
            if self._process is proc and not self._group_killed:
                self._signal_group(proc, signal.SIGKILL)
                self._group_killed = True

    @staticmethod
    def _signal_group(proc, sig):
        # Reap an exited leader before signalling any surviving group members.
        proc.poll()
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    def _cleanup(self, proc):
        try:
            self._kill_group(proc)
            return proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            # An escaped descendant may still hold the write ends of these pipes.
            return "" if proc.text_mode else b"", "" if proc.text_mode else b""
        finally:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass  # A broken stdin must not prevent closing outputs and reaping the parent.
            proc.wait(timeout=2)

    def _run(self, command, *, input=None, timeout=None, stdin=None, **kwargs):
        kwargs.pop("capture_output")
        kwargs.pop("check")
        with self._process_lock:
            if self._cancelled:
                raise OSError("engine cancelled")
            proc = subprocess.Popen(command, stdin=subprocess.PIPE if input is not None else stdin,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, **kwargs)
            self._process_done.clear()
            self._group_killed = False
            self._process = proc
        try:
            try:
                deadline = time.monotonic() + timeout if timeout is not None else float("inf")
                while True:
                    try:
                        # Check cancellation without letting inherited pipes retain the runner indefinitely.
                        stdout, stderr = proc.communicate(input=input, timeout=min(1, max(0, deadline - time.monotonic())))
                        break
                    except subprocess.TimeoutExpired:
                        input = None  # communicate retains any unsent input across retries
                        if self._cancelled:
                            # cancel() owns the two-second SIGTERM grace period.
                            try:
                                stdout, stderr = proc.communicate(timeout=max(0, self._cancel_deadline - time.monotonic()))
                            except subprocess.TimeoutExpired:
                                stdout, stderr = self._cleanup(proc)
                            break
                        if time.monotonic() >= deadline:
                            raise
            except BaseException:
                self._cleanup(proc)
                raise
            return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
        finally:
            with self._process_lock:
                self._process = None
                self._process_done.set()

    @property
    def label(self) -> str:
        return "claude/" + "-".join(self.model.split())

    @property
    def fix_command(self) -> str:
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "unset ANTHROPIC_API_KEY or replace it"
        prefix = f"CLAUDE_CONFIG_DIR={shlex.quote(self.profile)} " if self.profile else ""
        return prefix + "claude auth login"

    def command(self, system_file: str, schema: dict) -> list[str]:
        # Empty tools/MCP and an empty working directory keep hooks and plugins out, while retaining OAuth.
        return ["claude", "-p", "--model", self.model, "--effort", self.effort, "--output-format", "json",
                "--json-schema", json.dumps(schema), "--tools", "",
                "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {}}),
                "--setting-sources", "project", "--permission-mode", "dontAsk",
                "--append-system-prompt-file", system_file, "--no-session-persistence", "--max-turns", "3"]

    def ask(self, system: str, prompt: str, schema: dict, timeout_s: int, cwd: Path) -> Answer:
        safe_command = []
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cwd, prefix="system-", suffix=".md") as system_file:
                os.fchmod(system_file.fileno(), 0o600)
                system_file.write(system)
                system_file.flush()
                safe_command = self.command(system_file.name, schema)
                # argv uses the filesystem codec even with UTF-8 pipes.
                command = [os.fsdecode(arg.encode("utf-8")) for arg in safe_command]
                proc = self._runner(command, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=timeout_s,
                                    cwd=str(cwd), env=environment(self.profile), check=False)
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"claude timed out after {timeout_s}s",
                                 Diagnostics(exc.stdout or b"", exc.stderr or b"", None, safe_command)) from exc
        except OSError as exc:
            detail = "claude CLI not found on PATH" if isinstance(exc, FileNotFoundError) else f"claude could not start: {exc}"
            raise TransportError(detail, Diagnostics(command_without_secrets=safe_command)) from exc
        diagnostics = Diagnostics(proc.stdout or "", proc.stderr or "", proc.returncode, safe_command)
        if proc.returncode != 0 and not diagnostics.stdout.lstrip().startswith("{"):
            raise failure(f"claude exited {proc.returncode}: {diagnostics.stderr.strip()}",
                          diagnostics, self.fix_command, self.clock(), allow_input=False)
        return Answer(parse_envelope(diagnostics.stdout, diagnostics, fix_command=self.fix_command, now=self.clock()),
                      diagnostics)

    def probe(self) -> Identity:
        try:
            # `claude auth status --help` exposes no --setting-sources option. Keep the
            # same empty-cwd isolation as ask(), even though its settings flag is unavailable here.
            with tempfile.TemporaryDirectory(prefix="lightkeeper-") as cwd:
                proc = self._runner(["claude", "auth", "status"], capture_output=True, text=True, encoding="utf-8", timeout=20,
                                    cwd=cwd, env=environment(self.profile), check=False, stdin=subprocess.DEVNULL)
            status = json.loads(proc.stdout)
            if not isinstance(status, dict) or not isinstance(status.get("loggedIn"), bool):
                raise ValueError("expected a JSON object with boolean loggedIn")
            account = status.get("email") or status.get("account") or status.get("accountId")
            if isinstance(account, dict):
                account = account.get("email") or account.get("name") or account.get("id")
            account = account if isinstance(account, str) else None
            ok = status["loggedIn"] and proc.returncode == 0
            return Identity(ok, account, "authenticated" if ok else "not logged in", None if ok else self.fix_command)
        except subprocess.TimeoutExpired:
            return Identity(False, detail="claude auth status timed out after 20s", fix_command=self.fix_command)
        except (OSError, ValueError, TypeError) as exc:
            return Identity(False, detail=f"claude auth status failed: {exc}", fix_command=self.fix_command)
