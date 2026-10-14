"""Explicit navigation and a small form for display preferences."""
from dataclasses import asdict

from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, OptionList, Select, Static
from textual.widgets.option_list import Option

from ..settings import save_settings
from .repos import RepoCheckbox, error_text
from .rich_view import footer_text, multiline as text


class Menu(ModalScreen):
    DEFAULT_CSS = """
    Menu { align: center middle; }
    Menu OptionList { width: 48; height: auto; max-height: 90%; border: solid $accent; }
    """
    BINDINGS = [Binding("escape", "cancel", "Close")]
    ENTRIES = (
        ("overview", "Overview"), ("repositories", "Repositories"), ("agent", "Agent"),
        ("wake", "Check now"), ("screen_settings", "Screen settings"),
        ("settings.toml", "Edit settings.toml"), ("rules.md", "Edit rules.md"),
        ("template.md", "Edit template.md"), ("check_updates", "Check for updates now"),
        ("help", "Help"), ("quit", "Quit"),
    )

    def compose(self):
        yield OptionList(*(Option(label, id=key) for key, label in self.ENTRIES), id="menu-options")
        yield Static(footer_text(0, "↑↓ choose · Enter select · Esc close"), markup=False,
                     classes="modal-navigation")

    def action_cancel(self):
        self.dismiss(None)

    def on_option_list_option_selected(self, event):
        self.dismiss(event.option.id)


class ScreenForm(ModalScreen):
    DEFAULT_CSS = """
    ScreenForm { align: center middle; }
    ScreenForm #screen-form { width: 76; max-width: 100%; height: auto; max-height: 90%; border: solid $accent; padding: 1 2; background: $surface; }
    ScreenForm Static, ScreenForm Horizontal { height: auto; }
    ScreenForm Checkbox { width: auto; height: 1; border: none; padding: 0 1; color: $text; }
    ScreenForm .field-help { color: $text-muted; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    SWITCHES = (
        ("clock", "Use the local hour for the sky and sun or moon position."),
        ("moon", "Show the moon at night."),
        ("events", "Show occasional ships, whales and meteors."),
        ("updates", "Check for a newer lightkeeper release once per 24 hours."),
    )

    def __init__(self, settings, digest):
        super().__init__()
        self.settings, self.digest = settings, digest
        self.saving = False
        self.message = ""

    def compose(self):
        screen = self.settings.screen
        with VerticalScroll(id="screen-form"):
            yield Static(text("Screen settings"), markup=False)
            yield Static(text("Idle"), markup=False)
            yield Select([(s, s) for s in ("quiet", "always")], value=screen.idle, allow_blank=False, id="idle-setting")
            yield Static(text("quiet fills empty Watching; always puts the picture above Watching."), markup=False, classes="field-help")
            yield Static(text("Tempo"), markup=False)
            values = sorted({round(n / 10, 1) for n in range(2, 16)} | {screen.tempo})
            yield Select([(str(n), n) for n in values], value=screen.tempo, allow_blank=False, id="tempo")
            yield Static(text("Animation speed from 0.2 (slow) to 1.5 (fast), in steps of 0.1."), markup=False, classes="field-help")
            for key, help_text in self.SWITCHES:
                yield RepoCheckbox(key.capitalize(), getattr(screen, key), id=key)
                yield Static(text(help_text), markup=False, classes="field-help")
            yield Static(text(), markup=False, id="screen-message")
            with Horizontal():
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")
        yield Static(footer_text(0, "Tab choose · Enter select · Esc cancel"), markup=False,
                     classes="modal-navigation")

    def refresh_panel(self):
        self.query_one("#screen-message", Static).update(text(self.message))

    def action_cancel(self):
        if not self.saving:
            self.dismiss(None)

    def on_button_pressed(self, event):
        if event.button.id == "cancel":
            self.action_cancel()
        elif event.button.id == "save" and not self.saving:
            self.save()

    def save(self):
        values = {"idle": self.query_one("#idle-setting", Select).value,
                  "tempo": self.query_one("#tempo", Select).value}
        values.update((key, self.query_one(f"#{key}", Checkbox).value) for key, _ in self.SWITCHES)
        original = asdict(self.settings.screen)
        changes = {key: value for key, value in values.items() if value != original[key]}
        if not changes:
            self.dismiss("unchanged")
            return
        self.saving = True
        for control in self.query("Button, Select, Checkbox"):
            control.disabled = True
        app = self.app

        def saved(result, error):
            if error is None:
                app.apply_screen_settings(result.settings.screen)
                app.send_command("wake")
            if not self.is_mounted:
                return
            self.saving = False
            if error:
                for control in self.query("Button, Select, Checkbox"):
                    control.disabled = False
                self.message = error_text(error)
                self.refresh_panel()
            else:
                self.dismiss("screen settings saved")

        app.run_disk(lambda: save_settings(self.settings.config_dir, {"screen": changes}, self.digest), saved)
