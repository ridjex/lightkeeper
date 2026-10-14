"""Repository settings UI. Disk writes are atomic; access checks belong to the Runner."""
from dataclasses import asdict
from pathlib import Path
import re

from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static

from ..settings import ACTIONS, RepoSettings, SettingsError, load_settings, save_settings, settings_digest
from .editing import edit_file, nonempty_utf8
from .rich_view import footer_text, header_lines, multiline as text

SAVED = "saved; the Runner re-checks before each write"


def read_settings(config):
    digest = settings_digest(config)
    settings = load_settings(config, include_text=False)
    if digest != settings_digest(config):
        raise SettingsError("settings.toml", "conflict", "settings changed on disk; reopen the form")
    return settings, digest


class SettingsCache:
    def __init__(self):
        self.value = None
        self.stamp = None
        self.profile = "default"

    def read(self, config):
        path = Path(config) / "settings.toml"
        stamp = path.stat().st_mtime_ns if path.exists() else None
        digest = settings_digest(config)
        if self.value is None or (stamp, digest) != self.stamp:
            value = read_settings(config)
            profile = value[0].resolved_profile
            self.profile = Path(profile).name if profile else "default"
            self.value, self.stamp = value, (stamp, value[1])
        return self.value


def error_text(exc):
    return "settings changed on disk; reopen the form" if isinstance(exc, SettingsError) and exc.key == "conflict" else str(exc)


def open_count(app, repo):
    return sum(row.item.repo == repo for row in app.snapshot.watching)


class Confirm(ModalScreen):
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm VerticalScroll { width: 64; height: auto; max-height: 90%; border: solid yellow; padding: 1 2; background: $surface; }
    Confirm Static { height: auto; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self):
        with VerticalScroll():
            yield Static(text(self.message), markup=False)
            yield Button("Confirm", id="confirm", variant="warning")
            yield Button("Cancel", id="cancel")
        yield Static(footer_text(2, "Tab choose · Enter select · Esc cancel"), markup=False, classes="modal-navigation")

    def action_cancel(self):
        self.dismiss(False)

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "confirm")


class Repositories(Screen):
    DEFAULT_CSS = """
    Repositories #repos-header { height: 2; }
    Repositories #repos-footer { height: auto; }
    """
    BINDINGS = [Binding("escape", "back", "Back"), Binding("space", "toggle", "Pause/resume", priority=True),
                Binding("enter", "edit", "Edit", priority=True), Binding("a", "add", "Add"),
                Binding("n", "notes", "Notes")]

    def __init__(self):
        super().__init__()
        self.message = ""
        self.settings = None
        self.digest = ""
        self._table_state = None
        self.cache = SettingsCache()
        self._reading = False
        self._load_error = None

    def compose(self):
        yield Static(text(), markup=False, id="repos-header")
        yield DataTable(id="repos-table", cursor_type="row")
        yield Static(text(), markup=False, id="repos-footer")

    def on_mount(self):
        self.refresh_panel()
        self.query_one(DataTable).focus()

    def check_action(self, action, parameters):
        return not isinstance(self.focused, Input)

    @property
    def config(self):
        directory = self.app.snapshot.runtime.config_dir
        if not directory:
            raise SettingsError("settings.toml", "directory", "config directory unavailable")
        return Path(directory)

    @property
    def selected(self):
        table = self.query_one(DataTable)
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value if table.row_count else None

    def refresh_panel(self, *, read=True):
        if read and not self._reading:
            self._reading = True
            def loaded(result, error):
                self._reading = False
                if not self.is_mounted:
                    return
                if error:
                    self._load_error = error_text(error)
                else:
                    self.settings, self.digest = result
                    self._load_error = None
                self.refresh_panel(read=False)
            self.app.run_disk(lambda: self.cache.read(self.config), loaded)
        snapshot = self.app.snapshot
        rt = snapshot.runtime
        self.query_one("#repos-header", Static).update(header_lines(
            snapshot, self.app.clock(), self.size.width, self.app.ascii, self.app.queued_text(snapshot)))
        if self.settings:
            state = (self.digest, rt.dry_repos, rt.repo_checks, rt.mode, rt.live_lock_held)
            if state != self._table_state:
                table = self.query_one(DataTable)
                selected = self.selected
                table.clear(columns=True)
                table.add_columns(*(text(s) for s in ("enabled", "repo", "effective post", "remarks", "allowed", "deployments", "access")))
                checks = dict(rt.repo_checks)
                for name, repo in self.settings.repos.items():
                    allowed = text()
                    for letter, action in zip("ARC", ACTIONS):
                        allowed.append(letter + " ", style="" if action in repo.allowed else "dim")
                    mode = "live" if (repo.enabled and repo.post == "live" and name not in rt.dry_repos
                                      and rt.mode == "live" and rt.live_lock_held) else "dry-run"
                    table.add_row(text("on" if repo.enabled else "off"), text(name), text(mode), text(repo.remarks),
                                  allowed, text("on" if repo.deployments else "off"), text(checks.get(name, "unchecked")), key=name)
                names = list(self.settings.repos)
                if selected in names:
                    table.move_cursor(row=names.index(selected))
                self._table_state = state
        self.query_one("#repos-footer", Static).update(footer_text(
            2, "Space pause/resume · Enter edit · a add · n notes · Esc back", self._load_error or self.message))

    def action_back(self):
        self.app.pop_screen()

    def action_toggle(self):
        name = self.selected
        if name and self.settings:
            config, digest = self.config, self.digest
            patch = {"repos": {name: {"enabled": not self.settings.repo(name).enabled}}}
            def saved(result, error):
                if error is None:
                    self.app.send_command("wake")
                if self.is_mounted:
                    self.message = error_text(error) if error else SAVED
                    self.refresh_panel()
            self.app.run_disk(lambda: save_settings(config, patch, digest), saved)

    def open_form(self, name=None):
        def loaded(result, error):
            if not self.is_mounted:
                return
            if error:
                self._load_error = error_text(error)
                self.refresh_panel(read=False)
            else:
                settings, digest = result
                self.app.push_screen(RepoForm(settings, digest, name), self.form_closed)
        self.app.run_disk(lambda: self.cache.read(self.config), loaded)

    def form_closed(self, message):
        if message:
            self.message = message
        self.refresh_panel()

    def action_edit(self):
        if self.selected:
            self.open_form(self.selected)

    def action_add(self):
        self.open_form()

    def action_notes(self):
        name = self.selected
        if name:
            app, config = self.app, self.config
            path = config / "repos" / (name.replace("/", "__") + ".md")
            def edited(result, error):
                message = error_text(error) if error else result
                if message is None:
                    app.send_command("wake")
                if self.is_mounted:
                    self.message = message or f"{SAVED}; re-evaluates {open_count(app, name)} open items"
                    self.refresh_panel()
            app.run_disk(lambda: edit_file(app, path, nonempty_utf8, config_dir=config), edited)


