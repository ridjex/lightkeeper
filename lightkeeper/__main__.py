"""The polling loop (Runner) and the CLI entry point (main)."""
from __future__ import annotations

import argparse
import calendar
import faulthandler
import hashlib
import json
import logging
import queue
import re
from logging.handlers import RotatingFileHandler
from contextlib import ExitStack
from dataclasses import asdict, replace
import signal
import threading
import os
import subprocess
import sys
import time
from pathlib import Path

from .engines.base import AuthError, QuotaError
from .engines.claude import ClaudeEngine
from .updates import UpdateChecker
from .commands import CommandInbox, StopEvent
from .github import Gh, GhError, GhTimeout, PRStatus
from .journal import Journal, JournalError, Lock, key_str
from .migrate import migrate, rehash
from .settings import Settings, SettingsError, changed_components, config_path, load_settings, prepare_config_dir
from .models import MARKER, AnswerV2, Decision, Finding, PullRequest, WorkflowRun, is_automated
from .posting import owner_approval_body, render_body
from .policy import POLL_INTERVAL_S, PolicyError, approve_floor, evaluate_pr
from .reviewer import ClaudeReviewer, ReviewerError, UnclearAnswer

# Textual switches the terminal to the kitty keyboard protocol while a screen is open. Every key the dashboard
# and the preview use has a plain encoding, so the protocol buys nothing here, and a terminal that keeps the
# protocol on after the process ends turns Backspace into "7u" at the shell prompt. Set before Textual loads.
os.environ["TEXTUAL_DISABLE_KITTY_KEY"] = "1"

RUNNING_JOB_STATES = ("queued", "in_progress", "pending", "requested")
MAX_BACKOFF_S = 600
FAST_POLL_S = 15
REVIEWER_PAUSE_S = 900
NOTIFICATION_WINDOW_S = 3 * 24 * 3600
CI_WAIT_MAX_S = 3 * 3600          # checks still running after this long: tell the owner once, keep waiting
_REVIEW_STATES = {"approve": "APPROVED", "request_changes": "CHANGES_REQUESTED", "comment": "COMMENTED"}


_logger = logging.getLogger("lightkeeper")
_plain = True
_hang_fd = None  # Kept open for the process lifetime: faulthandler writes directly to it.
_TERMINAL_CONTROLS = re.compile(
    r"\x1b\](?:(?!\x07|\x1b\\).)*(?:\x07|\x1b\\)"  # OSC up to BEL or ST; an unterminated one loses only its ESC.
    r"|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[ -/]*[0-~]?|[\x00-\x08\x0b-\x1f\x7f]")


def clean_log_text(text: str) -> str:
    return _TERMINAL_CONTROLS.sub("", text)


class PlainLogFormatter(logging.Formatter):
    def format(self, record):
        return clean_log_text(super().format(record))


def default_state_root() -> Path:
    return Path.home() / ".local/state/lightkeeper"


def live_lock_path(login: str) -> Path:
    return default_state_root() / "locks" / login


def effective_mode(settings: Settings, state_dir: Path, *, once=False, dry_run=False, allow_live=False):
    """Global ceiling; repositories still choose their own posting mode."""
    custom = state_dir.resolve() != (default_state_root() / settings.login).resolve()
    notifications = not custom or allow_live
    if once or dry_run:
        reason = "--once" if once else "--dry-run"
    elif custom and not allow_live:
        reason = "non-default state directory (use --allow-live to permit live mode and notifications)"
    else:
        return "live", "repository settings", notifications
    return "dry-run", reason, notifications


def can_be_live(settings: Settings, mode: str) -> bool:
    return mode == "live" and any(repo.enabled and repo.post == "live" for repo in settings.repos.values())


class PrivateLogHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")


