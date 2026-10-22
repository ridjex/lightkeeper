"""Immutable display data. Wall-clock timestamps on rows; monotonic deadlines in Runtime."""
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping
from ..engines.base import Identity
from ..models import CICounts, Finding
from ..commands import CommandResult


@dataclass(frozen=True)
class ItemRef:
    kind: str
    repo: str
    number: int
    head: str = ""
    title: str = ""
    environment: str = ""
    environment_id: int = 0
    author: str = ""

    @property
    def identity(self) -> tuple:
        return self.kind, self.repo, self.number, self.environment_id

    @property
    def url(self) -> str:
        route = "pull" if self.kind == "pr" else "actions/runs"
        return f"https://github.com/{self.repo}/{route}/{self.number}"


@dataclass(frozen=True)
class Runtime:
    phase: str = "checking"
    phase_since: float = 0
    current_item: ItemRef | None = None
    progress: tuple[int, int] = (0, 0)
    next_check_at: float | None = None
    backoff: float = 0
    last_check_at: float | None = None
    last_check_ok: bool | None = None
    last_check_seconds: float = 0
    reviewer_paused_until: float = 0
    policy_error: str | None = None
    search_saturated: str | None = None
    last_error: str | None = None
    started_at: float = 0
    login: str = ""
    gh_login: str | None = None
    gh_confirmed: bool | None = None  # None until the first lookup; False retains only the last known login.
    engine_identity: Identity | None = None
    engine_name: str = "claude"
    engine_profile: str = "default"
    mode: str = "dry-run"
    model: str = ""
    effort: str = ""
    policy_hash: str = ""
    config_dir: str = ""
    idle: str = "always"
    tempo: float = .8
    clock: bool = True
    moon: bool = True
    events: bool = True
    updates: bool = True
    update_version: str = ""
    screen_revision: int = 0
    dry_repos: tuple[str, ...] = ()
    repos: tuple[str, ...] = ()
    configured_repo_count: int | None = None
    approve_repos: tuple[str, ...] = ()
    alerts: tuple["Alert", ...] = ()
    phase_reason: str = ""
    last_command: CommandResult | None = None
    live_lock_held: bool = False
    notifications: bool = False
    state_dir: str = ""
    repo_checks: tuple[tuple[str, str], ...] = ()
    command_results: tuple[CommandResult, ...] = ()
    alert_attempts: tuple[tuple[str, float, str], ...] = ()


@dataclass(frozen=True)
class ItemRow:
    item: ItemRef
    state: str
    why: str
    since: float
    needs_you: str | None = None
    needs_you_since: float = 0   # when the notify arrived; the state's `since` may be hours older
    dry_run: bool = False
    repo_dry: bool = False
    findings: tuple = ()
    ci: CICounts | None = None
    is_draft: bool = False
    failed_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class HistoryRow:
    item: ItemRef
    state: str
    why: str
    at: float
    dry_run: bool = False
    repeats: int = 1
    new_rules: bool = False
    change_text: str = ""


@dataclass(frozen=True)
class Alert:
    key: str
    level: str
    text: str
    since: float
    repo: str = ""
    fix_command: str = ""
    scope: str = "all watched items"


@dataclass(frozen=True)
class Event:
    at: float
    state: str
    why: str = ""
    summary: str = ""
    published_findings: int = 0
    engine: str = ""
    private_reason: str = ""
    body: str = ""
    change_text: str = ""
    session: str = ""
    review_url: str = ""


@dataclass(frozen=True)
class DetailState:
    latest_post: Event | None = None
    latest_body: str = ""
    latest_session: str = ""
    latest_private_reason: str = ""
    latest_model_note: str = ""
    latest_outcome: str = ""
    latest_summary: str = ""
    latest_why: str = ""
    latest_watch: str = ""
    latest_verdict: str = ""
    approve_refusal: str = ""
    floor_stale: bool = True
    floor_counts: tuple = ()
    latest_findings: tuple[Finding, ...] = ()
    base_ref: str = ""
    requested_reviewers: tuple[str, ...] | None = None
    head_pushed_at: str = ""
    failed_check_links: tuple[tuple[str, str], ...] = ()
    optional_checks: tuple[str, ...] = ()
    ci: CICounts | None = None
    is_draft: bool = False


@dataclass(frozen=True)
class Stats:
    posted: tuple[tuple[str, int], ...] = ()
    dry_run: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class Screen:
    runtime: Runtime
    watching: tuple[ItemRow, ...] = ()
    history: tuple[HistoryRow, ...] = ()
    alerts: tuple[Alert, ...] = ()
    stats: Stats = Stats()
    wall_now: float = 0
    log_path: str = "run.log"
    mono_at: float | None = None
    events: Mapping[tuple, tuple[Event, ...]] = field(default_factory=lambda: MappingProxyType({}))
    unknown_items: tuple[ItemRef, ...] = ()
    details: Mapping[tuple, DetailState] = field(default_factory=lambda: MappingProxyType({}))

    def detail(self, key: tuple) -> tuple[Event, ...]:
        return self.events.get(tuple(key), ())