class RepoCheckbox(Checkbox):
    """Explicit marks stay readable in dark, monochrome and ASCII terminals."""
    def render(self):
        return Content.assemble(
            ("[X]" if self.value else "[ ]", self.get_visual_style("toggle--button")),
            self.label.pad(1, 1).stylize_before(self.get_visual_style("toggle--label")))


class RepoForm(ModalScreen):
    DEFAULT_CSS = """
    RepoForm { align: center middle; }
    RepoForm #repo-form { width: 76; max-width: 100%; height: auto; max-height: 90%; border: solid $accent; padding: 1 2; background: $surface; }
    RepoForm Static { height: auto; }
    RepoForm Horizontal { height: auto; }
    RepoForm Checkbox { width: auto; height: 1; border: none; padding: 0 1; color: $text; }
    RepoForm Checkbox > .toggle--button { color: $text; background: $surface; }
    RepoForm Checkbox.-on > .toggle--button { color: $text-success; text-style: bold; }
    RepoForm .field-help { color: $text-muted; }
    RepoForm #form-navigation { dock: bottom; height: auto; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("A", "toggle('approve')", "Approve"),
                Binding("R", "toggle('request_changes')", "Request changes"), Binding("C", "toggle('comment')", "Comment")]

    def __init__(self, settings, digest, name=None):
        super().__init__()
        self.settings, self.digest, self.repo_name = settings, digest, name
        self.original = settings.repo(name) if name else RepoSettings("")
        self.live_confirmed = self.original.post == "live"
        self.pending = None
        self.pending_changes = None
        self.pending_since = None
        self.saving = False
        self.message = ""

    def compose(self):
        repo = self.original
        with VerticalScroll(id="repo-form"):
            yield Static(text("Repository · org/name (new repositories are checked before saving)"), markup=False)
            yield Input(repo.name, id="repo-name", disabled=self.repo_name is not None)
            yield Static(text("Enabled"), markup=False)
            yield RepoCheckbox("Enabled · watch and review this repository", repo.enabled, id="enabled")
            yield Static(text("Post"), markup=False)
            yield Select([("dry-run", "dry-run"), ("live", "live")], value=repo.post, allow_blank=False, id="post")
            yield Static(text("live posts reviews; dry-run only records what would be posted"), markup=False, classes="field-help")
            yield Static(text("Remarks"), markup=False)
            yield Select([(v, v) for v in ("blockers", "important", "all")], value=repo.remarks, allow_blank=False, id="remarks")
            yield Static(text("blockers only · blockers and important · everything"), markup=False, classes="field-help")
            yield Static(text("Allowed actions"), markup=False)
            with Horizontal():
                for label, action in zip(("Approve (A)", "Request changes (R)", "Comment (C)"), ACTIONS):
                    yield RepoCheckbox(label, action in repo.allowed, id=action)
            yield Static(text("Deployments"), markup=False)
            yield RepoCheckbox("Deployments · review deployment gates", repo.deployments, id="deployments")
            yield Static(text(), markup=False, id="form-footer")
            with Horizontal():
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")
                if self.repo_name:
                    yield Button("Remove", id="remove", variant="error")

        yield Static(footer_text(2, "A approve · R request changes · C comment · Esc cancel"), markup=False, id="form-navigation")

    def check_action(self, action, parameters):
        return not isinstance(self.focused, Input)

    def on_mount(self):
        self.refresh_panel()

    def refresh_panel(self):
        if self.pending:
            results = self.app.snapshot.runtime.command_results
            result = next((r for r in results if r.id == self.pending), None)
            if result:
                self.pending = None
                if result.outcome == "done" and result.text.startswith("access ok"):
                    name, changes = self.pending_changes
                    self.persist(name, changes)
                    return
                self.message = result.text
                self.set_busy(False)
            elif self.app.clock() - self.pending_since >= 30:
                self.pending = None
                self.pending_changes = None
                self.message = "check result lost; retry"
                self.set_busy(False)
        restart = ("restart required for live" if self.query_one("#post", Select).value == "live"
                   and not self.app.snapshot.runtime.live_lock_held else "")
        self.query_one("#form-footer", Static).update(text("; ".join(v for v in (self.message, restart) if v)))

    def set_busy(self, busy):
        for widget in self.query("Input, Select, Checkbox, #save"):
            widget.disabled = busy or (widget.id == "repo-name" and self.repo_name is not None)

    def action_cancel(self):
        self.dismiss(None)

    def action_toggle(self, action):
        if not self.pending and not self.saving:
            self.query_one(f"#{action}", Checkbox).toggle()

    def on_select_changed(self, event):
        if event.select.id == "post" and event.value == "dry-run":
            self.live_confirmed = False
        if event.select.id == "post" and event.value == "live" and not self.live_confirmed:
            message = "Switch to live posting?"
            if not self.app.snapshot.runtime.live_lock_held:
                message += "\nrestart required for live"
            self.app.push_screen(Confirm(message), self.confirm_live)
        self.refresh_panel()

    def confirm_live(self, confirmed):
        self.live_confirmed = confirmed
        if not confirmed:
            self.query_one("#post", Select).value = "dry-run"
        self.refresh_panel()

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "save":
            self.save()
        elif event.button.id == "remove":
            self.app.push_screen(Confirm(f"Remove {self.repo_name}? Journal history is kept."), self.confirm_remove)

    def confirm_remove(self, confirmed):
        if confirmed:
            self.persist(self.repo_name, None)

    def save(self):
        if self.pending or self.saving:
            return
        post = self.query_one("#post", Select).value
        if post == "live" and not self.live_confirmed:
            self.message = "confirm live posting first"
            self.refresh_panel()
            return
        name = self.repo_name or self.query_one("#repo-name", Input).value.strip()
        values = dict(enabled=self.query_one("#enabled", Checkbox).value, post=post,
                      remarks=self.query_one("#remarks", Select).value,
                      allowed=tuple(a for a in ACTIONS if self.query_one(f"#{a}", Checkbox).value),
                      deployments=self.query_one("#deployments", Checkbox).value)
        if self.repo_name:
            changes = {k: v for k, v in values.items() if v != asdict(self.original)[k]}
            if not changes:
                self.dismiss(None)
                return
            self.persist(name, changes)
        else:
            if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", name) or name.split("/")[-1] in (".", ".."):
                self.message = "repository must be org/name"
            elif name in self.settings.repos:
                self.message = "repository already configured; reopen its form"
            else:
                self.pending = self.app.send_command("check_repo", (name,))
                if self.pending:
                    self.pending_since = self.app.clock()
                    self.pending_changes = (name, values)
                    self.message = "checking access…"
                    self.set_busy(True)
                else:
                    self.message = self.app.message
            self.refresh_panel()

    def persist(self, name, changes):
        self.saving = True
        self.set_busy(True)
        app = self.app
        def saved(result, error):
            self.saving = False
            if error is None:
                app.send_command("wake")
            if not self.is_mounted:
                return
            if error:
                self.message = error_text(error)
                self.set_busy(False)
                self.refresh_panel()
                return
            message = SAVED
            if changes and "remarks" in changes and changes["remarks"] != self.original.remarks:
                message += f"; re-evaluates {open_count(app, name)} open items"
            if changes and changes.get("post") == "live" and not app.snapshot.runtime.live_lock_held:
                message += "; restart required for live"
            self.dismiss(message)
        app.run_disk(lambda: save_settings(self.settings.config_dir, {"repos": {name: changes}}, self.digest), saved)