def configure_logging(state_dir: Path, plain: bool = True) -> None:
    global _plain
    _plain = plain
    path = str((state_dir / "run.log").resolve())
    if not any(getattr(h, "baseFilename", None) == path for h in _logger.handlers):
        for handler in _logger.handlers[:]:
            _logger.removeHandler(handler)
            handler.close()
        state_dir.mkdir(parents=True, exist_ok=True)
        handler = PrivateLogHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3)
        handler.setFormatter(PlainLogFormatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        _logger.addHandler(handler)
        _logger.setLevel(logging.INFO)
        _logger.propagate = False


def _thread_exception(args):
    _logger.error("uncaught thread exception", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def configure_diagnostics(state_dir: Path) -> None:
    global _hang_fd
    if _hang_fd is None:
        _hang_fd = os.open(state_dir / "hang.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(_hang_fd, 0o600)
    faulthandler.register(signal.SIGUSR1, file=_hang_fd, all_threads=True)
    threading.excepthook = _thread_exception


def log(msg: str) -> None:
    msg = clean_log_text(msg)
    _logger.info(msg)
    if _plain:
        print(time.strftime("%H:%M:%S"), msg, flush=True)


def item_url(kind: str, key, fields: dict) -> str:
    """The GitHub page a journal line is about: the review when one was posted, else the PR or the run."""
    if kind == "pr":
        base = f"https://github.com/{key[0]}/pull/{key[1]}"
        return f"{base}#pullrequestreview-{fields['review_id']}" if fields.get("review_id") else base
    return f"https://github.com/{key[0]}/actions/runs/{key[1]}"


def item_label(kind: str, key) -> str:
    if kind == "pr":
        return f"{key[0].split('/')[-1]}#{key[1]} @{str(key[2])[:7]}"
    return f"{key[0].split('/')[-1]} run {key[1]}/{key[2]} env {key[3]}"


EVENT_WORDS = {"defer": "waiting", "nothing": "silent", "ignore": "skipped", "satisfied": "done by owner",
               "owner_approve": "approved by you", "owner_error": "owner write failed",
               "assessment": "model says", "applied": "POSTED", "unknown": "write unknown", "unclear": "answer unclear",
               "notify": "told owner", "closed": "closed", "seen": "watching", "error": "write failed", "withheld": "withheld"}


def macos_notify(title: str, text: str) -> None:
    """One macOS notification; a failure is logged and never stops the loop (the journal has the text)."""
    if sys.platform != "darwin":
        return
    # Text travels as an argument, never inside the script: AppleScript does not decode \uXXXX escapes.
    script = ["-e", "on run argv", "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
              "-e", "end run"]
    try:
        proc = subprocess.run(["osascript", *script, text[:240], title], capture_output=True, text=True, encoding="utf-8",
                              timeout=10, check=False, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            log(f"notification failed (exit {proc.returncode}): {proc.stderr.strip()[:200]}")
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"notification failed: {exc}")


def _parse_iso(stamp: str) -> float:
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))


def check_repo(gh, name: str) -> str:
    """The same read-only access check for CLI and Runner commands."""
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", name):
        raise ValueError("repository must be org/name")
    try:
        info = gh.json(["api", f"repos/{name}"])
    except GhError as exc:
        if exc.no_access:
            return "no access"
        raise
    return "archived" if info["archived"] else f"access ok · default branch {info['default_branch']}"


class Runner:
    def __init__(self, settings: Settings, gh, journal: Journal, reviewer=None, clock=time.time, notifier=macos_notify,
                 observer=None, live_lock_held=False, global_mode="live", sleeper=time.sleep):
        self.settings = settings
        self.global_mode = global_mode
        self.gh = gh
        self.journal = journal
        self.reviewer = reviewer
        self.clock = clock
        self.sleeper = sleeper
        self.notifier = notifier
        self.observer = observer or (lambda event, **fields: None)
        self.live_lock_held = live_lock_held
        self._identity_reason = None
        self._identity_checked = False
        self._identity_failures = 0
        self._last_gh_login = None
        self._writes_blocked = None
        self._tick_failures = 0       # consecutive ticks that raised: drives the backoff
        self._error_ticks = 0         # consecutive ticks with any item error: drives the alert only
        self._rate_limited = False
        configure_logging(journal.path.parent, _plain)
        self.reviewer_paused_until = 0.0
        self._auth_paused = False
        self._auth_notified = False
        self._reviewer_notified = False
        self._in_tick = False
        self._tick_pause_reason = None
        self._auth_pause_reason = ""
        self._reviewer_pause_reason = ""
        self._probe_warning = None
        self._engine_probed = False
        self.quarantined: set[tuple[str, str, int]] = set()  # (kind, repo, PR number or run id)
        self.seen_runs: dict[tuple[str, int], str] = {}       # (repo, run_id) -> notification updated_at, once settled
        self._default_branch: dict[str, str] = {}
        self._pr_statuses: dict[tuple[str, int], PRStatus] = {}
        self.fast_poll_interval = FAST_POLL_S
        self._next_fast_poll = 0.0
        self._wake_reason = ""
        self._reported_components = {}
        self.wake = threading.Event()
        self.stop = StopEvent(self.wake)
        self.commands = CommandInbox(self.wake)
        self._reviewing_key = None
        self.updates = UpdateChecker(journal.path.parent, gh, clock)
        self._announced_updates = set()

    @property
    def cancelled(self):
        return self.stop.is_set()

    def drain_commands(self, paused_until=None):
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return
            outcome, retain = "done", False
            try:
                if command.kind == "wake":
                    text = (f"paused until {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(paused_until))}"
                            if paused_until is not None else "checking now")
                elif command.kind == "check_updates":
                    text = self.check_updates(force=True)
                elif command.kind == "probe_engine":
                    identity = self._probe_engine()
                    text = ("engine unavailable" if identity is None else "engine probe ok" if identity.ok
                            else f"engine probe failed: {identity.detail}")
                    if identity is None or not identity.ok:
                        outcome = "failed"
                elif command.kind == "check_repo":
                    repo = command.key[0] if command.key else ""
                    text = check_repo(self.gh, repo)
                    self.observe("repo_check", repo=repo, text=text)
                    self.observe("alert_attempt", key=f"quarantined:{repo}", text=text)
                    if text.startswith("access ok"):
                        self.quarantined = {item for item in self.quarantined if item[1] != repo}
                        self._quarantine_conditions()
                    else:
                        outcome = "failed"
                elif command.kind == "reopen":
                    text = self._reopen(command)
                    retain = text == "re-review queued"
                    if text.startswith("refused:"):
                        outcome = "refused"
                elif command.kind == "owner_approve":
                    text = self._owner_approve(command)
                    if text.startswith("refused:"):
                        outcome = "refused"
                    elif text.startswith(("error:", "unknown:")):
                        outcome = "failed"
                else:
                    outcome, text = "unsupported", f"unsupported command: {command.kind}"
            except Exception as exc:
                outcome, text = "failed", f"{command.kind} failed: {exc}"
                if command.kind == "check_repo" and command.key:
                    self.observe("repo_check", repo=command.key[0], text=text)
                    self.observe("alert_attempt", key=f"quarantined:{command.key[0]}", text=text)
                elif command.kind == "probe_engine":
                    for key in ("engine_identity", "reviewer_paused"):
                        self.observe("alert_attempt", key=key, text=text)
            finally:
                self.commands.complete(command, retain=retain)
            self.observe("command_done", id=command.id, outcome=outcome, text=text)

    def _owner_authority(self, repo):
        """Reload owner authority in order; identity is the last check before POST."""
        if self._writes_blocked:
            return self._writes_blocked
        try:
            fresh = load_settings(self.settings.config_dir)
            if fresh.login != self.settings.login or not fresh.login:
                raise SettingsError(fresh.config_dir / "settings.toml", "identity.login", "login changed; restart the tool")
        except (SettingsError, OSError) as exc:
            self.condition("settings_error", str(exc))
            return f"settings invalid: {exc}"
        self.condition("settings_error", None)
        if repo not in fresh.repos or not fresh.repo(repo).enabled:
            return "repository disabled"
        if "approve" not in fresh.repo(repo).allowed:
            return "approve disabled"
        if self.global_mode != "live" or fresh.repo(repo).post != "live":
            return "dry-run mode"
        if not self.live_lock_held:
            return "live lock not held; restart to enable live mode"
        self.refresh_settings(fresh)
        return self._check_identity()

    def _owner_outcome(self, key, event, **fields):
        try:
            self._journal("pr", key, event, **fields)
        except (OSError, JournalError) as exc:
            self._writes_blocked = "journal outcome could not be persisted; writes blocked until restart"
            log(f"{self._writes_blocked}: {exc}")
            self.condition("journal_error", self._writes_blocked, level="error")
            return False
        return True

    def _owner_approve(self, command):
        """Manual authority, independent of model eligibility and automated write boundaries."""
        key = command.key
        if not key or len(key) != 3 or command.head != key[2]:
            return "refused: invalid PR key or head"
        repo = key[0]
        if reason := self._owner_authority(repo):
            return f"refused: {reason}"
        if self.journal.is_unknown(key):
            return "refused: unknown write"
        if self.journal.is_final(key, "", "owner_approve", None):
            return "already approved by you"
        try:
            pr = self.gh.pull_request(repo, key[1])
        except Exception as exc:
            return f"refused: could not fetch PR: {exc}"
        self._publish_pr_status(PRStatus.from_pr(pr))
        if pr.state != "OPEN":
            return "refused: closed"
        if pr.head_sha != command.head:
            return "refused: head changed"
        if pr.author == self.settings.login:
            return "refused: own pull request"
        mine = [r for r in pr.reviews if r.author == self.settings.login]
        if any(r.state == "PENDING" for r in mine):
            return "refused: the owner has an unsubmitted review open"
        manual = next((r for r in reversed(mine) if r.state == "APPROVED" and r.commit_sha == command.head
                       and not is_automated(r.body)), None)
        if manual:
            self._journal("pr", key, "satisfied", origin="owner", source="observed",
                          reason="approved by you on GitHub", body=manual.body, review_id=manual.review_id,
                          reviewed_at=manual.submitted_at)
            return "already approved by you"
        if any(r.state == "APPROVED" and r.commit_sha == command.head for r in mine):
            return "already approved by you"
        floor = approve_floor(pr)
        body = owner_approval_body(command.text, self.settings.login)
        if floor and body == owner_approval_body("", self.settings.login):
            return f"refused: note required to override {floor}"
        if self.cancelled:
            return "refused: stopping"
        fields = dict(action="approve", origin="owner", body=body, title=pr.title, author=pr.author,
                      fingerprint=pr.fingerprint, body_digest=hashlib.sha256(body.encode()).hexdigest())
        self.observe("phase", phase="writing", kind="pr", key=key)
        try:
            # Durable intent precedes the final authority checks so nothing interrupts checks -> POST.
            self._journal("pr", key, "owner_attempt", head=command.head, **fields)
            if reason := self._owner_authority(repo):
                self._owner_outcome(key, "owner_error", reason=reason, **fields)
                return f"refused: {reason}"
            try:
                review_id = self.gh.submit_review(pr, "approve", body)
            except (KeyboardInterrupt, SystemExit):
                self._owner_outcome(key, "unknown", reason="interrupted during write", **fields)
                raise
            except Exception as exc:
                event = "owner_error" if isinstance(exc, GhError) and exc.definite else "unknown"
                self._owner_outcome(key, event, reason=str(exc), **fields)
                return f"{'error' if event == 'owner_error' else event}: {exc}"
            if review_id is None:
                self._owner_outcome(key, "unknown", reason="missing review id after write", **fields)
                return "unknown: missing review id after write"
            if not self._owner_outcome(key, "owner_approve", review_id=review_id, **fields):
                return f"unknown: {self._writes_blocked}"
            return "approved by you"
        finally:
            self.observe("phase", phase="idle")

    def _reopen(self, command):
        key = command.key
        if not key or len(key) != 3 or command.head != key[2]:
            return "refused: invalid PR key or head"
        if not self._in_scope(key[0]):
            return "refused: repository disabled"
        if self.journal.owner_takeover(key) or self.journal.is_protected(key):
            return "refused: taken over by you"
        if self._reviewing_key == key:
            return "refused: already queued"
        if self.journal.is_unknown(key):
            return "refused: unknown write"
        # An impossible context matches only forever-final records.
        if self.journal.is_final(key, "", "reopen", None):
            return "refused: forever-final"
        pr = self.gh.pull_request(key[0], key[1])
        self._publish_pr_status(PRStatus.from_pr(pr))
        if pr.state != "OPEN":
            return "refused: closed"
        if pr.head_sha != command.head:
            return "refused: head changed"
        protection = evaluate_pr(pr, self.settings)
        if protection.outcome in ("satisfied", "ignore"):
            return f"refused: {protection.reason}"
        self._journal("pr", key, "reopen", reason="re-review queued", title=pr.title, author=pr.author)
        return "re-review queued"

    def observe(self, event, **fields):
        if event == "phase" and fields.get("phase") == "checking":
            fields["reason"] = self._wake_reason
        try:
            self.observer(event, **fields)
        except Exception:
            _logger.exception("runner observer failed")

    def condition(self, key, text, **fields):
        self.observe("condition", key=key, text=text, **fields)

    def _check_identity(self) -> str | None:
        """Check the account before a tick or POST; log once per failure streak."""
        self._identity_checked = False
        actual = error = None
        # Three 15-second attempts plus 1/3-second pauses fit within the minute between ticks.
        for delay in (0, 1, 3):
            if delay:
                if self.cancelled:
                    break
                self.sleeper(delay)
            try:
                actual = self.gh.login(timeout_s=15)
                break
            except Exception as exc:  # malformed output and transport failures both block writes
                error = exc
                if isinstance(exc, GhError) and exc.rate_limited:
                    self._rate_limited = True    # GitHub asks for a pause, not a burst of retries
                    break
        if actual is not None:
            self._last_gh_login = actual
            self._identity_failures = 0
            reason = None if actual == self.settings.login else f"gh login is {actual}, expected {self.settings.login}"
            if reason and reason != self._identity_reason:
                log(reason)
            self.condition("gh_identity", reason)
        else:
            self._identity_failures += 1
            detail = f"GitHub rate limited: {error}" if isinstance(error, GhError) and error.rate_limited else str(error)
            reason = f"gh login lookup failed, expected {self.settings.login}: {detail}"
            if self._identity_failures == 1:
                log(f"gh unreachable, writes wait: {detail}")
            if self._identity_failures >= 3:
                self.condition("gh_identity", reason)
            # Below the streak the alert stays as it was: a confirmed mismatch must not fade into "unconfirmed".
        self._identity_reason = reason
        self._identity_checked = reason is None
        self.observe("gh_identity", login=self._last_gh_login, confirmed=actual is not None)
        self.observe("alert_attempt", key="gh_identity", text=reason or "gh identity ok")
        return reason

    def sleeping(self, seconds, phase="sleeping"):
        self.observe("sleeping", seconds=seconds, phase=phase)

    def _publish_pr_status(self, status: PRStatus):
        self._pr_statuses[(status.repo, status.number)] = status
        self.observe("pr_status", status=status)

    def light_poll(self, timeout_s=FAST_POLL_S) -> str | None:
        """Read-only hints; only a full fetch can affect a review or the durable watch list."""
        try:
            watched = [key for key in self.journal.watched_prs() if self._in_scope(key[0])
                       and ("pr", *key) not in self.quarantined]
            statuses = self.gh.pr_statuses(watched, timeout_s=timeout_s) if watched else []
            reason = None
            for status in statuses:
                previous = self._pr_statuses.get((status.repo, status.number))
                if previous and status.wake_key != previous.wake_key and reason is None:
                    change = ("new push" if status.head != previous.head else
                              "state changed" if status.state != previous.state else
                              "draft changed" if status.is_draft != previous.is_draft else
                              "CI finished" if status.ci.running == 0 else "CI check failed")
                    reason = f"{change} on {status.repo}#{status.number}"
                self._publish_pr_status(status)
            self.fast_poll_interval = FAST_POLL_S
            return reason
        except Exception as exc:  # a failed hint must never stop or accelerate the main loop
            detail = exc.stderr if isinstance(exc, GhError) else str(exc)
            log(f"light poll failed; skipping ({type(exc).__name__}: {detail[:200]})")
            self.fast_poll_interval = min(self.fast_poll_interval * 2, POLL_INTERVAL_S)
            return None

    def wait(self, stop, seconds, *, allow_wake=True, mono=time.monotonic):
        deadline = mono() + seconds
        paused = not allow_wake or self._rate_limited
        paused_until = self.clock() + seconds if paused else None
        while mono() < deadline:
            poll_at = min(deadline, mono() + FAST_POLL_S)
            while mono() < poll_at:
                if stop.is_set():
                    self.wake.clear()
                    self.drain_commands(paused_until)
                    return
                if self.wake.wait(max(0, poll_at - mono())):
                    self.wake.clear()
                    self.drain_commands(paused_until)
                    if stop.is_set() or not paused:
                        return
            if mono() >= deadline:
                return  # the full fetch is due; do not spend another query on the same data
            if self._rate_limited or mono() < self._next_fast_poll:
                continue
            reason = self.light_poll(timeout_s=min(FAST_POLL_S, deadline - mono()))
            self._next_fast_poll = mono() + self.fast_poll_interval
            if reason and not paused:
                self._wake_reason = reason
                self.observe("phase", phase="checking")
                return

    def _auth_failure(self, detail: str, fix_command: str | None):
        self._auth_paused = True
        message = f"engine login expired: {detail}; run: {fix_command or 'check engine authentication'}"
        self._auth_pause_reason = message
        self.condition("engine_identity", message, fix_command=fix_command or "")
        if not self._auth_notified:
            log(message)
            self.notifier(MARKER, message)
            self._auth_notified = True

    def _probe_engine(self, *, after_failure=False):
        engine = getattr(self.reviewer, "engine", None)
        if engine is None:  # legacy test doubles / callers without an engine
            return
        identity = engine.probe()
        self._engine_probed = True
        self.observe("engine_identity", identity=identity, name=engine.label.split("/", 1)[0],
                     profile=Path(self.settings.resolved_profile).name if self.settings.resolved_profile else "default")
        if identity.ok:
            if not after_failure and self._auth_paused:
                self._auth_notified = False
            self._auth_paused = False
            self._probe_warning = None
            self.condition("engine_identity", None)
        else:
            message = (f"engine probe failed: {identity.detail}; "
                       f"run: {identity.fix_command or 'check engine authentication'}")
            if message != self._probe_warning:
                log(message)
                self._probe_warning = message
            # A probe can be falsely negative while calls work. Only a call's AuthError pauses auth.
            if not self._auth_paused:
                self.condition("engine_identity", message, level="warning", fix_command=identity.fix_command or "")
        # Auth health never changes a quota/transport deadline.
        for key in ("engine_identity", "reviewer_paused"):
            self.observe("alert_attempt", key=key,
                         text="engine probe ok" if identity.ok else f"engine probe failed: {identity.detail}")
        return identity

    def _model_pause_reason(self) -> str | None:
        if self._tick_pause_reason:
            return self._tick_pause_reason
        if self._auth_paused:
            return self._auth_pause_reason
        if self.clock() < self.reviewer_paused_until:
            return self._reviewer_pause_reason or "reviewer paused after a failure"
        return None

    def _pause_reviewer(self, exc: ReviewerError) -> None:
        cause = exc.cause
        if isinstance(cause, AuthError):
            self._auth_failure(str(cause), cause.fix_command)
            message = self._auth_pause_reason
        else:
            now = self.clock()
            reset = cause.reset_at if isinstance(cause, QuotaError) else None
            deadline = reset if reset is not None and reset > now else now + REVIEWER_PAUSE_S
            self.reviewer_paused_until = max(self.reviewer_paused_until, deadline)
            detail = " ".join(str(exc).split()).removeprefix("claude reported an error: ")[:100]
            until = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self.reviewer_paused_until))
            message = (f"engine quota exhausted; resets at {until}: {detail}" if isinstance(cause, QuotaError)
                       else f"model failure, paused until {until}: {detail}")
            self._reviewer_pause_reason = message
            self.condition("reviewer_paused", message, seconds=max(0, self.reviewer_paused_until - self.clock()))
            if not self._reviewer_notified:
                log(message)
                self.notifier(MARKER, message)
                self._reviewer_notified = True
        # Even immediate probe recovery (or a deadline passing) must not retry another item this tick.
        if self._in_tick:
            self._tick_pause_reason = message
        self.observe("phase", phase="paused")
        self._probe_engine(after_failure=True)

    def _unknown_condition(self):
        count = sum(self._in_scope(r["key"][0]) for r in self.journal.unknown_records())
        self.condition("unknown_write", f"{count} unknown write(s); reconciled automatically when GitHub confirms; "
                       "otherwise left to you" if count else None)

    # ---------- pull requests
    def process_pr(self, repo: str, number: int) -> str:
        try:
            return self._process_pr(repo, number)
        finally:
            if self._reviewing_key is not None:
                self.commands.reviewing(self._reviewing_key, False)
                self._reviewing_key = None

    def _process_pr(self, repo: str, number: int) -> str:
        if not self._in_scope(repo):
            return "skip"
        pr = self.gh.pull_request(repo, number)
        key = (repo, number, pr.head_sha)
        if pr.state != "OPEN":
            if (repo, number) in self.journal.watched_prs():
                self._journal("pr", key, "closed", reason=f"pull request is {pr.state.lower()}", source="observed",
                              author=pr.author, title=pr.title)
            self._pr_statuses.pop((repo, number), None)
            return "closed"
        if self.journal.observation_changed("pr", key, pr.title, author=pr.author):
            self._journal("pr", key, "seen", reason="watching until closed", source="observed",
                          title=pr.title, author=pr.author)
        self._publish_pr_status(PRStatus.from_pr(pr))
        # Dismissal is a veto even after an approval settled this head. Report it without reopening it.
        if (not self.journal.was_notified(key, "dismissed")
                and any(r.author == self.settings.login and r.state == "DISMISSED" and r.commit_sha == pr.head_sha
                        and is_automated(r.body) for r in pr.reviews)):
            self._notify("pr", key, f"{repo}#{number}: your automated review was dismissed on this head; "
                                    "leaving it to you", tag="dismissed")
        if self.journal.is_unknown(key):
            log(f"{repo}#{number} @{pr.head_sha[:7]}: earlier write outcome unknown; leaving to a human")
            return "unknown"
        if self.journal.is_final(key, self.settings.judgement(repo).hash, self.mode(repo), pr.fingerprint):
            return "final"
        if self.journal.owner_takeover(key):
            self._journal("pr", key, "ignore", reason="taken over by you", protected=True,
                          source="eligibility", fingerprint=pr.fingerprint)
            return "ignore"
        decision = evaluate_pr(pr, self.settings)
        if decision.outcome == "review":
            decision = self._stored_decision(key, pr.fingerprint) or self._ask_about_pr(pr)
        if self.cancelled:
            return "skip"
        return self._settle_pr(key, pr, decision)

    def _ask_about_pr(self, pr: PullRequest) -> Decision:
        if self.reviewer is None:
            return Decision("ignore", "reviewer not configured", "eligibility")
        if reason := self._model_pause_reason():
            return Decision("defer", reason, "ai")
        try:
            failed_logs = self.gh.failed_check_logs(pr)
            self._reviewing_key = (pr.repo, pr.number, pr.head_sha)
            self.commands.reviewing(self._reviewing_key, True)
            self.observe("phase", phase="reviewing", kind="pr", key=self._reviewing_key)
            self.reviewer.rules = self.settings.prompt_rules(pr.repo)
            self.reviewer.remarks = self.settings.repo(pr.repo).remarks
            # "Currently reviewing" is enforced by not draining commands during a model call;
            # the inbox also refuses duplicates until the resulting review settles.
            try:
                decision = self.reviewer.review_pr(pr, failed_logs)
            finally:
                self.observe("phase", phase="checking")
            self._auth_notified = False
            self._reviewer_notified = False
            return decision
        except UnclearAnswer as exc:
            if self.cancelled:
                return Decision("defer", "stopping", "ai")
            self._probe_engine()
            return Decision("unclear", str(exc)[:300], "ai", data={"session": str(exc.session or "")},
                            notify=f"{pr.repo}#{pr.number}: model answer unclear, see {exc.session}")
        except ReviewerError as exc:
            if self.cancelled:
                return Decision("defer", "stopping", "ai")
            self._pause_reviewer(exc)
            return Decision("defer", f"reviewer failed: {exc}"[:200], "ai")

    def _settle_pr(self, key, pr: PullRequest, decision: Decision) -> str:
        fields = {"reason": decision.reason, "source": decision.source, "fingerprint": pr.fingerprint, **decision.data}
        previous = self.journal.settled_outcome(key)
        notified = self.journal.was_notified(key)

        def notify(outcome, message):
            if notified and previous == outcome:
                return
            warned = outcome not in ("approve", "satisfied", "defer", "would_approve") and self._warn_if_block_active(key, pr)
            if not warned:
                self._notify("pr", key, message)
        if decision.outcome in ("satisfied", "ignore", "nothing", "unclear"):
            if decision.outcome == "ignore" and decision.source == "eligibility" and decision.reason in (
                    "the owner reviewed this pull request themselves",
                    "the owner has an unsubmitted review open",
                    "automated review was dismissed on this head"):
                fields["protected"] = True
            self._journal("pr", key, decision.outcome, **fields)
            notify(decision.outcome, decision.notify)
            return decision.outcome
        if decision.outcome == "defer":
            self._defer("pr", key, decision.reason)
            since = self.journal.defer_since(key)
            if (decision.reason.startswith("checks still running") and since
                    and self.clock() - _parse_iso(since) > CI_WAIT_MAX_S and not self.journal.was_notified(key, "ci-wait")):
                self._notify("pr", key, f"{pr.repo}#{pr.number}: checks still running after "
                                        f"{CI_WAIT_MAX_S // 3600} h; look yourself", tag="ci-wait")
            return "defer"
        assert decision.outcome in ("approve", "request_changes", "comment"), decision
        action = decision.outcome
        self._journal("pr", key, "assessment", action=action, body=decision.body, notify=decision.notify,
                      summary=decision.summary, why=decision.why, watch=decision.watch,
                      findings=[asdict(f) for f in decision.findings],
                      data=decision.data, **fields)
        if boundary := self._boundary(pr.repo, action):
            return self._discard("pr", key, decision, boundary, pr.fingerprint)
        blocked = approve_floor(pr) if action == "approve" else None
        if blocked:                                           # the floor the model cannot be talked out of
            self._journal("pr", key, "nothing", reason=f"approve refused by the floor: {blocked}", source="eligibility",
                          fingerprint=pr.fingerprint)
            notify("nothing", f"{pr.repo}#{pr.number}: the model wanted to approve despite {blocked}; look yourself")
            return "nothing"
        try:
            result = self._write_pr(pr, decision)
        except GhError as exc:
            if not exc.definite or exc.rate_limited:
                raise                                         # a read failed or GitHub throttled us: retry next tick
            self._journal("pr", key, "error", action=action, reason=str(exc)[:300], source=decision.source,
                          fingerprint=pr.fingerprint)
            notify("error", f"{pr.repo}#{pr.number}: could not post {action}: {str(exc)[:120]}")
            return "error"
        rendered = result.get("rendered", {})
        if "defer" in result or "withheld" in result:
            return self._discard("pr", key, decision, result, pr.fingerprint)
        if "dry" in result:
            self._journal("pr", key, f"would_{action}", action=action, **rendered,
                          notify=decision.notify, **fields)
            if self.mode(pr.repo) != "live":
                notify(f"would_{action}", decision.notify)
            return f"would_{action}"
        if "satisfied" in result:
            self._journal("pr", key, "satisfied", reason=result["satisfied"], source="observed")
            return "satisfied"
        if "already" in result:                               # posted earlier (e.g. a crash before journaling)
            self._journal("pr", key, "applied", action=action, reason=result["already"], source="observed",
                          fingerprint=pr.fingerprint)
            notify(action, None)
            return "already"
        if "unknown" in result:
            self._journal("pr", key, "unknown", action=action, reason=result["unknown"], source=decision.source,
                          fingerprint=pr.fingerprint, **rendered)
            notify("unknown", None)
            return "unknown"
        self._journal("pr", key, "applied", action=action, notify=decision.notify,
                      **fields, **rendered, **result["applied"])
        notify(action, decision.notify)
        return "applied"

    def _warn_if_block_active(self, key, pr: PullRequest) -> bool:
        """An automated "changes requested" from an earlier head still blocks the merge; when the new head does
        not earn an approve, the owner must know, once per head."""
        latest = next((r for r in reversed(pr.reviews) if r.author == self.settings.login
                       and r.state in ("APPROVED", "CHANGES_REQUESTED")), None)
        blocking = latest is not None and latest.state == "CHANGES_REQUESTED" and is_automated(latest.body)
        if blocking and not self.journal.was_notified(key, "block-active"):
            self._notify("pr", key, f"{pr.repo}#{pr.number}: your automated 'changes requested' still blocks it "
                                    f"and the new head was not approved; look yourself", tag="block-active")
            return True
        return False

    def _write_pr(self, pr: PullRequest, decision: Decision) -> dict:
        action = decision.outcome
        dry = self.mode(pr.repo) != "live"
        if not dry:
            fresh = self.gh.pull_request(pr.repo, pr.number)
            self._publish_pr_status(PRStatus.from_pr(fresh))
            if fresh.head_sha != pr.head_sha:
                return {"defer": "head moved during assessment"}
            again = evaluate_pr(fresh, self.settings)
            if again.outcome == "satisfied":
                return {"satisfied": again.reason}
            if fresh.fingerprint != pr.fingerprint:
                return {"defer": "context changed during assessment (base, description, checks, reviews or files)"}
            if again.outcome != "review":
                return {"defer": f"state changed before write: {again.reason}"}
            wanted = _REVIEW_STATES[action]
            if any(r.author == self.settings.login and r.state == wanted and r.commit_sha == pr.head_sha
                   and is_automated(r.body) for r in fresh.reviews):
                return {"already": f"an automated {action} already exists on this head"}   # one per head and action
        self.observe("phase", phase="writing", kind="pr", key=(pr.repo, pr.number, pr.head_sha))
        if not dry and (reason := self._check_identity()):
            return {"defer": reason}
        boundary = self._boundary(pr.repo, action, writing=not dry)
        if boundary and "dry" not in boundary:
            return boundary
        rendered = self._render(pr.repo, pr.head_sha, decision)
        fields = {"body": rendered.body, "published_findings": rendered.published_findings,
                  "summary": decision.summary, "why": decision.why, "watch": decision.watch}
        if rendered.withheld_reason:
            return {"withheld": rendered.withheld_reason, "rendered": fields}
        self._save_rendered(decision, rendered.body)
        if dry or "dry" in boundary:
            return {"dry": True, "rendered": fields}
        try:
            review_id = self.gh.submit_review(fresh, action, rendered.body)
        except (KeyboardInterrupt, SystemExit):
            self._journal("pr", (pr.repo, pr.number, pr.head_sha), "unknown", action=action,
                          reason="interrupted during write", source=decision.source, fingerprint=pr.fingerprint, **fields)
            raise
        except Exception as exc:
            if isinstance(exc, GhError) and exc.definite:
                raise
            return {"unknown": str(exc), "rendered": fields}  # GitHub may have accepted it
        if review_id is None:
            return {"unknown": "missing review id after write", "rendered": fields}
        return {"applied": {"review_id": review_id}, "rendered": fields}

    def _render(self, repo, head, decision, *, deployment=False):
        answer = AnswerV2(decision.outcome, decision.summary, decision.findings, decision.reason, decision.notify,
                          why=decision.why, watch=decision.watch)
        engine = decision.data.get("engine") or ClaudeEngine(self.settings.agent.model).label
        comment = replace(self.settings.comment, disclose=False) if deployment else self.settings.comment
        return render_body(answer, "{summary}" if deployment else self.settings.template,
                           comment, self.settings.repo(repo), head_sha=head,
                           judgement=self.settings.judgement(repo).hash, engine_label=engine, login=self.settings.login)

    @staticmethod
    def _save_rendered(decision, body):
        if session := decision.data.get("session"):
            (Path(session) / "rendered.md").write_text(body, encoding="utf-8")

    # ---------- deployments (approve | ignore)
    def process_run(self, repo: str, run_id: int) -> str:
        if not self._deployments(repo):
            return "skip"
        pending = [p for p in self.gh.pending_deployments(repo, run_id) if p.can_approve]
        for key in self.journal.pending_gates():
            if key[:2] == (repo, run_id) and not any(p.env_id == key[3] for p in pending):
                self._journal("deployment", key, "gate_gone", reason="environment no longer pending", source="observed")
        if not pending:
            return "skip"                                    # nothing for this account to approve (yet)
        run = self.gh.run_by_id(repo, run_id)
        outcomes = []
        for env in pending:
            self.drain_commands()
            if self.cancelled:
                break
            outcomes.append(self._process_environment(run, env))
        acted = [o for o in outcomes if o != "skip"]
        return acted[0] if acted else "skip"

    def _process_environment(self, run: WorkflowRun, env) -> str:
        key = (run.repo, run.run_id, run.attempt, env.env_id)
        for old in self.journal.pending_gates():
            if old[:2] == key[:2] and old[3] == key[3] and old[2] != key[2]:
                self._journal("deployment", old, "gate_gone", reason="new run attempt", source="observed")
        if self.journal.observation_changed("deployment", key, run.workflow_file, env.name):
            self._journal("deployment", key, "seen", title=run.workflow_file, environment=env.name, source="observed")
        self.observe("phase", phase="checking", kind="deployment", key=key)
        if self.journal.is_unknown(key):
            log(f"{run.repo} run {run.run_id}/{run.attempt} {env.name}: earlier write outcome unknown; leaving to a human")
            return "unknown"
        jobs = self.gh.jobs(run.repo, run.run_id, run.attempt)
        fingerprint = hashlib.sha256(json.dumps([run.attempt, run.head_sha, [[j.job_id, j.status, j.conclusion] for j in jobs]]).encode()).hexdigest()[:16]
        if self.journal.is_final(key, self.settings.judgement(run.repo).hash, self.mode(run.repo), fingerprint):
            return "final"
        data = {"environment_id": env.env_id, "environment": env.name}
        running = [j.name for j in jobs if j.status in RUNNING_JOB_STATES]
        if running:                                          # the model must not judge a half-finished run
            return self._settle_deploy(key, Decision("defer", f"jobs still running: {', '.join(running[:3])}",
                                                     "readiness", data=data), None, fingerprint)
        stored = self._stored_decision(key, fingerprint)
        if stored:
            return self._settle_deploy(key, stored, lambda: self._write_gate(run, stored), fingerprint)
        logs = {j.job_id: self._job_log(run.repo, j.job_id) for j in jobs if j.status == "completed"}
        branch = self._branch(run.repo)
        workflow_text = self.gh.file_at(run.repo, f".github/workflows/{run.workflow_file}", run.head_sha)
        if self.reviewer is not None:
            self.reviewer.rules = self.settings.prompt_rules(run.repo)
        verdict = self._ask_about_deployment(
            key, lambda: self.reviewer.review_deployment(run, env.name, jobs, logs, branch, workflow_text))
        decision = replace(verdict, data={**verdict.data, **data})
        if self.cancelled:
            return "skip"
        return self._settle_deploy(key, decision, lambda: self._write_gate(run, decision), fingerprint)

    def _ask_about_deployment(self, key, ask) -> Decision:
        if self.reviewer is None:
            return Decision("ignore", "reviewer not configured", "eligibility")
        if reason := self._model_pause_reason():
            return Decision("defer", reason, "ai")
        try:
            self.observe("phase", phase="reviewing", kind="deployment", key=key)
            decision = ask()
            self._auth_notified = False
            self._reviewer_notified = False
            return decision
        except UnclearAnswer as exc:
            if self.cancelled:
                return Decision("defer", "stopping", "ai")
            self._probe_engine()
            return Decision("unclear", str(exc)[:300], "ai", data={"session": str(exc.session or "")},
                            notify=f"deployment: model answer unclear, see {exc.session}")
        except ReviewerError as exc:
            if self.cancelled:
                return Decision("defer", "stopping", "ai")
            self._pause_reviewer(exc)
            return Decision("defer", f"reviewer failed: {exc}"[:200], "ai")

    def _job_log(self, repo: str, job_id: int) -> str:
        try:
            return self.gh.job_log(repo, job_id)
        except GhError as exc:
            if exc.status == 404:                              # skipped job or expired log
                return "(log not available)"
            raise

    def _branch(self, repo: str) -> str:
        if repo not in self._default_branch:
            self._default_branch[repo] = self.gh.default_branch(repo)
        return self._default_branch[repo]

    def _write_gate(self, run: WorkflowRun, decision: Decision) -> dict:
        dry = self.mode(run.repo) != "live"
        env_id = decision.data["environment_id"]
        key = (run.repo, run.run_id, run.attempt, env_id)
        if not dry:
            current = self.gh.run_by_id(run.repo, run.run_id)
            if current.attempt != run.attempt or current.head_sha != run.head_sha:
                return {"defer": "run was re-run or changed during assessment"}
            pending = self.gh.pending_deployments(run.repo, run.run_id)
            env = next((p for p in pending if p.env_id == decision.data["environment_id"] and p.can_approve), None)
            if env is None:
                return {"defer": "environment no longer pending or approvable"}
        self.observe("phase", phase="writing", kind="deployment", key=key)
        if not dry and (reason := self._check_identity()):
            return {"defer": reason}
        boundary = self._boundary(run.repo, "approve", deployment=True, writing=not dry)
        if boundary and "dry" not in boundary:
            return boundary
        rendered = self._render(run.repo, run.head_sha, decision, deployment=True)
        fields = {"body": rendered.body, "summary": decision.summary, "published_findings": 0}
        self._save_rendered(decision, rendered.body)
        if dry or "dry" in boundary:
            return {"dry": True, "rendered": fields}
        try:
            result = self.gh.approve_deployment(run.repo, run.run_id, env_id, rendered.body)
            if result is not None:
                json.loads(result)  # A successful transport can still leave the response undecodable.
        except (KeyboardInterrupt, SystemExit):
            self._journal("deployment", key, "unknown", reason="interrupted during write", source=decision.source,
                          **decision.data, **fields)
            raise
        except Exception as exc:
            if isinstance(exc, GhError) and exc.definite:
                raise
            return {"unknown": str(exc), "rendered": fields}
        if result is None:
            return {"unknown": "missing deployment response after write", "rendered": fields}
        return {"applied": {"environment_id": env_id}, "rendered": fields}

    def _settle_deploy(self, key, decision: Decision, write, fingerprint: str) -> str:
        kind = "deployment"
        if decision.outcome in ("satisfied", "ignore", "unclear"):
            self._journal(kind, key, decision.outcome, reason=decision.reason, source=decision.source,
                          fingerprint=fingerprint, **decision.data)
            self._notify(kind, key, decision.notify)
            return decision.outcome
        if decision.outcome == "defer":
            self._defer(kind, key, decision.reason)
            return "defer"
        assert decision.outcome == "approve", decision
        self._journal(kind, key, "assessment", action="approve", reason=decision.reason, source=decision.source,
                      fingerprint=fingerprint, body=decision.body, summary=decision.summary,
                      notify=decision.notify, data=decision.data)
        if boundary := self._boundary(key[0], "approve", deployment=True):
            return self._discard(kind, key, decision, boundary, fingerprint)
        try:
            result = write()
        except GhError as exc:
            if not exc.definite or exc.rate_limited:
                raise
            self._journal(kind, key, "error", reason=str(exc)[:300], source=decision.source, fingerprint=fingerprint)
            self._notify(kind, key, f"deployment {key_str(key)}: could not approve: {str(exc)[:120]}")
            return "error"
        rendered = result.get("rendered", {})
        if "defer" in result or "withheld" in result:
            return self._discard(kind, key, decision, result, fingerprint)
        if "dry" in result:
            self._journal(kind, key, "would_approve", reason=decision.reason, source=decision.source,
                          fingerprint=fingerprint, **rendered)
            return "would_approve"
        if "satisfied" in result:
            self._journal(kind, key, "satisfied", reason=result["satisfied"], source="observed")
            return "satisfied"
        if "unknown" in result:
            self._journal(kind, key, "unknown", reason=result["unknown"], source=decision.source, **decision.data, **rendered)
            return "unknown"
        self._journal(kind, key, "applied", reason=decision.reason, source=decision.source, **rendered, **result["applied"])
        return "applied"

    # ---------- shared
    def _journal(self, kind: str, key, event: str, **fields) -> None:
        url = item_url(kind, key, fields)
        self.journal.append(kind=kind, key=key, event=event, mode="dry-run" if event.startswith("would_") else self.mode(key[0]),
                            policy=self.settings.judgement(key[0]).hash,
                            components=self.settings.judgement(key[0]).components, url=url, **fields)
        if kind == "pr" and (event in ("nothing", "ignore", "unclear", "error", "applied", "satisfied", "owner_approve", "unknown", "closed")
                             or event.startswith("would_")):
            self.commands.release(key)
        if event in ("unknown", "applied", "satisfied", "owner_approve", "owner_attempt", "owner_error"):
            self._unknown_condition()
        word = EVENT_WORDS.get(event, event)
        if event.startswith("would_"):
            word = f"would post {event[6:]}"
        elif event in ("applied", "assessment", "error", "unknown") and fields.get("action"):
            word = f"{word} {fields['action']}"
        text = (fields.get("summary", "") if event == "applied" or event.startswith("would_")
                else fields.get("text") or fields.get("reason") or "")
        log(f"{item_label(kind, key):<42} {word:<26} {text[:160]}\n{'':>51}{url}")

    def _defer(self, kind: str, key, reason: str) -> None:
        if self.journal.defer_reason(key) != reason:
            self._journal(kind, key, "defer", reason=reason)

    def _notify(self, kind: str, key, text: str | None, tag: str = "") -> None:
        """Tell the owner: one journal record (the durable list behind --status) plus one macOS notification."""
        if not text:
            return
        if self.mode(key[0]) != "live":
            text = f"[{self.mode(key[0])}] {text}"
        self._journal(kind, key, "notify", text=text, tag=tag, reason=text)
        self.notifier(MARKER, text)

    def reconcile_unknown(self) -> int:
        """Resolve earlier writes whose outcome was unknown by looking at what GitHub actually recorded."""
        resolved = 0
        for rec in self.journal.unknown_records():
            kind, key = rec["kind"], tuple(rec["key"])
            if (not self._in_scope(key[0]) or (kind == "deployment" and not self._deployments(key[0]))
                    or (kind, key[0], key[1]) in self.quarantined):
                continue
            try:
                if kind == "pr":
                    repo, number, head = key
                    action = rec.get("action", "approve")
                    wanted = _REVIEW_STATES[action]
                    pr = self.gh.pull_request(repo, number)
                    self._publish_pr_status(PRStatus.from_pr(pr))
                    if rec.get("origin") == "owner":
                        manual = [r for r in pr.reviews if r.author == self.settings.login
                                  and r.state == "APPROVED" and r.commit_sha == head and not is_automated(r.body)]
                        digest = rec.get("body_digest") or hashlib.sha256(rec.get("body", "").encode()).hexdigest()
                        match = next((r for r in reversed(manual)
                                      if hashlib.sha256(r.body.encode()).hexdigest() == digest), None)
                        observed = match or (manual[-1] if manual else None)
                        if observed:
                            self._journal(kind, key, "owner_approve" if match else "satisfied", action="approve",
                                          origin="owner", source="observed", body=observed.body,
                                          review_id=observed.review_id, reviewed_at=observed.submitted_at,
                                          reason="approved by you" if match else "approved by you on GitHub")
                            resolved += 1
                        else:
                            log(f"{kind} {key_str(key)}: earlier write outcome still unknown; leaving to a human")
                        continue
                    ok = any(r.author == self.settings.login and r.state == wanted and r.commit_sha == head
                             and (action == "approve" or is_automated(r.body)) for r in pr.reviews)
                else:
                    repo, run_id, attempt, env_id = key
                    if self.gh.run_by_id(repo, run_id).attempt != attempt:
                        log(f"deployment {key_str(key)}: run was re-run since the unknown write; leaving to a human")
                        continue
                    ok = any(login == self.settings.login and state == "approved" and env_id in env_ids
                             for login, state, env_ids in self.gh.run_approvals(repo, run_id))
            except Exception as exc:                           # noqa: BLE001 - never let one record stop the tick
                log(f"reconcile {kind} {key_str(key)}: could not check ({type(exc).__name__}: {str(exc)[:200]})")
                continue
            if ok and kind == "pr" and rec.get("action", "approve") != "approve":
                self._journal(kind, key, "applied", action=rec["action"], fingerprint=rec.get("fingerprint"),
                              reason="write found on GitHub after an unknown outcome", source="observed")
                resolved += 1
            elif ok:
                self._journal(kind, key, "satisfied", reason="write found on GitHub after an unknown outcome",
                              source="observed")
                resolved += 1
            else:
                log(f"{kind} {key_str(key)}: earlier write outcome still unknown; leaving to a human")
        return resolved

    def _guard(self, item: tuple[str, str, int], fn, count) -> str:
        """Run one item; an item without access is quarantined, any other failure is logged and counted so the
        remaining items still get processed this tick. Returns the outcome ("error" on failure)."""
        if item in self.quarantined:
            return "skip"
        kind, repo, number = item
        label = f"{repo} {kind} {number}"
        try:
            outcome = fn()
        except GhError as exc:
            if exc.no_access:
                self.quarantined.add(item)
                log(f"{label}: no access ({exc}); quarantined for this session")
                self._quarantine_conditions()
                count("quarantined")
                return "quarantined"
            self._rate_limited |= exc.rate_limited
            log(f"{label}: error, will retry next tick ({exc})")
            count("error")
            return "error"
        except GhTimeout as exc:
            log(f"{label}: timeout, will retry next tick ({exc})")
            count("error")
            return "error"
        except Exception as exc:                               # noqa: BLE001 - one bad item must not stop the loop
            log(f"{label}: unexpected {type(exc).__name__}, will retry next tick ({str(exc)[:200]})")
            count("error")
            return "error"
        if outcome != "skip":
            count(outcome)
        return outcome

    def _in_scope(self, repo: str) -> bool:
        return repo in self.settings.repos and self.settings.repo(repo).enabled

    def _quarantine_conditions(self):
        for repo in sorted(self.settings.repos.keys() | {repo for _, repo, _ in self.quarantined}):
            items = sorted(f"{kind} #{number}" for kind, name, number in self.quarantined if name == repo)
            self.condition(f"quarantined:{repo}", f"{repo}: no access; quarantined {', '.join(items)}"
                           if items and self._in_scope(repo) else None, repo=repo, scope=repo)

    def _deployments(self, repo: str) -> bool:
        return self._in_scope(repo) and self.settings.repo(repo).deployments

    def mode(self, repo: str) -> str:
        return "dry-run" if self.global_mode == "dry-run" else self.settings.repo(repo).post

    def _stored_decision(self, key, fingerprint) -> Decision | None:
        rec = self.journal.assessment(key, self.settings.judgement(key[0]).hash, fingerprint)
        if rec:
            return Decision(rec["action"], rec["reason"], rec["source"], body=rec.get("body", ""),
                            notify=rec.get("notify"), data=rec.get("data", {}), summary=rec.get("summary", ""),
                            findings=tuple(Finding.from_record(f) for f in rec.get("findings", ())),
                            why=rec.get("why", ""), watch=rec.get("watch", ""))
        return None

    def _boundary(self, repo, action, *, deployment=False, writing=False) -> dict:
        if self._writes_blocked:
            return {"defer": self._writes_blocked}
        if self.cancelled:
            return {"defer": "stopping"}
        try:
            fresh = load_settings(self.settings.config_dir)
            if fresh.login != self.settings.login:
                raise SettingsError(fresh.config_dir / "settings.toml", "identity.login", "login changed; restart the tool")
        except SettingsError as exc:
            self.condition("settings_error", str(exc))
            return {"defer": "settings invalid, not writing"}
        self.condition("settings_error", None)
        if repo not in fresh.repos or not fresh.repo(repo).enabled:
            return {"defer": "repository disabled"}
        if deployment and not fresh.repo(repo).deployments:
            return {"defer": "deployments disabled"}
        before, after = self.settings.judgement(repo), fresh.judgement(repo)
        if before.hash != after.hash:
            return {"defer": "stale assessment: " + changed_components(before.components, after.components, repo)}
        if action not in fresh.repo(repo).allowed:
            return {"withheld": f"{action} disabled"}
        # Presentation may change without invalidating the assessment; render from this boundary's settings.
        self.settings = replace(self.settings, template=fresh.template, comment=fresh.comment)
        if writing:
            if self.global_mode == "dry-run" or fresh.repo(repo).post == "dry-run":
                return {"dry": True}
            if not self.live_lock_held:
                return {"defer": "live lock not held; restart to enable live mode"}
        return {}

    def _discard(self, kind, key, decision, result, fingerprint):
        if "withheld" in result:
            reason = result["withheld"]
            self._journal(kind, key, "withheld", action=decision.outcome, reason=reason,
                          **result.get("rendered", {"body": decision.body, "summary": decision.summary,
                                                    "why": decision.why, "watch": decision.watch}),
                          private_reason=decision.reason, notify=decision.notify,
                          fingerprint=fingerprint, source=decision.source)
            return "withheld"
        self._defer(kind, key, result["defer"])
        return "defer"

    def refresh_settings(self, settings: Settings) -> None:
        """Adopt validated settings between ticks; authority never changes judgement identity."""
        if settings.login != self.settings.login:
            raise SettingsError(settings.config_dir / "settings.toml", "identity.login", "login changed; restart the tool")
        if can_be_live(settings, self.global_mode) and not self.live_lock_held:
            log("settings reload refused dry-run -> live: live lock not held; restart to enable live mode")
            self.global_mode = "dry-run"
        if settings.repos != self.settings.repos:
            self.seen_runs.clear()
        for repo in settings.repos:
            before = (self.settings.judgement(repo).components if repo in self.settings.repos
                      else self.journal.components.get(repo))
            # On first start the journal can describe a judgement from a previous process.
            if before == settings.judgement(repo).components:
                before = self.journal.components.get(repo, before)
            text = changed_components(before, settings.judgement(repo).components, repo) if before else ""
            if text and self._reported_components.get(repo) != settings.judgement(repo).components:
                log(f"{repo}: {text}; settled outcomes will be re-evaluated")
                self.observe("policy", repo=repo, text=text)
                self.seen_runs.clear()
            self._reported_components[repo] = settings.judgement(repo).components
        self.settings = settings
        self.observe("policy", login=settings.login, mode="live" if can_be_live(settings, self.global_mode) else "dry-run",
                     model=settings.agent.model, effort=settings.agent.effort, config_dir=str(settings.config_dir),
                     live_lock_held=self.live_lock_held, state_dir=str(self.journal.path.parent),
                     repos=tuple(name for name in settings.repos if self._in_scope(name)),
                     configured_repo_count=len(settings.repos), idle=settings.screen.idle, tempo=settings.screen.tempo,
                     clock=settings.screen.clock, moon=settings.screen.moon, events=settings.screen.events, updates=settings.screen.updates,
                     approve_repos=tuple(name for name, repo in settings.repos.items() if repo.enabled and "approve" in repo.allowed),
                     dry_repos=tuple(name for name in settings.repos if self.mode(name) == "dry-run"))
        self.condition("no_repositories", f"no repositories configured; edit {settings.config_dir}/settings.toml"
                       if not settings.repos else None, level="warning")
        self._unknown_condition()
        self._quarantine_conditions()
        if self.reviewer is not None and hasattr(self.reviewer, "rules"):
            self.reviewer.rules = settings.rules
            self.reviewer.timeout_s = settings.agent.timeout_s
            engine = getattr(self.reviewer, "engine", None)
            if isinstance(engine, ClaudeEngine):
                profile_changed = engine.profile != settings.resolved_profile
                engine.model, engine.effort, engine.profile = settings.agent.model, settings.agent.effort, settings.resolved_profile
                if profile_changed:
                    self._auth_notified = False
                    self._probe_warning = None
                    self._engine_probed = False
                    self.observe("engine_identity", identity=None, name="claude",
                                 profile=Path(engine.profile).name if engine.profile else "default")

    def check_updates(self, *, force=False):
        if not self.settings.repos or not self._identity_checked:
            return "update check unavailable; configure repositories and verify gh identity first"
        if force:
            # A menu check may precede the next full tick after a form or editor save.
            try:
                screen = load_settings(self.settings.config_dir, include_text=False).screen
            except SettingsError as exc:
                logging.getLogger("lightkeeper").debug("update check failed: %s", exc)
                return "update check unavailable; settings invalid"
            self.settings = replace(self.settings, screen=screen)
            self.observe("policy", **asdict(screen))
        message = self.updates.check(enabled=self.settings.screen.updates, force=force)
        version = self.updates.available
        self.observe("policy", update_version=version)
        if version and version not in self._announced_updates:
            self._announced_updates.add(version)
            log(f"update {version} available: brew upgrade lightkeeper or pipx upgrade lightkeeper")
        return message

    def tick(self) -> dict[str, int]:
        self._tick_pause_reason = None
        started = time.monotonic()
        self._rate_limited = False
        self.observe("phase", phase="checking", progress=(0, 0))
        if self.settings.repos and (not self._engine_probed or self._auth_paused):
            self._probe_engine()
        if self.clock() >= self.reviewer_paused_until:
            self.condition("reviewer_paused", None)
        try:
            if self.settings.login and self._check_identity():
                # No login confirmation means no writes; skip this tick and retry in a minute.
                self.observe("tick_done", ok=False, seconds=time.monotonic() - started)
                self._wake_reason = ""
                return {}
            self.check_updates()
            self._in_tick = True
            counts = self._tick()
        except Exception as exc:
            self._tick_failures += 1
            self._error_ticks += 1
            self._rate_limited = isinstance(exc, GhError) and exc.rate_limited
            self.condition("tick_failed", f"tick failed: {exc}" if self._tick_failures >= 2 or self._rate_limited else None)
            self.observe("tick_done", ok=False, seconds=time.monotonic() - started)
            self._wake_reason = ""
            raise
        finally:
            self._in_tick = False
            self._tick_pause_reason = None
        self._tick_failures = 0                  # one broken item must not slow every other review down
        self._error_ticks = self._error_ticks + 1 if counts.get("error") else 0
        self.condition("tick_failed", "GitHub rate limited" if self._rate_limited else
                       f"errors in {self._error_ticks} ticks in a row; see run.log" if self._error_ticks >= 2 else None)
        self._unknown_condition()
        self.observe("tick_done", ok=not bool(counts.get("error")), seconds=time.monotonic() - started)
        self._wake_reason = ""
        return counts

    def _tick(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        self._default_branch = {}

        def count(outcome: str) -> None:
            counts[outcome] = counts.get(outcome, 0) + 1

        if self.reconcile_unknown():
            count("reconciled")
        try:
            repos = tuple(r for r in self.settings.repos if self._in_scope(r))
            found, saturated = self.gh.search_review_requests(self.settings.login, repos) if repos else ([], False)
            self.condition("search_saturated", "search hit its result limit; requests may be missed" if saturated else None)
            if saturated:
                log("warning: search hit its result limit; some review requests may be missed this tick")
        except (GhError, GhTimeout) as exc:
            self._rate_limited |= isinstance(exc, GhError) and exc.rate_limited
            log(f"search: error, watched pull requests still run ({exc})")
            count("error")
            found = []
        todo_prs = sorted(set(found) | set(self.journal.watched_prs()))
        todo_prs = [(repo, number) for repo, number in todo_prs if self._in_scope(repo)]
        for index, (repo, number) in enumerate(todo_prs, 1):
            self.drain_commands()
            if self.cancelled:
                return counts
            if self._in_scope(repo):
                self.observe("phase", phase="checking", kind="pr", key=(repo, number), progress=(index, len(todo_prs)))
                self._guard(("pr", repo, number), lambda repo=repo, number=number: self.process_pr(repo, number), count)
        if self.cancelled:
            return counts
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock() - NOTIFICATION_WINDOW_S))
        try:
            requests = self.gh.approval_requests(since) if any(self._deployments(r) for r in self.settings.repos) else []
        except (GhError, GhTimeout) as exc:
            self._rate_limited |= isinstance(exc, GhError) and exc.rate_limited
            log(f"notifications: error, will retry next tick ({exc})")
            count("error")
            requests = []
        todo: dict[tuple[str, int], str | None] = {}
        for repo, run_id, updated_at in requests:
            if self._deployments(repo) and self.seen_runs.get((repo, run_id)) != updated_at:
                todo[(repo, run_id)] = updated_at
        for key in self.journal.pending_gates():    # pending gates outlive the notification window
            repo, run_id = key[0], key[1]
            if self._deployments(repo) and (repo, run_id) not in todo:
                todo[(repo, run_id)] = self.seen_runs.get((repo, run_id))
        for index, ((repo, run_id), updated_at) in enumerate(todo.items(), len(todo_prs) + 1):
            self.drain_commands()
            if self.cancelled:
                return counts
            self.observe("phase", phase="checking", kind="deployment", key=(repo, run_id),
                         progress=(index, len(todo_prs) + len(todo)))
            outcome = self._guard(("deployment", repo, run_id),
                                  lambda repo=repo, run_id=run_id: self.process_run(repo, run_id), count)
            if outcome in ("applied", "ignore", "unclear", "satisfied", "would_approve", "final", "unknown") and updated_at:
                self.seen_runs[(repo, run_id)] = updated_at    # only a settled run is remembered
        return counts


