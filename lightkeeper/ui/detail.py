"""PR detail reads immutable snapshots and local session files; actions use the Runner inbox."""
from datetime import datetime
from dataclasses import replace
from pathlib import Path
import re

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Static

from ..posting import owner_approval_body
from .rich_view import STATES, age, footer_text, glyph, header_lines, multiline as text, word


def section(title, *parts):
    return Group(text(title, "bold underline"), *parts)


def key_values(rows):
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(justify="right", style="dim", no_wrap=True)
    table.add_column(ratio=1)
    for label, value in rows:
        table.add_row(text(label), text(value) if isinstance(value, str) else value)
    return table


def timeline_table(events, ascii=False):
    table = Table(box=None, padding=(0, 1), pad_edge=False, expand=True)
    for name in ("time", "event", "detail"):
        table.add_column(text(name, "dim"), ratio=1 if name == "detail" else None,
                         no_wrap=name != "detail")
    # Collapse identical displayed event text, retaining the first occurrence's time/order.
    grouped = {}
    for event in events:
        detail = event.summary or event.private_reason or event.why
        if event.change_text:
            detail = " · ".join(filter(None, (detail, event.change_text)))
        key = (event.state, detail)
        first, count = grouped.get(key, (event, 0))
        grouped[key] = (first, count + 1)
    for (state, detail), (event, count) in grouped.items():
        suffix = f" {'x' if ascii else '×'}{count}" if count > 1 else ""
        table.add_row(text(datetime.fromtimestamp(event.at).strftime("%H:%M:%S"), "dim"),
                      text(state + suffix), text(detail))
    return table if events else text("none", "dim")


def session_path(state_dir: str, session: str) -> Path | None:
    if not state_dir or not session:
        return None
    try:
        root, path = Path(state_dir).expanduser().resolve(), Path(session).expanduser().resolve()
        return path if path != root and path.is_relative_to(root) else None
    except (OSError, RuntimeError, ValueError):
        return None


def fetched_description(prompt):
    """Find the generated Description section outside any verbatim fenced data."""
    lines = prompt.splitlines()
    fence = None
    for index, line in enumerate(lines):
        if fence:
            if line == fence:
                fence = None
            continue
        opening = re.search(r"(`{4,})$", line)
        if opening:
            fence = opening.group(1)
        elif line == "## Description" and index + 1 < len(lines):
            opening = lines[index + 1]
            if re.fullmatch(r"`{4,}", opening):
                body = []
                for value in lines[index + 2:]:
                    if value == opening:
                        break
                    body.append(value)
                    if len(body) == 12:
                        break
                return "\n".join(body)
    return None


