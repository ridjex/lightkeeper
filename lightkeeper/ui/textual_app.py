"""Textual Overview over immutable snapshots; the runner alone owns work and writes."""
from collections import deque
from dataclasses import asdict, replace
import logging
import os
import queue
import subprocess
import sys
import time
import webbrowser
from uuid import uuid4

from rich.console import Console
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from ..commands import Command
from .idle import Look
from .project import label
from .platform import clipboard as system_clipboard, open_folder
from .detail import ConfirmOwnerApprove, PRDetail
from .agent import AgentPanel
from .repos import Repositories, RepoForm, read_settings, error_text
from .menu import Menu, ScreenForm
from .rich_view import age, countdown, glyph, header_lines, footer_text, idle_panel, multiline, print_summary, row_data, text


class OverviewTable(DataTable, inherit_bindings=False):
    """Navigation belongs to the app's single cursor, not individual tables."""

    def on_click(self, event):
        event.prevent_default()
        event.stop()


class TextualApp(App):
    CSS = """
    Screen { layout: vertical; }
    #alerts-heading, #watching-heading, #history-heading { height: 1; }
    #footer { height: auto; dock: bottom; background: $surface; }
    .modal-navigation { dock: bottom; height: auto; background: $surface; }
    #header { height: 2; }
    DataTable { height: 1fr; min-height: 1; scrollbar-size: 1 1; }
    #alerts, #watching { height: auto; }
    #watching, #watching-block { min-height: 12; }
    #watching-block { height: auto; }
    #idle, #idle-above { height: auto; display: none; }
    """
    BINDINGS = [
        Binding("m", "menu", "Menu", priority=True),
        Binding("up", "move(-1)", "Previous", priority=True),
        Binding("down", "move(1)", "Next", priority=True),
        Binding("tab", "section", "Next section", priority=True),
        Binding("n", "needs_you", "Needs you", priority=True),
        Binding("enter", "open_detail", "Open", priority=True),
        Binding("o", "open_url", "GitHub", priority=True),
        Binding("r", "wake", "Check now", priority=True),
        Binding("t", "alert_action", "Retry alert", priority=True),
        Binding("c", "copy_fix", "Copy fix", priority=True),
        Binding("q,ctrl+c", "quit", "Quit", priority=True),
        Binding("question_mark", "help", "Help", priority=True),
        Binding("1", "screen(1)", "Overview", priority=True),
        Binding("2", "screen(2)", "Repositories", priority=True),
        Binding("3", "screen(3)", "Agent", priority=True),
        Binding("L", "logs", "State folder", priority=True),
    ]
    SECTIONS = ("alerts", "watching", "history")

    def __init__(self, observer, worker, *, opener=None, clock=None, clipboard=None, folder_opener=open_folder):
        super().__init__()
        self.observer, self.worker = observer, worker
        self.opener = opener if opener is not None else webbrowser.open
        self.folder_opener = folder_opener
        self.copy_command = clipboard if clipboard is not None else self._copy_command
        self.clock = clock if clock is not None else lambda: time.monotonic()
        self.ascii = (os.environ.get("LIGHTKEEPER_ASCII") == "1"
                      or (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8")
        self._render_failed = False
        self._last_snapshot = None
        self._row_width = None
        self._idle_layout = None
        self._idle_compact_key = None
        self._idle_visible = False
        self._idle_interval = 1 / 10
        self.rows = []  # (section, row); index is the one cursor across all three sections
        self.cursor = 0
        self.help_visible = False
        self.message = ""
        self.expanded_alerts = set()
        self._disk_jobs = deque()
        self._disk_busy = False
        self._screen_settings = None
        self._screen_revision = 0
        self._screen_source = None
        self._screen_snapshot = None

    @property
    def snapshot(self):
        snapshot = self.observer.snapshot
        if self._screen_settings is not None:
            if snapshot.runtime.screen_revision != self._screen_revision:
                self._screen_settings = None
                self._screen_source = self._screen_snapshot = None
            else:
                if snapshot is not self._screen_source:
                    self._screen_source = snapshot
                    self._screen_snapshot = replace(snapshot, runtime=replace(
                        snapshot.runtime, **asdict(self._screen_settings)))
                return self._screen_snapshot
        return snapshot

    def apply_screen_settings(self, settings):
        # Preview the saved display values until the runner adopts them at its next boundary.
        # The observer and review authority remain owned by the runner thread.
        self._screen_settings = settings
        self._screen_revision = self.observer.snapshot.runtime.screen_revision
        self._screen_source = self._screen_snapshot = None
        self._last_snapshot = None
        self._idle_compact_key = None
        self.refresh_snapshot()

    def action_menu(self):
        if isinstance(self.screen, Menu):
            self.screen.action_cancel()
        else:
            self.push_screen(Menu(), self.menu_selected)

    def dialog_open(self):
        """A form or confirmation is somewhere in the stack; navigation would discard it."""
        return any(isinstance(screen, ModalScreen) and not isinstance(screen, Menu) for screen in self.screen_stack)

    def menu_selected(self, key):
        if key in ("overview", "repositories", "agent", "settings.toml", "rules.md", "template.md") and self.dialog_open():
            self.show_message("Close the current dialog before navigating")
            return
        if key in ("overview", "repositories", "agent"):
            self.action_screen(("overview", "repositories", "agent").index(key) + 1)
        elif key in ("wake", "check_updates"):
            self.send_command(key)
            self.show_message(self.message or "check queued")
        elif key == "screen_settings":
            directory = self.snapshot.runtime.config_dir
            if not directory:
                self.show_message("config directory unavailable")
                return
            def loaded(result, error):
                if error:
                    self.show_message(error_text(error))
                else:
                    self.push_screen(ScreenForm(*result), lambda message: self.show_message(message) if message else None)
            self.run_disk(lambda: read_settings(directory), loaded)
        elif key in ("settings.toml", "rules.md", "template.md"):
            self.action_screen(3)
            self.call_after_refresh(self.screen.action_edit, key)
        elif key == "help":
            self.action_help()
        elif key == "quit":
            self.action_quit()

    def run_disk(self, operation, completed):
        """One app-owned worker queue for settings reads, saves and editor sessions."""
        self._disk_jobs.append((operation, completed))
        self._start_disk()

    def _start_disk(self):
        if self._disk_busy or not self._disk_jobs:
            return
        operation, completed = self._disk_jobs.popleft()
        self._disk_busy = True

        def work():
            try:
                result, error = operation(), None
            except Exception as exc:
                result, error = None, exc
            self.call_from_thread(finish, result, error)

        def finish(result, error):
            try:
                completed(result, error)
            finally:
                self._disk_busy = False
                self._start_disk()

        self.run_worker(work, thread=True, exclusive=True, group="settings-disk")

    def compose(self) -> ComposeResult:
        yield Static(text(), markup=False, id="header")
        for section in self.SECTIONS:
            if section == "watching":
                yield Static(text(), markup=False, id="idle-above")
            yield Static(text(), markup=False, id=f"{section}-heading")
            # DataTable has no markup switch: supplying only Text cells/labels bypasses markup.
            table = OverviewTable(id=section, show_header=False, show_row_labels=False,
                                  cursor_type="row", cursor_foreground_priority="renderable")
            if section == "watching":
                with Vertical(id="watching-block"):
                    yield table
                    yield Static(text(), markup=False, id="idle")
            else:
                yield table
        yield Static(text(), markup=False, id="footer")

    def on_mount(self):
        self.overview = self.screen
        self._idle_timer = self.set_interval(self._idle_interval, self.animate_idle, pause=True)
        self.screen_change_signal.subscribe(self, self.idle_screen_changed, immediate=True)
        self._refresh_timer = self.set_interval(0.25, self.refresh_snapshot)
        self.refresh_snapshot()

    def on_unmount(self):
        self._idle_visible = False
        for name in ("_refresh_timer", "_idle_timer"):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()

    def idle_screen_changed(self, screen):
        try:
            self.refresh_idle(self.snapshot, self.clock(),
                              self.size.width - self.overview.scrollbar_size_vertical, force=True)
        except Exception:
            self.render_error()

    def idle_rate(self, interval):
        if interval == self._idle_interval:
            return
        self._idle_interval = interval
        if not self.is_running:
            return
        if hasattr(self, "_idle_timer"):
            self._idle_timer.stop()
            self._idle_timer = self.set_interval(interval, self.animate_idle, pause=not self._idle_visible)

    def on_app_blur(self, event: events.AppBlur):
        self.idle_rate(1)

    def on_app_focus(self, event: events.AppFocus):
        self.idle_rate(1 / 10)

    def animate_idle(self):
        if not self._idle_visible:
            return
        try:
            self.refresh_idle(self.snapshot, self.clock(),
                              self.size.width - self.overview.scrollbar_size_vertical, force=True)
        except Exception:
            self.render_error()

    def check_action(self, action, parameters):
        if action == "help":
            return True
        if action == "menu":
            return not isinstance(self.focused, Input)
        return (not isinstance(self.focused, Input)
                and (action == "quit" or
                     (action in ("screen", "logs") and (not isinstance(self.screen, ModalScreen)
                                                        or (isinstance(self.screen, Menu) and not self.dialog_open()))) or
                     self.screen is getattr(self, "overview", None)))

    @property
    def selected(self):
        return self.rows[self.cursor] if self.rows else None

    @staticmethod
    def row_identity(entry):
        section, row = entry
        if section == "alerts":
            return section, row.key
        # Distinguish repeated history entries, while a Watching head change keeps selection.
        return (section, row.item.identity, row.item.head, row.at) if section == "history" else (
            section, row.item.identity)

    def rebuild_rows(self, screen, now, width):
        previous = self.selected
        identity = self.row_identity(previous) if previous else None
        watching = sorted(screen.watching, key=lambda r: (not bool(r.needs_you), r.state != "reviewing", r.since))
        groups = (screen.alerts, watching, screen.history)
        self.rows = [(section, row) for section, group in zip(self.SECTIONS, groups) for row in group]
        identities = [self.row_identity(entry) for entry in self.rows]
        if identity in identities:
            self.cursor = identities.index(identity)
        else:
            self.cursor = min(self.cursor, max(0, len(self.rows) - 1))
        for section, group in zip(self.SECTIONS, groups):
            table = self.query_one(f"#{section}", DataTable)
            table.clear(columns=True)
            heading = self.query_one(f"#{section}-heading", Static)
            heading.update(text(f" {section.upper()} {len(group)}", "bold"))
            heading.display = table.display = bool(group) or section != "alerts"
            if section == "watching" and not group:
                table.display = False
                continue
            if not group and section != "alerts":
                widths = [max(1, width - 3)]
                cells = [[text("no reviews yet today", "dim")]]
            elif section == "alerts":
                widths, cells = [max(1, width - 3)], [[self.alert_text(row, screen, now)] for row in group]
            else:
                widths, cells = row_data(screen, group, now, width, self.ascii, section == "history")
                # DataTable needs concrete widths; why takes the remaining space, as in Rich.
                widths = [max(1, width - 1 - sum(w or 0 for w in widths) - 2 * len(widths))
                          if w is None else w for w in widths]
            for index, cell_width in enumerate(widths):
                table.add_column(text(), key=str(index), width=cell_width)
            for row_index, row_cells in enumerate(cells):
                for index, (cell, cell_width) in enumerate(zip(row_cells, widths)):
                    if section != "alerts":
                        cell.truncate(cell_width, overflow="ellipsis")
                    if group and section != "alerts" and ((section == "history" and index == 0)
                                                or (section == "watching" and index == len(widths) - 1)):
                        cell.pad_left(max(0, cell_width - cell.cell_len))
                table.add_row(*row_cells, key=str(row_index), height=None if section == "alerts" else 1)
        self.refresh_idle(screen, now, width, force=True)
        self.show_selection(focus=not isinstance(self.focused, Input))

    def refresh_idle(self, screen, now, width, *, force=False):
        panels = self.overview.query("#idle, #idle-above")
        if not panels:  # Children unmount before the app's own Unmount event.
            self._idle_timer.pause()
            self._idle_visible = False
            return
        above = self.overview.query_one("#idle-above", Static)
        inside = self.overview.query_one("#idle", Static)
        always = screen.runtime.idle == "always"
        panel = above if always else inside
        # Header, both headings, one History row, footer, and the Watching minimum.
        used = 5 + max(2, self.overview.query_one("#footer").size.height)
        used += max(12, len(screen.watching))
        if screen.alerts:
            used += 1 + sum(len(self.alert_text(alert, screen, now).plain.splitlines())
                            for alert in screen.alerts)
        free_rows = self.size.height - used
        if always:
            show = free_rows >= 3  # Needs rows of its own above Watching.
        else:
            # Quiet draws inside Watching's reserved 12 rows, which exist either way, so the
            # full picture always fits there; 14 is idle_panel's full-picture threshold.
            show = not screen.watching
            free_rows = 14
        above.display = show and always
        inside.display = show and not always
        self.overview.query_one("#watching").display = bool(screen.watching) or always
        visible = panel.display and self.screen is self.overview
        if visible != self._idle_visible:
            self._idle_visible = visible
            (self._idle_timer.resume if visible else self._idle_timer.pause)()
            force = True
        if not visible:
            self._idle_compact_key = None
            return
        layout = (width, free_rows, always)
        compact_key = (layout, int(now), screen.runtime.tempo, screen.runtime.clock, screen.runtime.moon, screen.runtime.events) if width < 60 or free_rows < 14 else None
        if compact_key is not None and compact_key == self._idle_compact_key and not self._render_failed:
            return
        if force or layout != self._idle_layout or self._render_failed or compact_key is not None:
            panel.update(idle_panel(screen, now, width, self.ascii,
                                    free_rows=free_rows, no_color=self.no_color,
                                    look=Look(tempo=screen.runtime.tempo, daytime=screen.runtime.clock,
                                              moon=screen.runtime.moon, events=screen.runtime.events)))
            self._idle_layout = layout
            self._idle_compact_key = compact_key

    def alert_text(self, alert, screen, now):
        message = alert.text
        if alert.key == "reviewer_paused" and screen.runtime.reviewer_paused_until:
            message += f"; resumes in {countdown(screen.runtime.reviewer_paused_until - now)}"
        if alert.key in self.expanded_alerts:
            action = self.alert_command(alert)
            attempt = next(((at, result) for key, at, result in screen.runtime.alert_attempts if key == alert.key), None)
            last = f"{age(now - attempt[0])} ago · {attempt[1]}" if attempt else "not attempted"
            message += (f"\n  cause: {alert.text}\n  scope: {alert.scope} · since {age(now - alert.since)} ago"
                        f"\n  last attempt and result: {last}\n  action: {action[2] if action else 'none'}")
            fix = self.fix_command(alert)
            if fix:
                message += f" · c copy: {fix}"
            if alert.key == "unknown_write":
                affected = ", ".join(f"{i.repo}#{i.number} @{i.head[:7]}" for i in screen.unknown_items)
                message += f"\n  affected: {affected or 'none'} · reconciled automatically when GitHub confirms; otherwise left to you"
        render = multiline if alert.key in self.expanded_alerts else text
        return render(f" {'^' if self.ascii else '▲'} {message}", "red" if alert.level == "error" else "yellow")

    @staticmethod
    def alert_command(alert):
        if alert.key in ("engine_identity", "reviewer_paused"):
            return "probe_engine", None, "t engine probe"
        if alert.key == "gh_identity":
            return "wake", None, "t gh re-check"
        if alert.key.startswith("quarantined") and alert.repo:
            return "check_repo", (alert.repo,), "t repository re-check"
        return None

    def fix_command(self, alert):
        identity = self.snapshot.runtime.engine_identity
        if alert.fix_command:
            return alert.fix_command
        if alert.key in ("engine_identity", "reviewer_paused") and identity:
            return identity.fix_command or ""
        return ""

    def refresh_timed_cells(self, screen, now):
        since_snapshot = max(0, now - screen.mono_at) if screen.mono_at is not None else 0
        self.refresh_idle(screen, now, self.size.width - self.overview.scrollbar_size_vertical)
        indices = dict.fromkeys(self.SECTIONS, 0)
        for section, row in self.rows:
            table = self.query_one(f"#{section}", DataTable)
            key = str(indices[section])
            indices[section] += 1
            if section == "alerts":
                table.update_cell(key, "0", self.alert_text(row, screen, now))
                continue
            if row.state == "reviewing":
                table.update_cell(key, "1", glyph(row.state, now, self.ascii))
            if section == "watching":
                seconds = (now - screen.runtime.phase_since if row.state == "reviewing" else
                           screen.wall_now - row.since + since_snapshot)
                column = table.ordered_columns[-1]
                cell = text(age(seconds), "dim")
                cell.truncate(column.width, overflow="ellipsis")
                cell.pad_left(max(0, column.width - cell.cell_len))
                table.update_cell(key, column.key, cell)

    def show_selection(self, *, focus=True):
        for section in self.SECTIONS:
            table = self.query_one(f"#{section}", DataTable)
            active = bool(self.selected and self.selected[0] == section)
            table.show_cursor = active
            if active:
                index = sum(entry[0] == section for entry in self.rows[:self.cursor])
                table.move_cursor(row=index)
                if focus:
                    table.focus()

    def refresh_snapshot(self):
        if not self.is_running:
            return
        overview = getattr(self, "overview", None)
        header = overview.query("#header").first(Static) if overview and overview.query("#header") else None
        if header is None:
            return
        try:
            if self.screen is not overview:
                for screen in self.screen_stack:
                    if not screen.is_mounted:
                        continue
                    if isinstance(screen, PRDetail):
                        screen.refresh_detail()
                    elif isinstance(screen, ConfirmOwnerApprove):
                        screen.refresh_confirmation()
                    elif isinstance(screen, (Repositories, RepoForm, AgentPanel, ScreenForm)):
                        screen.refresh_panel()
                self._render_failed = False
                if self.worker.poll_join():
                    self.exit(self.worker.exit_code, return_code=self.worker.exit_code)
                return
            snapshot = self.snapshot
            screen = snapshot
            if self.worker.runner.cancelled and screen.runtime.phase == "writing":
                screen = replace(screen, runtime=replace(screen.runtime, phase="finishing"))
            now, width = self.clock(), self.size.width - overview.scrollbar_size_vertical
            queued = self.queued_text(screen)
            header.update(header_lines(screen, now, width, self.ascii, queued))
            # Account for the Overview scrollbar when the fixed blocks exceed the height.
            if snapshot is not self._last_snapshot or width != self._row_width:
                self.rebuild_rows(screen, now, width)
                self._last_snapshot, self._row_width = snapshot, width
            else:
                self.refresh_timed_cells(screen, now)
            self.update_footer(screen)
            self._render_failed = False
        except Exception:
            self.render_error()
        if self.worker.poll_join():
            self.exit(self.worker.exit_code, return_code=self.worker.exit_code)

    def render_error(self):
        if not self._render_failed:
            logging.getLogger("lightkeeper").exception("render error")
        self._render_failed = True
        try:
            self.overview.query_one("#footer", Static).update(text("render error (see run.log)", "red"))
        except Exception:
            pass  # Teardown may have removed the footer too.

    def queued_text(self, screen):
        if self.worker.runner.commands.empty():
            return ""
        item = screen.runtime.current_item
        return "queued" + (", after " + label(item, screen.runtime.repos) if item else "")

    def update_footer(self, screen=None):
        screen = screen or self.snapshot
        hints = ("↑↓ move · Tab section · n needs you · Enter detail/run/alert · o GitHub · r check now"
                 + (" · t retry · c copy fix" if self.help_visible else ""))
        message = " · ".join(v for v in (self.message, screen.runtime.last_command.text
                                         if screen.runtime.last_command else "") if v)
        self.query_one("#footer", Static).update(footer_text(1, hints, message))

    def action_move(self, delta):
        self.cursor = max(0, min(self.cursor + delta, len(self.rows) - 1))
        self.show_selection()

    def action_section(self):
        current = self.SECTIONS.index(self.selected[0]) if self.selected else -1
        for offset in range(1, 4):
            section = self.SECTIONS[(current + offset) % 3]
            for i, entry in enumerate(self.rows):
                if entry[0] == section:
                    self.cursor = i
                    self.show_selection()
                    return

    def action_needs_you(self):
        for offset in range(1, len(self.rows) + 1):
            index = (self.cursor + offset) % len(self.rows)
            section, row = self.rows[index]
            if section == "watching" and row.needs_you:
                self.cursor = index
                self.show_selection()
                return

    def action_open_url(self):
        if self.selected and self.selected[0] != "alerts":
            url = self.selected[1].item.url
            self.run_worker(lambda: self.open_url_in_thread(url), thread=True)

    def open_url_in_thread(self, url):
        try:
            opened = self.opener(url)
        except Exception:
            opened = False
        if opened is False:
            self.call_from_thread(self.open_url_failed)

    def open_url_failed(self):
        self.message = "Could not open GitHub URL"
        if isinstance(self.screen, PRDetail):
            self.screen.message = self.message
            self.screen.refresh_detail()
        elif self.screen is self.overview:
            self.update_footer()

    def open_folder_in_thread(self, path):
        try:
            self.folder_opener(path)
        except (OSError, subprocess.SubprocessError):
            self.call_from_thread(self.show_message, f"Could not open session folder: {path}")

    def _copy_command(self, value):
        return system_clipboard(value, self.copy_to_clipboard)

    def show_message(self, message):
        self.message = message
        if isinstance(self.screen, PRDetail):
            self.screen.message = message
            self.screen.refresh_detail()
        elif isinstance(self.screen, (Repositories, RepoForm, AgentPanel, ScreenForm)):
            self.screen.message = message
            self.screen.refresh_panel()
        elif self.screen is self.overview:
            self.update_footer()
        elif isinstance(self.screen, ModalScreen):
            for footer in self.screen.query(".modal-navigation"):
                footer.update(footer_text(0, "Esc back", message))

    def action_open_detail(self):
        if not self.selected:
            return
        section, row = self.selected
        if section != "alerts" and row.item.kind != "pr":
            self.action_open_url()
        elif section == "alerts":
            self.expanded_alerts.symmetric_difference_update({row.key})
            self._last_snapshot = None
            self.refresh_snapshot()
        else:
            self.push_screen(PRDetail(row))

    def action_alert_action(self):
        if self.selected and self.selected[0] == "alerts":
            action = self.alert_command(self.selected[1])
            if action:
                self.send_command(action[0], action[1])

    def action_copy_fix(self):
        if self.selected and self.selected[0] == "alerts":
            command = self.fix_command(self.selected[1])
            if command:
                def copy():
                    try:
                        copied = self.copy_command(command)
                        message = command if copied is False else "Fix command copied"
                    except (OSError, subprocess.SubprocessError):
                        message = f"Could not copy: {command}"
                    self.call_from_thread(self.show_message, message)
                self.run_worker(copy, thread=True)

    def action_help(self):
        if self.screen is not self.overview:
            bindings = self.screen.BINDINGS
            help_text = " · ".join(f"{binding.key} {binding.description}" for binding in bindings
                                   if isinstance(binding, Binding))
            self.notify(help_text + " · ? help", title="Help", timeout=10)
            return
        self.message = ""
        self.help_visible = not self.help_visible
        self.update_footer()

    def action_screen(self, number):
        while self.screen is not self.overview:
            self.pop_screen()
        if number == 2:
            self.push_screen(Repositories())
        elif number == 3:
            self.push_screen(AgentPanel())

    def action_logs(self):
        path = self.snapshot.runtime.state_dir
        if path:
            self.run_worker(lambda: self.open_folder_in_thread(path), thread=True)

    def action_quit(self):
        self.worker.request_stop()
        self.refresh_snapshot()

    def action_wake(self):
        self.send_command("wake")
        self.refresh_snapshot()

    def send_command(self, kind, key=None, head="", note=""):
        command = Command(uuid4().hex, kind, key, head, note)
        queued = False
        try:
            if kind == "check_repo":
                command_id = self.worker.runner.commands.put_check(command)
                self.message = ""
                return command_id
            queued = self.worker.runner.commands.put(command)
            self.message = "" if queued else "already queued"
        except queue.Full:
            self.message = "command inbox full"
        for screen in self.screen_stack:
            if isinstance(screen, PRDetail):
                screen.message = self.message
        return command.id if queued else None

    def print_summary(self):
        print_summary(self.snapshot, Console(markup=False, highlight=False), self.ascii)