def _status(journal: Journal) -> int:
    journal.replay(read_only=True)
    print(json.dumps(journal.summary(), indent=2, ensure_ascii=False))
    return 0


def live_screen_class(plain=False, stdout=None):
    stdout = sys.stdout if stdout is None else stdout
    if plain or not stdout.isatty() or os.environ.get("TERM") == "dumb":
        return None
    try:
        from .ui.textual_app import TextualApp
    except ImportError:
        print("warning: Textual unavailable; using plain mode", file=sys.stderr)
        return None
    return TextualApp


def _terminate(signum, frame):
    raise SystemExit(128 + signum)


def _run_textual(args, settings, runner, observer, view):
    from .worker import RunnerThread

    def run():
        if settings.repos and not runner.cancelled:
            runner._probe_engine()
        return _loop(args, settings, runner, runner.stop)

    worker = RunnerThread(runner, observer, run)
    app = view(observer, worker)
    # Signals only request shutdown. Repeated interrupts cannot release the locks
    # while the runner is still journaling a write.
    def stop(signum, frame):
        worker.request_stop()

    prior_int = signal.signal(signal.SIGINT, stop)
    prior_term = signal.signal(signal.SIGTERM, stop)
    worker.thread.start()
    try:
        app.run()
    finally:
        try:
            worker.join()
            app.print_summary()
        finally:
            signal.signal(signal.SIGINT, prior_int)
            signal.signal(signal.SIGTERM, prior_term)
    return worker.exit_code or app.return_code or 0