class ConfirmReopen(ModalScreen):
    DEFAULT_CSS = """
    ConfirmReopen { align: center middle; }
    #confirmation { width: 64; height: auto; padding: 1 2; border: solid yellow; background: $surface; }
    #confirmation Static { height: auto; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("enter", "confirm", "Re-review")]

    def __init__(self, item, callback):
        super().__init__()
        self.item, self.callback = item, callback

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="confirmation"):
            yield Static(text(f"Re-review {self.item.repo}#{self.item.number} at {self.item.head[:7]}?\n"
                              "Clears the cached assessment; the next tick asks the model again.\n"
                              "The Runner checks the current head and protected outcomes before reopening."), markup=False)
            yield Button("Re-review", id="confirm", variant="warning")
            yield Button("Cancel", id="cancel")
        yield Static(footer_text(1, "Enter re-review · Esc cancel"), markup=False, classes="modal-navigation")

    def check_action(self, action, parameters):
        return not isinstance(self.focused, Input)

    def action_cancel(self):
        self.dismiss()

    def action_confirm(self):
        self.callback()
        self.dismiss()

    def on_button_pressed(self, event):
        self.action_confirm() if event.button.id == "confirm" else self.action_cancel()


class ConfirmOwnerApprove(ModalScreen):
    DEFAULT_CSS = """
    ConfirmOwnerApprove { align: center middle; }
    ConfirmOwnerApprove VerticalScroll { width: 76; height: auto; max-height: 90%; padding: 1 2; border: solid yellow; background: $surface; }
    ConfirmOwnerApprove Static { height: auto; margin-bottom: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, item, runtime, verdict, summary, floor, callback, floor_counts=(), floor_stale=False):
        super().__init__()
        self.item, self.runtime, self.floor, self.callback = item, runtime, floor, callback
        self.verdict, self.summary = verdict, summary
        self.floor_counts, self.floor_stale = floor_counts, floor_stale
        self.submitted = False

    def compose(self):
        with VerticalScroll():
            yield Static(text(f"Approve {self.item.repo}#{self.item.number} at {self.item.head[:7]}?\n"
                              f"Model verdict: {self.verdict or 'not assessed'}\n"
                              f"Public summary: {self.summary or 'none'}\n"
                              f"Floor refusal: {self.floor or 'none'}\n"
                              f"Effective mode: live · gh {self.runtime.gh_login or '?'}"
                              f"{' (unconfirmed)' if self.runtime.gh_confirmed is False else ''} · expected @{self.runtime.login}"),
                         markup=False)
            yield Static(text("Note (required to override the floor)" if self.floor else "Note (optional)"), markup=False)
            yield Static(text(), markup=False, id="owner-freshness")
            yield Input(placeholder="Your approval note", id="owner-note")
            yield Static(text(owner_approval_body("", self.runtime.login)), markup=False, id="owner-preview")
            yield Button("Approve", id="confirm", variant="warning", disabled=bool(self.floor))
            yield Button("Cancel", id="cancel")
        yield Static(footer_text(1, "Tab choose · Enter select · Esc cancel"), markup=False, classes="modal-navigation")

    def on_mount(self):
        self.refresh_confirmation()

    def check_action(self, action, parameters):
        return action == "cancel" or not isinstance(self.focused, Input)

    def refresh_confirmation(self):
        detail = self.app.snapshot.details.get((self.item.repo, self.item.number, self.item.head))
        if detail is None or detail.floor_stale or (detail.floor_counts, detail.approve_refusal) != (self.floor_counts, self.floor):
            self.floor_stale = True
        self.query_one("#owner-freshness", Static).update(text(
            "CI changed since; reopen to confirm" if self.floor_stale else ""))
        note = self.query_one("#owner-note", Input).value
        body = owner_approval_body(note, self.runtime.login)
        self.query_one("#owner-preview", Static).update(text(body))
        self.query_one("#confirm", Button).disabled = (self.submitted or self.floor_stale or
            bool(self.floor and body == owner_approval_body("", self.runtime.login)))

    def on_input_changed(self, event):
        self.refresh_confirmation()

    def action_cancel(self):
        self.dismiss()

    def on_button_pressed(self, event):
        if self.submitted:
            return
        if event.button.id == "confirm":
            self.refresh_confirmation()
            if self.query_one("#confirm", Button).disabled:
                return
            self.submitted = True
            self.query_one("#confirm", Button).disabled = True
            self.callback(self.query_one("#owner-note", Input).value)
        self.dismiss()


