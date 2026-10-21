"""Incremental journal projection and the small runner-to-snapshot adapter. No terminal I/O."""
from collections import Counter, deque
from dataclasses import replace
from datetime import datetime
import time
from types import MappingProxyType

from ..settings import changed_components
from ..models import Finding
from ..commands import CommandResult
from .model import Alert, DetailState, Event, HistoryRow, ItemRef, ItemRow, Runtime, Screen, Stats

ACTIONS = {"approve": "approved", "request_changes": "changes", "comment": "comment"}
STATES = {"defer": "waiting", "satisfied": "approved", "nothing": "silent", "ignore": "yours",
          "owner_approve": "approved", "owner_error": "error", "owner_attempt": "unknown",
          "closed": "closed", "unknown": "unknown", "error": "error", "unclear": "error", "withheld": "withheld"}


def item_ref(kind, key, **fields) -> ItemRef:
    return ItemRef(kind, key[0], int(key[1]), str(key[2]) if len(key) > 2 else "",
                   fields.get("title", ""), fields.get("environment", ""),
                   int(key[3]) if kind == "deployment" and len(key) > 3 else 0, fields.get("author", ""))


def label(item: ItemRef, repos: tuple[str, ...]) -> str:
    name = item.repo.rsplit("/", 1)[-1]
    if sum(r.rsplit("/", 1)[-1] == name for r in set(repos)) > 1:
        name = item.repo
    suffix = f"#{item.number}"
    return name + suffix + (f"/{item.environment or item.environment_id}" if item.kind == "deployment" else "")