def _loop(args, settings, runner, stop):
    runner.stop = stop
    while not stop.is_set():
        runner.drain_commands()
        try:
            runner.refresh_settings(load_settings(settings.config_dir))
            runner.condition("settings_error", None)
        except SettingsError as exc:
            log(f"settings reload failed; keeping last good settings: {exc}")
            runner.condition("settings_error", f"settings reload failed: {exc}")
        try:
            counts = runner.tick()
            summary = ", ".join(f"{n} {EVENT_WORDS.get(k, k)}" for k, n in sorted(counts.items()))
            log(f"tick: {summary or 'nothing to do'}")
        except (GhError, GhTimeout) as exc:
            log(f"tick failed ({exc}); consecutive failures: {runner._tick_failures}")
        if args.once:
            return 0
        failures = runner._tick_failures
        seconds = min(POLL_INTERVAL_S * 2 ** min(failures, 4), MAX_BACKOFF_S)
        runner.sleeping(seconds, "backoff" if failures else "sleeping")
        runner.wait(stop, seconds, allow_wake=not (failures or runner._error_ticks))
    return 0


def main(argv=None) -> int:
    os.umask(0o077)
    configure_output()
    if sys.version_info < (3, 11):
        print("lightkeeper needs Python 3.11+ (tomllib)", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog="lightkeeper",
                                 description="Review GitHub pull requests and approve deployment gates by your rules.")
    from . import __version__
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--init", action="store_true", help="create missing configuration files from packaged templates and exit")
    ap.add_argument("--config-dir", help="default: $LIGHTKEEPER_CONFIG or ~/.config/lightkeeper")
    ap.add_argument("--policy", help="explicit legacy migration source; unfinished migrations resume their recorded source")
    ap.add_argument("--check-repo", metavar="org/name", help="check repository access and exit")
    ap.add_argument("--once", action="store_true", help="one pass, forced dry-run, then exit")
    ap.add_argument("--dry-run", action="store_true", help="loop, but never write to GitHub")
    ap.add_argument("--allow-live", action="store_true",
                    help="allow repository live mode and notifications with a non-default state directory")
    ap.add_argument("--status", action="store_true", help="print the journal summary and exit")
    ap.add_argument("--plain", action="store_true", help="print transition lines instead of the live terminal screen")
    ap.add_argument("--preview-idle", action="store_true",
                    help="show the idle scene full screen, without GitHub or the journal, and exit")
    ap.add_argument("--state-dir", default=None, help="default: ~/.local/state/lightkeeper/<login>")
    args = ap.parse_args(argv)
    if args.init:
        from .settings import init_config
        try:
            for path, created in init_config(args.config_dir):
                print(f"{path}: {'created' if created else 'already existed'}")
            print("edit settings.toml (login, repository) and rules.md: the model reads rules.md verbatim; "
                  "the copy approves by default, see README \"Writing rules\" for the alternative")
            return 0
        except (SettingsError, OSError) as exc:
            print(f"settings error: {exc}", file=sys.stderr)
            return 2
    if args.preview_idle:
        if not sys.stdout.isatty():
            print("the preview needs an interactive terminal", file=sys.stderr)
            return 2
        try:
            from .ui.preview import run_preview
        except ImportError:
            print("install lightkeeper[ui] to use --preview-idle", file=sys.stderr)
            return 2
        return run_preview(ascii=os.environ.get("LIGHTKEEPER_ASCII") == "1")
    if args.check_repo:
        if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", args.check_repo):
            print("repository must be org/name", file=sys.stderr)
            return 2
        try:
            result = check_repo(Gh(), args.check_repo)
            print(f"{args.check_repo}: {result}")
            return 0 if result.startswith("access ok") else 2
        except (GhError, GhTimeout, ValueError, KeyError) as exc:
            print(f"{args.check_repo}: access check failed: {exc}", file=sys.stderr)
            return 2
    report = None
    try:
        if args.status:
            config = config_path(args.config_dir)
            if not config.is_dir():
                raise SettingsError(config, "directory", "configuration directory does not exist; run lightkeeper --init")
        else:
            config = prepare_config_dir(args.config_dir)
            manifest = config / "migration.json"
            incomplete = manifest.exists() and not json.loads(manifest.read_text(encoding="utf-8"))["done"]
            if (not (config / "settings.toml").exists() and args.policy is not None) or incomplete:
                report = migrate(Path(args.policy) if args.policy is not None else None, config)
            if not (config / "settings.toml").exists() and args.policy is None and Path("policy.toml").exists():
                print("found ./policy.toml; run with --policy policy.toml to migrate it", file=sys.stderr)
        settings = load_settings(config, include_text=not args.status)  # Status needs no rules.
    except (SettingsError, PolicyError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"settings error: {exc}", file=sys.stderr)
        return 2
    state_root = Path(os.environ.get("LIGHTKEEPER_STATE") or default_state_root())
    state_dir = (Path(args.state_dir) if args.state_dir else state_root / settings.login).expanduser().resolve()
    mode, mode_reason, notifications = effective_mode(settings, state_dir, once=args.once,
                                                     dry_run=args.dry_run, allow_live=args.allow_live)
    live = can_be_live(settings, mode)
    journal = Journal(state_dir / "journal.jsonl")
    if args.status:
        return _status(journal)
    prior_term = signal.signal(signal.SIGTERM, _terminate)
    try:
        with ExitStack() as locks:
            locks.enter_context(Lock(state_dir / "lock"))
            if live:
                locks.enter_context(Lock(live_lock_path(settings.login)))
            view = live_screen_class(args.plain)
            configure_logging(state_dir, plain=view is None)
            configure_diagnostics(state_dir)
            if report:
                log(str(report))
                if view is not None:
                    print(report, flush=True)
            observer = None
            if view:
                from .ui.model import Runtime
                from .ui.project import Observer, Projection
                now = time.monotonic()
                shown_log = str(state_dir / "run.log").replace(str(Path.home()), "~", 1)
                observer = Observer(Projection(log_path=shown_log),
                                    Runtime(phase_since=now, started_at=now, login=settings.login, mode="live" if live else "dry-run",
                                            model=settings.agent.model, effort=settings.agent.effort,
                                            config_dir=str(config), state_dir=str(state_dir),
                                            repos=tuple(name for name, repo in settings.repos.items() if repo.enabled),
                                            configured_repo_count=len(settings.repos), idle=settings.screen.idle, tempo=settings.screen.tempo,
                                            clock=settings.screen.clock, moon=settings.screen.moon, events=settings.screen.events, updates=settings.screen.updates,
                                            dry_repos=tuple(name for name, repo in settings.repos.items() if repo.post == "dry-run"),
                                            live_lock_held=live, notifications=notifications))
            records = observer.replay(journal) if observer else journal.replay()
            if result := rehash(settings, journal):
                if "legacy_hash" in result:
                    log(f"rehash {result['legacy_hash']} -> {result['judgements']}; saved {state_dir / 'rehash.json'}")
                if "frame_2" in result:
                    log(f"rehash frame 2 -> {result['frame_2']}")
            gh = Gh()
            if settings.login:
                login = gh.login()
                if login != settings.login:
                    print(f"gh is logged in as {login!r} but settings expects {settings.login!r}", file=sys.stderr)
                    return 2
            reviewer = ClaudeReviewer(settings.agent.model, settings.rules, settings.resolved_profile, settings.agent.effort,
                                      sessions_dir=state_dir / "sessions", timeout_s=settings.agent.timeout_s)
            log(f"lightkeeper  pid={os.getpid()}  mode={'live' if live else 'dry-run'}  login={settings.login}  "
                f"model={settings.agent.model}/{settings.agent.effort}  settings={config}")
            log(f"global mode ceiling: {mode} ({mode_reason}); notifications {'on' if notifications else 'off'}")
            log(f"repos: {', '.join(settings.repos)}")
            log(f"journal: {records} records in {journal.path}")
            log(f"sessions: {state_dir / 'sessions'}")
            runner = Runner(settings, gh, journal, reviewer, observer=observer, live_lock_held=live, global_mode=mode,
                            notifier=macos_notify if notifications else lambda *_: None)
            runner.refresh_settings(settings)
            if not settings.repos:
                log(f"no repositories configured; edit {config}/settings.toml")
            elif not view:
                runner._probe_engine()
            if view:
                return _run_textual(args, settings, runner, observer, view)
            return _loop(args, settings, runner, runner.stop)
    except SettingsError as exc:
        print(f"settings error: {exc}", file=sys.stderr)
        return 2
    except JournalError as exc:
        print(f"journal error: {exc}", file=sys.stderr)
        return 3
    except (GhError, GhTimeout) as exc:
        print(f"GitHub error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        log("stopped")
        return 0
    finally:
        signal.signal(signal.SIGTERM, prior_term)


if __name__ == "__main__":
    sys.exit(main())
