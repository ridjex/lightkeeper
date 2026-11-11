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
                   Trace, TransportError, environment)
from ..workspace import REVIEW_MAX_TURNS, escape

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


def stream_trace(stdout: str | bytes, cwd: Path) -> tuple[dict | None, Trace, bool]:
    """Pair Read calls with successful results; attempts alone are never coverage."""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    calls, opened, denied = {}, {}, {}
    result, seen = None, False
    root = cwd.resolve()

    def denial(event):
        call = calls.get(event.get("tool_use_id"), event)
        name = call.get("name") or call.get("tool_name") or "unknown tool"
        argument = call.get("input", call.get("tool_input", {}))
        line = f"{escape(str(name))} {json.dumps(argument, ensure_ascii=False, sort_keys=True)}"
        denied[line] = None

    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue  # A timeout can leave a partial last event; a result is still required below.
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            result = event
        if event.get("type") == "system" and event.get("subtype") == "permission_denied":
            denial(event)
        content = (event.get("message") or {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                seen = True
                calls[block.get("id")] = block
            elif block.get("type") == "tool_result" and not block.get("is_error"):
                call = calls.get(block.get("tool_use_id"), {})
                value = (call.get("input") or {}).get("file_path")
                if call.get("name") == "Read" and isinstance(value, str):
                    path = (root / value).resolve()
                    if path.is_relative_to(root) and path.is_file():
                        opened[path.relative_to(root).as_posix()] = None
    result = result or None
    for event in (result or {}).get("permission_denials") or []:
        if isinstance(event, dict):
            denial(event)
    turns = (result or {}).get("num_turns", 0)
    cost = (result or {}).get("total_cost_usd")
    return result, Trace(tuple(opened), tuple(denied), turns if type(turns) is int else 0,
                         float(cost) if type(cost) in (int, float) and math.isfinite(cost) else None), seen


def stopped(bound: str, trace: Trace, cwd: Path, diagnostics: Diagnostics) -> InputError:
    offered = sum(path.is_file() for path in cwd.rglob("*"))
    error = InputError(f"review stopped at the {bound} after opening {len(trace.opened)} of {offered} files", diagnostics)
    error.trace = trace
    return error


class ClaudeEngine:
    def __init__(self, model: str, profile: str | None = None, effort: str = "medium", runner=None,
                 clock=time.time, budget_usd: float = 5.0):
        self.budget_usd = budget_usd
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
        # Built-in read-only tools are confined by dontAsk to the material workspace.
        return ["claude", "-p", "--model", self.model, "--effort", self.effort, "--output-format", "stream-json", "--verbose",
                "--json-schema", json.dumps(schema), "--tools", "Read,Grep,Glob",
                "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {}}),
                "--setting-sources", "project", "--permission-mode", "dontAsk",
                "--append-system-prompt-file", system_file, "--no-session-persistence", "--max-turns", str(REVIEW_MAX_TURNS),
                "--max-budget-usd", str(self.budget_usd)]

    def ask(self, system: str, prompt: str, schema: dict, timeout_s: int, cwd: Path) -> Answer:
        safe_command = []
        try:
            with tempfile.TemporaryDirectory(prefix="lightkeeper-system-") as system_dir, \
                    tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=system_dir, suffix=".md") as system_file:
                os.fchmod(system_file.fileno(), 0o600)
                system_file.write(system)
                system_file.flush()
                safe_command = self.command(system_file.name, schema)
                # argv uses the filesystem codec even with UTF-8 pipes.
                command = [os.fsdecode(arg.encode("utf-8")) for arg in safe_command]
                proc = self._runner(command, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=timeout_s,
                                    cwd=str(cwd), env=environment(self.profile), check=False)
        except subprocess.TimeoutExpired as exc:
            diagnostics = Diagnostics(exc.stdout or b"", exc.stderr or b"", None, safe_command)
            _, trace, seen = stream_trace(diagnostics.stdout, cwd)
            if seen:
                raise stopped(f"{timeout_s}-second time limit", trace, cwd, diagnostics) from exc
            raise TransportError(f"claude timed out after {timeout_s}s", diagnostics) from exc
        except OSError as exc:
            detail = "claude CLI not found on PATH" if isinstance(exc, FileNotFoundError) else f"claude could not start: {exc}"
            raise TransportError(detail, Diagnostics(command_without_secrets=safe_command)) from exc
        diagnostics = Diagnostics(proc.stdout or "", proc.stderr or "", proc.returncode, safe_command)
        if proc.returncode != 0 and not diagnostics.stdout.lstrip().startswith("{"):
            raise failure(f"claude exited {proc.returncode}: {diagnostics.stderr.strip()}",
                          diagnostics, self.fix_command, self.clock(), allow_input=False)
        result, trace, _ = stream_trace(diagnostics.stdout, cwd)
        try:
            if result is None and proc.returncode != 0:      # the CLI died mid-stream: retry, do not settle
                raise failure(f"claude exited {proc.returncode} without a result: {diagnostics.stderr.strip()}",
                              diagnostics, self.fix_command, self.clock(), allow_input=False)
            if result is None:
                raise AnswerError("claude stream has no result line", diagnostics)
            parsed = parse_envelope(json.dumps(result), diagnostics, fix_command=self.fix_command, now=self.clock())
        except (InputError, AnswerError, AuthError, QuotaError, TransportError) as exc:
            exc.trace = trace
            if isinstance(exc, InputError):
                reason = f"{result.get('subtype', '')} {result.get('terminal_reason', '')} {exc}".lower()
                if "max_turns" in reason:
                    raise stopped(f"{REVIEW_MAX_TURNS}-turn limit", trace, cwd, diagnostics) from exc
                if "budget" in reason:
                    raise stopped(f"${self.budget_usd:g} budget limit", trace, cwd, diagnostics) from exc
            raise
        return Answer(parsed, diagnostics, trace)

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
