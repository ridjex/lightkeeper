"""Compact engine/identity panel; all probes go through the Runner."""
from pathlib import Path
import subprocess

from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, Static

from .. import __version__
from ..updates import UPGRADE
from ..settings import validate_settings_file, validate_template
from .editing import nonempty_utf8
from .editor import open_editor
from .repos import SettingsCache, error_text
from .rich_view import age, footer_text, header_lines, multiline as text


class AgentPanel(Screen):
    DEFAULT_CSS = """
    AgentPanel #agent-header { height: 2; }
    AgentPanel #agent-body, AgentPanel #agent-footer { height: auto; }
    """
    BINDINGS = [Binding("escape", "back", "Back"), Binding("t", "probe", "Probe"),
                Binding("c", "copy_fix", "Copy fix"), Binding("e", "edit('settings.toml')", "Settings"),
                Binding("r", "edit('rules.md')", "Rules"), Binding("T", "edit('template.md')", "Template")]

    def __init__(self):
        super().__init__()
        self.message = ""
        self.probe_id = None
        self.cache = SettingsCache()
        self.settings = None
        self._reading = False
        self._load_error = None

    def compose(self):
        yield Static(text(), markup=False, id="agent-header")
        with VerticalScroll():
            yield Static(text(), markup=False, id="agent-body")
        yield Static(text(), markup=False, id="agent-footer")

    def on_mount(self):
        self.refresh_panel()

    def check_action(self, action, parameters):
        return not isinstance(self.focused, Input)

    def refresh_panel(self, *, read=True):
        directory = self.app.snapshot.runtime.config_dir
        if read and directory and not self._reading:
            self._reading = True
            def loaded(result, error):
                self._reading = False
                if not self.is_mounted:
                    return
                self._load_error = error_text(error) if error else None
                if not error:
                    self.settings = result[0]
                self.refresh_panel(read=False)
            self.app.run_disk(lambda: self.cache.read(directory), loaded)
        snapshot = self.app.snapshot
        rt = snapshot.runtime
        if self.probe_id:
            result = next((r for r in rt.command_results if r.id == self.probe_id), None)
            if result:
                self.message, self.probe_id = result.text, None
        self.query_one("#agent-header", Static).update(header_lines(
            snapshot, self.app.clock(), self.size.width, self.app.ascii, self.app.queued_text(snapshot)))
        profile = self.cache.profile if self.settings else rt.engine_profile
        identity = rt.engine_identity
        auth = (f"{'ok' if identity.ok else 'failed'} · {identity.account or ''} · {identity.detail}"
                if identity else "unchecked")
        fix = identity.fix_command if identity else None
        self.query_one("#agent-body", Static).update(text(
            f"lightkeeper {__version__}"
            + (f" · update {rt.update_version} available: {UPGRADE}" if rt.updates and rt.update_version else "")
            + "\n"
            + f"Engine: {rt.engine_name}\nModel: {rt.model}\nEffort: {rt.effort}\nProfile: {profile}\n"
            f"Auth: {auth}\nFix: {fix or 'none'}\n"
            f"gh: {rt.gh_login or 'unchecked'} · expected: {rt.login or 'unconfigured'}\n"
            f"Config dir: {rt.config_dir}\nState dir: {rt.state_dir}\n"
            f"Notifications: {'on' if rt.notifications else 'off'}\n"
            f"Uptime: {age(max(0, self.app.clock() - rt.started_at))}"))
        result = rt.last_command.text if rt.last_command else ""
        self.query_one("#agent-footer", Static).update(footer_text(
            3, "t probe · c copy fix · e settings · r rules · T template · Esc back",
            self._load_error or self.message or result))

    def action_back(self):
        self.app.pop_screen()

    def action_probe(self):
        self.probe_id = self.app.send_command("probe_engine")
        self.message = self.app.message or "probe queued"
        self.refresh_panel()

    def action_copy_fix(self):
        identity = self.app.snapshot.runtime.engine_identity
        if identity and identity.fix_command:
            app = self.app
            def copy():
                try:
                    copied = app.copy_command(identity.fix_command)
                    message = identity.fix_command if copied is False else "Fix command copied"
                except (OSError, subprocess.SubprocessError):
                    message = f"Could not copy: {identity.fix_command}"
                app.call_from_thread(app.show_message, message)
            self.run_worker(copy, thread=True)

    def action_edit(self, name):
        directory = self.app.snapshot.runtime.config_dir
        if not directory:
            self.message = "config directory unavailable"
            self.refresh_panel()
            return
        root = Path(directory)
        if name == "settings.toml":
            def validate(path):
                return validate_settings_file(path, root)
        elif name == "template.md":
            def validate(path):
                return validate_template(path.read_text(encoding="utf-8"))
        else:
            validate = nonempty_utf8
        app = self.app
        def edited(result, error):
            message = error_text(error) if error else result
            if message is None:
                app.send_command("wake")
            if self.is_mounted:
                self.message = message or "saved; the Runner re-checks before each write"
                self.refresh_panel()
        open_editor(app, root / name, validate, edited, config_dir=root)