class Projection:
    def __init__(self, history_limit: int = 200, log_path: str = "run.log"):
        self.log_path = log_path
        self._current: dict[tuple, tuple] = {}
        self._rows: dict[tuple, ItemRow] = {}
        self._open: set[tuple] = set()
        self._verdicts: dict[tuple, tuple] = {}
        self._history: deque[HistoryRow] = deque(maxlen=history_limit)
        self._counts: dict[tuple, set[tuple]] = {}
        self._changes: dict[str, str] = {}
        self._statuses: dict[tuple, object] = {}
        self._full_statuses: dict[tuple, object] = {}
        self._events: dict[tuple, tuple[Event, ...]] = {}
        self._unknown: dict[tuple, ItemRef] = {}
        self._details: dict[tuple, DetailState] = {}

    def feed(self, rec: dict) -> None:
        if rec["event"] == "rehash":
            return
        kind, key, event = rec["kind"], tuple(rec["key"]), rec["event"]
        item = item_ref(kind, key, **{k: rec[k] for k in ("title", "environment", "author") if k in rec})
        identity, revision = item.identity, (kind, *key)
        at = datetime.fromisoformat(rec["at"].replace("Z", "+00:00")).timestamp()
        previous = self._rows.get(revision)
        if previous:
            item = replace(item, title=rec.get("title", previous.item.title),
                           author=rec.get("author", previous.item.author),
                           environment=rec.get("environment", previous.item.environment))
        row = previous or ItemRow(item, "waiting", "new head; queued", at)
        row = replace(row, item=item)
        change_text = ""
        # Reconciliation may refer to an older head; it cannot roll back a seen current head.
        if event == "seen" or identity not in self._current or not (event in ("applied", "satisfied", "owner_approve")
                                                                    and rec.get("source") == "observed"):
            self._current[identity] = revision
        if event in ("closed", "gate_gone"):
            if self._current.get(identity) == revision:
                self._open.discard(identity)
        elif kind == "pr" or event == "seen" or previous is None:
            self._open.add(identity)
        if event == "notify":
            message = rec.get("text") or rec.get("reason") or ""
            for prefix in (f"{item.repo}#{item.number}: ", f"{item.repo.rsplit('/', 1)[-1]}#{item.number}: "):
                message = message.removeprefix(prefix)       # the row already names the item
            row = replace(row, needs_you=message, needs_you_since=at)
        state = ACTIONS.get(rec.get("action", "approve")) if event == "applied" else STATES.get(event)
        dry = event.startswith("would_")
        if dry:
            state = ACTIONS.get(event[6:])
        if state:
            public = event == "applied" or dry
            owner = event == "owner_approve" or (event == "satisfied" and rec.get("origin") == "owner")
            why = ("approved by you on GitHub" if event == "satisfied" and owner else
                   "approved by you" if owner else
                   rec.get("reason", "") if event == "applied" and rec.get("source") == "observed" else
                   rec.get("summary", "") if public else
                   "already approved" if event == "satisfied" else rec.get("reason", ""))
            if state == "closed" and "merged" in why:
                state = "merged"
            shown = state                         # what the Watching row shows; history keeps `state`
            if row.state == "changes" and not row.dry_run and event in ("nothing", "unclear"):
                shown = "changes"                 # our block on this head still stands; "silent" would hide it
            since = row.since if (row.state, row.dry_run) == (shown, dry) else at
            clear = event in ("satisfied", "owner_approve", "ignore", "closed") or (event == "applied" and state == "approved")
            row = replace(row, state=shown, why=why, since=since, dry_run=dry,
                          needs_you=None if clear else row.needs_you)
            meaningful = owner or (event not in ("defer", "satisfied") and not (event == "applied" and rec.get("source") == "observed"))
            if meaningful:
                note = why if public or state != "approved" else rec.get("body") or rec.get("notify") or ""
                if owner:
                    note = why
                if event == "withheld":
                    note = f"{rec['action']}: {why}"
                signature = (state, note if event == "withheld" else why, dry)
                old = self._verdicts.get(revision)
                changed = bool(old and old[1] != rec.get("policy") and state not in ("closed", "merged"))
                change_text = (changed_components(old[2], rec["components"], item.repo)
                               if old and old[2] and rec.get("components") else self._changes.get(item.repo, ""))
                if old and old[0] == signature:
                    for i in range(len(self._history) - 1, -1, -1):
                        h = self._history[i]
                        if h.item.identity == identity and h.item.head == item.head:
                            self._history[i] = replace(h, repeats=h.repeats + 1, new_rules=h.new_rules or changed,
                                                      change_text=change_text if changed else h.change_text)
                            break
                else:
                    self._history.append(HistoryRow(item, state, note, at, dry,
                                                   new_rules=changed, change_text=change_text))
                self._verdicts[revision] = (signature, rec.get("policy"), rec.get("components"))
        if event == "gate_gone":
            row = replace(row, needs_you=None)
        if event == "reopen":
            row = replace(row, state="waiting", why="re-review queued", since=at, dry_run=False)
        self._events[key] = (*self._events.get(key, ()), Event(
            at, event, row.why, rec.get("summary", ""), rec.get("published_findings", 0),
            rec.get("engine", rec.get("data", {}).get("engine", "")),
            rec.get("private_reason", rec.get("reason", "")), rec.get("body", ""), change_text,
            rec.get("session", rec.get("data", {}).get("session", "")), rec.get("url", "")))[-20:]
        latest = self._events[key][-1]
        detail = self._details.get(key, DetailState())
        status = self._statuses.get(identity)
        self._details[key] = replace(
            detail,
            latest_post=latest if event in ("applied", "satisfied", "owner_approve", "withheld", "unknown",
                                            "owner_attempt", "nothing", "ignore", "error", "owner_error", "unclear")
            or dry else detail.latest_post,
            latest_body=latest.body if event == "satisfied" and rec.get("origin") == "owner" else latest.body or detail.latest_body,
            latest_session=latest.session or detail.latest_session,
            latest_private_reason=(latest.private_reason if event == "assessment" else
                                   detail.latest_private_reason if detail.latest_verdict else
                                   latest.private_reason or detail.latest_private_reason),
            latest_outcome=event if state and event != "defer" else detail.latest_outcome,
            latest_summary=latest.summary if event == "assessment" else latest.summary or detail.latest_summary,
            latest_why=rec.get("why", "") if event == "assessment" else detail.latest_why,
            latest_watch=rec.get("watch", "") if event == "assessment" else detail.latest_watch,
            latest_verdict=rec.get("action", "") if event == "assessment" else detail.latest_verdict,
            approve_refusal=status.approve_refusal if status and status.head == item.head else detail.approve_refusal,
            latest_findings=tuple(Finding.from_record(f) for f in rec.get("findings", ()))
            if event == "assessment" else detail.latest_findings)

        if event in ("unknown", "owner_attempt"):
            self._unknown[key] = item
        elif event in ("applied", "satisfied", "owner_approve", "owner_error"):
            self._unknown.pop(key, None)
        self._rows[revision] = row
        retained = {self._current[identity][1:] for identity in self._open}
        retained.update((h.item.repo, h.item.number, h.item.head) if h.item.kind == "pr" else
                        (h.item.repo, h.item.number, int(h.item.head), h.item.environment_id) for h in self._history)
        for old_key in self._events.keys() - retained:
            del self._events[old_key]
            self._details.pop(old_key, None)
        if ((event == "applied" and rec.get("source") != "observed") or event == "owner_approve" or dry
                or event == "satisfied" and rec.get("origin") == "owner"):
            action = event[6:] if dry else rec.get("action", "approve")
            counted_at = (datetime.fromisoformat(rec["reviewed_at"].replace("Z", "+00:00")).timestamp()
                          if rec.get("reviewed_at") else at)
            day = datetime.fromtimestamp(counted_at).date()
            if rec.get("origin") == "owner":
                for (_, was_dry), counted in self._counts.items():
                    if not was_dry:
                        counted.discard((revision, action))
            self._counts.setdefault((day, dry), set()).add((revision, action))

    def screen(self, runtime: Runtime, wall_now: float) -> Screen:
        rows = []
        for identity in self._open:
            row = self._rows[self._current[identity]]
            if row.item.repo not in runtime.repos:
                continue
            status = self._statuses.get(identity)
            if status and status.head == row.item.head:
                row = replace(row, item=replace(row.item, author=status.author, title=status.title), ci=status.ci,
                              is_draft=status.is_draft, failed_checks=status.failed_checks or ())
            if runtime.phase == "reviewing" and runtime.current_item and runtime.current_item.identity == identity:
                row = replace(row, state="reviewing", why="model reviewing", dry_run=False)
            row = replace(row, repo_dry=row.item.repo in runtime.dry_repos)
            rows.append(row)
        rows.sort(key=lambda r: (not bool(r.needs_you), r.state != "reviewing", r.since, r.item.identity))
        day = datetime.fromtimestamp(wall_now).date()
        def counts(dry):
            return tuple(sorted(Counter(action for revision, action in self._counts.get((day, dry), ())
                                        if revision[1] in runtime.repos).items()))
        history = tuple(h for h in reversed(self._history) if h.item.repo in runtime.repos)
        # Key hints belong to the screen that has the keys (textual_app.alert_text), not to plain output.
        alerts = tuple(Alert(f"needs_you:{r.item.identity}", "needs_you",
                             f"{label(r.item, runtime.repos)} needs you: {r.needs_you}",
                             r.needs_you_since or r.since, r.item.repo) for r in rows if r.needs_you) + runtime.alerts
        return Screen(runtime, tuple(rows), history, alerts, Stats(counts(False), counts(True)),
                      wall_now, self.log_path, events=MappingProxyType(dict(self._events)),
                      unknown_items=tuple(item for item in self._unknown.values() if item.repo in runtime.repos),
                      details=MappingProxyType(dict(self._details)))

    def policy_changed(self, repo, text):
        self._changes[repo] = text

    def pr_status(self, status):
        identity = ("pr", status.repo, status.number, 0)
        previous = self._statuses.get(identity)
        if previous and previous.head != status.head:
            old_key = (previous.repo, previous.number, previous.head)
            if old_key in self._details:
                self._details[old_key] = replace(self._details[old_key], floor_stale=True)
        key = (status.repo, status.number, status.head)
        detail = self._details.get(key, DetailState())
        if status.failed_checks is not None:
            self._full_statuses[identity] = status
            detail = replace(detail, approve_refusal=status.approve_refusal,
                             floor_stale=False, floor_counts=status.counts,
                             base_ref=status.base_ref, requested_reviewers=status.requested_reviewers,
                             head_pushed_at=status.head_pushed_at or detail.head_pushed_at,
                             failed_check_links=status.failed_check_links)
        else:
            full = self._full_statuses.get(identity)
            stale = not full or full.head != status.head or full.counts != status.counts
            detail = replace(detail, floor_stale=detail.floor_stale or stale)
            if full and full.head == status.head:
                status = replace(status, approve_refusal=full.approve_refusal,
                                 failed_checks=full.failed_checks if not stale else None)
            if stale:
                detail = replace(detail, failed_check_links=())
        detail = replace(detail, ci=status.ci, is_draft=status.is_draft,
                         head_pushed_at=status.head_pushed_at or detail.head_pushed_at)
        self._statuses[identity] = status
        self._details[key] = detail