class PRDetail(Screen):
    DEFAULT_CSS = """
    PRDetail #detail-header { height: 2; }
    PRDetail VerticalScroll { height: 1fr; }
    PRDetail #detail-top, PRDetail #detail-status, PRDetail #detail-body, PRDetail #detail-timeline,
    PRDetail #detail-assessment, PRDetail #detail-description, PRDetail #session-pane { height: auto; margin-bottom: 1; }
    PRDetail #detail-footer { height: auto; }
    """
    BINDINGS = [Binding("escape", "back", "Back"), Binding("o", "open_url", "GitHub"),
                Binding("s", "session", "Session folder"), Binding("p", "prompt", "Prompt / raw answer"),
                Binding("R", "reopen", "Re-review"), Binding("A", "owner_approve", "Owner approve")]

    def __init__(self, row):
        super().__init__()
        self.row = row
        self.key = (row.item.repo, row.item.number, row.item.head)
        self.message = ""
        self.pane_visible = False
        self._loaded_session = None
        self._load_generation = 0
        self.pending_approval = None
        self._description_session = None
        self._description_generation = 0

    def compose(self) -> ComposeResult:
        yield Static(text(), markup=False, id="detail-header")
        with VerticalScroll():
            yield Static(text(), markup=False, id="detail-top")
            yield Static(text(), markup=False, id="detail-status")
            yield Static(text(), markup=False, id="detail-assessment")
            yield Static(text(), markup=False, id="detail-body")
            yield Static(text(), markup=False, id="detail-description")
            yield Static(text(), markup=False, id="detail-timeline")
            yield Static(text(), markup=False, id="session-pane")
        yield Static(text(), markup=False, id="detail-footer")

    def on_mount(self):
        self.query_one("#session-pane").display = False
        self.query_one("#detail-description").display = False
        self.refresh_detail()

    def check_action(self, action, parameters):
        return not isinstance(self.focused, Input)

    def refresh_detail(self):
        snapshot = self.app.snapshot
        if self.app.worker.runner.cancelled and snapshot.runtime.phase == "writing":
            snapshot = replace(snapshot, runtime=replace(snapshot.runtime, phase="finishing"))
        runtime = snapshot.runtime
        row = next((r for r in (*snapshot.watching, *snapshot.history)
                    if (r.item.repo, r.item.number, r.item.head) == self.key), self.row)
        self.row = row
        item = row.item
        events = snapshot.detail(self.key)
        detail = snapshot.details.get(self.key)
        self.query_one("#detail-header", Static).update(header_lines(
            snapshot, self.app.clock(), self.size.width, self.app.ascii, self.app.queued_text(snapshot)))
        ci = detail.ci if detail and detail.ci is not None else getattr(row, "ci", None)
        links = detail.failed_check_links if detail else ()
        optional = detail.optional_checks if detail else ()
        failed = ", ".join(name + (f" ({url})" if url else "") for name, url in links) if links else ", ".join(
            getattr(row, "failed_checks", ()))
        counts = " · ".join(f"{n} {label}" for n, label in (
            (ci.passed, "passed"), (ci.running, "running"),
            (ci.failed, "failed" + (f": {failed}" if failed else "")), (ci.other, "other")) if n) if ci else ""
        if not counts:
            counts = "no checks" if ci is not None or row.state in ("closed", "merged") else "CI unavailable"
        if optional:
            counts += "\n" + ", ".join(f"{name} (not required)" for name in optional)
        private = detail.latest_private_reason if detail else next(
            (e.private_reason for e in reversed(events) if e.private_reason), "")
        summary = detail.latest_summary if detail else next((e.summary for e in reversed(events) if e.summary), "")
        needs_you = getattr(row, "needs_you", None)
        if needs_you:
            next_step = "o opens GitHub · A approves as owner if it is fine"
        elif row.state in ("closed", "merged", "approved") and not row.dry_run:
            next_step = "final"
        elif row.state == "yours":
            next_step = "yours (takeover)"
        elif runtime.reviewer_paused_until > self.app.clock() or any(
                a.key == "engine_identity" and a.level == "error" for a in snapshot.alerts):
            next_step = "paused"
        elif self.app.worker.runner.commands.targets(self.key):
            next_step = "queued"
        elif row.state == "unknown":
            next_step = "awaiting reconciliation"
        elif row.state == "withheld" or runtime.policy_error or any(
                a.key in ("gh_identity", "settings_error") and a.level == "error" for a in snapshot.alerts):
            next_step = "needs you"
        elif row.state == "waiting" and getattr(row, "is_draft", False):
            next_step = "waits for draft to be ready"
        elif row.state == "waiting" and row.why.startswith("checks still running"):
            next_step = "waits for required checks"
        elif row.state in ("waiting", "reviewing"):
            next_step = "reviewing" if row.state == "reviewing" else "next check"
        else:
            next_step = "final"
        base = detail.base_ref if detail else ""
        reviewers = detail.requested_reviewers if detail else None
        pushed = ""
        if detail and detail.head_pushed_at:
            try:
                stamp = datetime.fromisoformat(detail.head_pushed_at.replace("Z", "+00:00"))
                if stamp.tzinfo is not None:
                    since_snapshot = max(0, self.app.clock() - snapshot.mono_at) if snapshot.mono_at is not None else 0
                    pushed = "pushed " + age(snapshot.wall_now + since_snapshot - stamp.timestamp()) + " ago"
            except ValueError:
                pass
        draft = detail.is_draft if detail and detail.ci is not None else getattr(row, "is_draft", False)
        metadata = [f"author {item.author}" if item.author else "", f"head {item.head[:7]}" if item.head else "",
                    f"base {base}" if base else "", "draft" if draft else "", pushed]
        self.query_one("#detail-top", Static).update(Group(
            text(f"{item.repo}#{item.number} · {item.title}", "bold"),
            text(" · ".join(filter(None, metadata)), "dim")))
        state = text().append_text(glyph(row.state, self.app.clock(), self.app.ascii))
        state.append(" needs you" if needs_you else " " + word(row), "bold yellow" if needs_you else STATES[row.state][2])
        status = [("state", state), ("why", needs_you or row.why or "none"), ("next step", next_step), ("CI", counts)]
        if reviewers is not None:
            status.append(("reviewers", ", ".join(reviewers) or "none"))
        closed = next((e for e in reversed(events) if e.state in ("closed", "merged")), None)
        if closed:
            status.append(("merged" if row.state == "merged" else "closed",
                           datetime.fromtimestamp(closed.at).strftime("%Y-%m-%d %H:%M:%S")))
        self.query_one("#detail-status", Static).update(section("Status", key_values(status)))
        findings = detail.latest_findings if detail else ()
        assessed = bool((detail and detail.latest_verdict) or any(e.state == "assessment" for e in events))
        assessment = self.query_one("#detail-assessment", Static)
        if assessed:
            parts = []
            if detail and detail.latest_why:
                parts.append(text(detail.latest_why, "bold"))
            if detail and detail.latest_watch:
                parts.append(text(detail.latest_watch))
            parts.append(key_values([("public summary", summary or "none")]))
            for finding in findings:
                bullet = text("- " if self.app.ascii else "• ")
                bullet.append_text(text(finding.severity, {"blocker": "bold red", "important": "yellow",
                                                          "minor": "cyan"}.get(finding.severity, "dim")))
                bullet.append_text(text(" · " + finding.origin, "dim"))
                bullet.append_text(text(" · " + finding.text))
                for value in (finding.path, finding.url):
                    if value:
                        bullet.append_text(text(" · " + value, "dim"))
                parts.append(bullet)
            if not findings:
                parts.append(text("no findings", "dim"))
            parts.append(Panel(text(private or "none", "dim"), title=text("private · journal only", "dim"),
                               title_align="left", border_style="dim", box=box.ASCII if self.app.ascii else box.SQUARE))
            assessment.update(section("Assessment", *parts))
        else:
            assessment.update(text("no assessment on this head", "dim"))
        post = detail.latest_post if detail else None
        if post is None:
            post = next((e for e in reversed(events) if e.state in (
                "applied", "satisfied", "owner_approve", "withheld", "unknown", "owner_attempt", "nothing", "ignore",
                "error", "owner_error", "unclear") or e.state.startswith("would_")), None)
        latest_outcome = post.state if post else ""
        outcome = ("Posted" if latest_outcome in ("applied", "owner_approve", "satisfied") else
                   "Dry-run" if latest_outcome.startswith("would_") else
                   "Withheld" if latest_outcome == "withheld" else
                   "Outcome unknown" if latest_outcome in ("unknown", "owner_attempt") else "nothing posted")
        posted = text(outcome, "bold")
        if post:
            posted.append(" · " + datetime.fromtimestamp(post.at).strftime("%Y-%m-%d %H:%M:%S"), "dim")
            if post.review_url:
                posted.append_text(text(" · " + post.review_url))
        body_event = next((e for e in reversed(events) if e.body), None)
        body = detail.latest_body if detail else body_event.body if body_event else ""
        bodies = [text(body)] if body and outcome in ("Posted", "Dry-run", "Outcome unknown") else []
        self.query_one("#detail-body", Static).update(section("Posted", posted, *bodies))
        self.query_one("#detail-timeline", Static).update(section("Timeline", timeline_table(events, self.app.ascii)))
        result = runtime.last_command.text if runtime.last_command else ""
        if self.pending_approval:
            done = next((r for r in runtime.command_results if r.id == self.pending_approval), None)
            self.message = done.text if done else self.app.queued_text(snapshot) or "queued"
            if done:
                self.pending_approval = None
        self.query_one("#detail-footer", Static).update(footer_text(
            1, "o GitHub · s session · p prompt/raw · R re-review · A owner approve · Esc back", self.message or result))
        if self.session() != self._description_session:
            self.load_description()
        if self.pane_visible and self.session() != self._loaded_session:
            self.load_session()

    def load_description(self):
        session = self._description_session = self.session()
        self._description_generation += 1
        generation = self._description_generation
        state_dir = self.app.snapshot.runtime.state_dir
        self.query_one("#detail-description").display = False
        app = self.app

        def read():
            value = None
            path = session_path(state_dir, session)
            if path:
                candidate = session_path(state_dir, str(path / "prompt.md"))
                try:
                    if candidate:
                        with candidate.open("rb") as stream:
                            prompt = stream.read(64 * 1024).decode("utf-8", errors="replace")
                        description = fetched_description(prompt)
                        if description is not None:
                            value = section("Description", text(description or "(empty)"))
                except OSError:
                    pass
            app.call_from_thread(self.show_description, generation, value)
        self.run_worker(read, thread=True)

    def show_description(self, generation, value):
        if self.is_mounted and generation == self._description_generation:
            widget = self.query_one("#detail-description", Static)
            widget.display = value is not None
            if value is not None:
                widget.update(value)

    def session(self):
        snapshot = self.app.snapshot
        detail = snapshot.details.get(self.key)
        if detail:
            return detail.latest_session
        return next((e.session for e in reversed(snapshot.detail(self.key)) if e.session), "")

    def action_back(self):
        self.app.pop_screen()

    def action_open_url(self):
        self.app.run_worker(lambda: self.app.open_url_in_thread(self.row.item.url), thread=True)

    def action_session(self):
        app = self.app
        state_dir, session = app.snapshot.runtime.state_dir, self.session()
        def open_session():
            path = session_path(state_dir, session)
            if path is None:
                app.call_from_thread(app.show_message, "Session unavailable or outside the state directory")
            else:
                app.open_folder_in_thread(path)
        self.run_worker(open_session, thread=True)

    def action_prompt(self):
        self.pane_visible = not self.pane_visible
        self.query_one("#session-pane").display = self.pane_visible
        if self.pane_visible:
            self.load_session()

    def load_session(self):
        self._loaded_session = self.session()
        self._load_generation += 1
        generation = self._load_generation
        state_dir = self.app.snapshot.runtime.state_dir
        session = self._loaded_session
        self.query_one("#session-pane", Static).update(text("Loading session…"))
        app = self.app

        def read():
            path = session_path(state_dir, session)
            parts = []
            for name in ("prompt.md", "stdout.json"):
                value = "unavailable"
                if path:
                    candidate = session_path(state_dir, str(path / name))
                    try:
                        if candidate:
                            with candidate.open("rb") as stream:
                                raw = stream.read(64 * 1024)
                                truncated = stream.seek(0, 2) > len(raw)
                            value = raw.decode("utf-8", errors="replace")
                            if truncated:
                                value += "\n… truncated, open with s"
                    except OSError:
                        pass
                parts.append(f"{name} (read-only)\n{value}")
            app.call_from_thread(self.show_session, generation, text("\n\n".join(parts)))
        self.run_worker(read, thread=True)

    def show_session(self, generation, value):
        if self.is_mounted and generation == self._load_generation:
            self.query_one("#session-pane", Static).update(value)

    def action_reopen(self):
        if self.row.state == "reviewing":
            self.message = "refused: already queued"
            self.refresh_detail()
            return
        # Capture the revision now; updates under the modal cannot change the confirmed target.
        item, key = self.row.item, self.key
        self.app.push_screen(ConfirmReopen(item, lambda: self.app.send_command("reopen", key, item.head)))

    def action_owner_approve(self):
        if self.pending_approval:
            self.refresh_detail()
            return
        snapshot = self.app.snapshot
        runtime, item = snapshot.runtime, self.row.item
        reason = ("dry-run mode" if runtime.mode != "live" or item.repo in runtime.dry_repos or not runtime.live_lock_held else
                  "repository disabled" if item.repo not in runtime.repos else
                  "approve disabled" if item.repo not in runtime.approve_repos else
                  "unknown write" if any((i.repo, i.number, i.head) == self.key for i in snapshot.unknown_items) else "")
        if reason:
            self.message = f"refused: {reason}"
            self.refresh_detail()
            return
        detail = snapshot.details.get(self.key)
        key, head = self.key, item.head
        def confirm(note):
            if not self.pending_approval:
                self.pending_approval = self.app.send_command("owner_approve", key, head, note)
            self.refresh_detail()
        self.message = ""
        self.app.push_screen(ConfirmOwnerApprove(
            item, runtime, detail.latest_verdict if detail else "", detail.latest_summary if detail else "",
            detail.approve_refusal if detail else "", confirm,
            detail.floor_counts if detail else (), detail.floor_stale if detail else True))
