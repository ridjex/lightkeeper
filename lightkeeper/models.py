"""Plain data types shared by the other modules. No I/O here."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, fields

MARKER = "lightkeeper"
_NAMES = "(?:lightkeeper|approval-delegate)"  # Reviews posted under the former name stay recognised.
PR_ACTIONS = ("approve", "request_changes", "comment", "nothing")
_JOB_URL = re.compile(r"/actions/runs/(\d+)/jobs?/(\d+)")
_MARKER_LINE = re.compile(rf"^{_NAMES} \(\S+\) · policy [0-9a-f]{{6}} · head [0-9a-f]{{7}}$", re.MULTILINE)
_MARKER_V2 = re.compile(rf"<!-- {_NAMES} v2 head=(?P<head>[0-9a-fA-F]{{7,40}}) "
                        rf"judgement=(?P<judgement>[0-9a-fA-F]{{6,64}}) engine=(?P<engine>\S+) -->")


def marker_line(model: str, policy_hash: str, head_sha: str) -> str:
    """Legacy v1 format, retained for compatibility fixtures and readers."""
    return f"{MARKER} ({model}) · policy {policy_hash[:6]} · head {head_sha[:7]}"


def is_automated(body: str) -> bool:
    """True only for a review the tool wrote: its marker line is the last line of the body. A human merely
    mentioning the tool's name does not match."""
    last = body.rstrip().rsplit("\n", 1)[-1]
    return bool(_MARKER_LINE.fullmatch(last) or _MARKER_V2.fullmatch(last))


def trace_fields(body: str) -> dict[str, str] | None:
    """Read a v2 trace at the same last non-empty, unquoted line as is_automated."""
    match = _MARKER_V2.fullmatch(body.rstrip().rsplit("\n", 1)[-1])
    return match.groupdict() if match else None


@dataclass(frozen=True)
class Finding:
    severity: str
    origin: str
    text: str
    path: str | None = None
    url: str | None = None

    @classmethod
    def from_record(cls, record: dict):
        """Journal readers tolerate fields introduced by later compatible writers."""
        return cls(**{field.name: record[field.name] for field in fields(cls) if field.name in record})


@dataclass(frozen=True)
class AnswerV2:
    action: str
    summary: str                         # public
    findings: tuple[Finding, ...]
    reason: str                          # private; never published
    notify: str | None = None
    why: str = ""
    watch: str = ""


@dataclass(frozen=True)
class ChangedFile:
    path: str
    previous_path: str | None
    status: str          # added | removed | modified | renamed | copied | changed | unchanged
    additions: int
    deletions: int
    patch: str | None    # None when GitHub omits it (binary or too large)

    @property
    def is_complete(self) -> bool:
        """True when the diff fully represents the change (a pure rename has no patch and is fine)."""
        if self.patch is not None:
            return True
        return self.status in ("renamed", "copied") and self.additions + self.deletions == 0


@dataclass(frozen=True)
class Check:
    name: str
    status: str       # "completed" or a pending state (queued, in_progress, pending)
    conclusion: str   # "success", "failure", ... or "" while not completed
    details_url: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "completed" and self.conclusion in ("failure", "error", "timed_out", "cancelled",
                                                                  "action_required", "stale", "startup_failure")

    @property
    def job_id(self) -> int | None:
        """The Actions job behind this check, when the details link points at one."""
        m = _JOB_URL.search(self.details_url)
        return int(m.group(2)) if m else None


@dataclass(frozen=True)
class CICounts:
    """Display groups only; deliberately independent of review eligibility (notably stale)."""
    total: int = 0
    passed: int = 0
    running: int = 0
    failed: int = 0
    other: int = 0

    @classmethod
    def from_states(cls, counts: tuple[tuple[str, int], ...], total: int):
        passed = running = failed = 0
        for state, count in counts:
            if state == "success":
                passed += count
            elif state in ("queued", "in_progress", "pending", "requested", "waiting", "expected"):
                running += count
            elif state in ("failure", "error", "timed_out", "cancelled", "action_required", "startup_failure"):
                failed += count
        return cls(total, passed, running, failed, total - passed - running - failed)


@dataclass(frozen=True)
class Comment:
    author: str
    body: str


@dataclass(frozen=True)
class Review:
    author: str
    state: str        # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED | PENDING
    commit_sha: str
    body: str = ""
    review_id: int | None = None
    submitted_at: str = ""


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    head_sha: str
    base_ref: str
    author: str
    is_draft: bool
    state: str        # OPEN | CLOSED | MERGED
    title: str
    body: str
    requested_users: tuple[str, ...]
    requested_teams: tuple[str, ...]   # "org/slug"
    reviews: tuple[Review, ...]
    checks: tuple[Check, ...]
    files: tuple[ChangedFile, ...]
    author_is_bot: bool = False
    changed_files: int | None = None   # GitHub's own count; the files API caps its listing at 3000
    comments: tuple[Comment, ...] = ()  # issue comments, oldest first; context only, not part of the fingerprint

    @property
    def fingerprint(self) -> str:
        """Identity of everything an assessment looked at. A base retarget changes the diff without moving the
        head SHA, so the head alone is not enough to know that a write still targets what was assessed.
        Other people's reviews count (a teammate's "changes requested" must invalidate an approval in flight);
        the tool's own reviews and issue comments do not (bots edit comments constantly)."""
        payload = [self.base_ref, self.head_sha, self.title, self.body, self.is_draft,
                   [[c.name, c.status, c.conclusion, c.details_url] for c in self.checks],
                   [[r.author, r.state, r.commit_sha, r.body] for r in self.reviews if not is_automated(r.body)],
                   [[f.path, f.previous_path, f.status, f.additions, f.deletions, f.patch] for f in self.files]]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class WorkflowRun:
    repo: str
    run_id: int
    attempt: int
    workflow_file: str   # basename of the workflow path, e.g. "infra-apply.yml"
    event: str
    head_branch: str
    head_sha: str
    status: str


@dataclass(frozen=True)
class Job:
    job_id: int
    name: str
    status: str
    conclusion: str


@dataclass(frozen=True)
class PendingEnvironment:
    env_id: int
    name: str
    can_approve: bool


@dataclass(frozen=True)
class Decision:
    """What to do with one request. PR outcomes: approve | request_changes | comment | nothing (the model's
    four answers) plus ignore | defer | satisfied | review | unclear (code-owned). Deployments: approve | ignore
    | defer | satisfied | review | unclear."""
    outcome: str
    reason: str       # private: one line for the journal
    source: str       # eligibility | readiness | observed | ai
    data: dict = field(default_factory=dict)
    body: str = ""    # public text posted on GitHub (PR actions only)
    notify: str | None = None   # one line for the owner's notification, when they should look themselves
    summary: str = ""          # public v2 summary; body is the rendered output (or legacy v1 text)
    findings: tuple[Finding, ...] = ()
    why: str = ""
    watch: str = ""