class Observer:
    """Only the runner thread mutates these fields; the UI reads the immutable snapshot reference."""
    def __init__(self, projection: Projection, runtime: Runtime, mono=time.monotonic, wall=time.time):
        self.projection, self.runtime, self.mono, self.wall = projection, runtime, mono, wall
        self.snapshot = projection.screen(runtime, wall())

    def publish(self):
        self.snapshot = replace(self.projection.screen(self.runtime, self.wall()), mono_at=self.mono())

    def feed(self, record):
        self.projection.feed(record)
        self.publish()

    def replay(self, journal):
        """Build startup state without publishing an intermediate snapshot for every record."""
        journal.observer = self.projection.feed
        try:
            records = journal.replay()
        finally:
            journal.observer = self.feed
        self.publish()
        return records

    def __call__(self, event, **fields):
        now = self.mono()
        if event == "command_done":
            result = CommandResult(**fields)
            self.runtime = replace(self.runtime, last_command=result,
                                   command_results=(*self.runtime.command_results, result)[-16:])
        elif event == "repo_check":
            checks = dict(self.runtime.repo_checks)
            checks[fields["repo"]] = fields["text"]
            self.runtime = replace(self.runtime, repo_checks=tuple(checks.items()))
        elif event == "alert_attempt":
            attempts = {key: (at, result) for key, at, result in self.runtime.alert_attempts}
            attempts[fields["key"]] = (now, fields["text"])
            self.runtime = replace(self.runtime, alert_attempts=tuple((key, *value) for key, value in attempts.items()))
        elif event == "phase":
            ref = item_ref(fields["kind"], fields["key"]) if fields.get("key") else None
            self.runtime = replace(self.runtime, phase=fields["phase"], phase_since=now, current_item=ref,
                                   progress=fields.get("progress", self.runtime.progress), next_check_at=None,
                                   phase_reason=fields.get("reason", ""))
        elif event == "pr_status":
            self.projection.pr_status(fields["status"])
        elif event == "sleeping":
            self.runtime = replace(self.runtime, phase=fields.get("phase", "sleeping"), phase_since=now,
                                   current_item=None, next_check_at=now + fields["seconds"],
                                   backoff=fields["seconds"] if fields.get("phase") == "backoff" else 0)
        elif event == "tick_done":
            self.runtime = replace(self.runtime, last_check_at=now, last_check_ok=fields["ok"],
                                   last_check_seconds=fields["seconds"], current_item=None, phase="idle")
        elif event == "policy":
            if "text" in fields:
                self.projection.policy_changed(fields["repo"], fields["text"])
            else:
                if "idle" in fields:
                    fields["screen_revision"] = self.runtime.screen_revision + 1
                self.runtime = replace(self.runtime, **fields)
        elif event == "gh_identity":
            self.runtime = replace(self.runtime, gh_login=fields["login"], gh_confirmed=fields["confirmed"])
        elif event == "engine_identity":
            self.runtime = replace(self.runtime, engine_identity=fields["identity"], engine_name=fields["name"],
                                   engine_profile=fields["profile"])
        elif event == "condition":
            key, text = fields["key"], fields["text"]
            alerts = {a.key: a for a in self.runtime.alerts}
            old = alerts.pop(key, None)
            if text:
                level = fields.get("level", "error" if key in ("settings_error", "policy_error", "tick_failed", "unknown_write", "gh_identity", "engine_identity") else "warning")
                alerts[key] = Alert(key, level, text, old.since if old else now,
                                    fields.get("repo", ""), fields.get("fix_command", ""),
                                    fields.get("scope", "all watched items"))
            extra = {key: text} if key in ("policy_error", "search_saturated") else {}
            if key == "tick_failed":
                extra["last_error"] = text
            if key == "reviewer_paused":
                extra["reviewer_paused_until"] = now + fields.get("seconds", 0) if text else 0
            self.runtime = replace(self.runtime, alerts=tuple(alerts.values()), **extra)
        self.publish()
