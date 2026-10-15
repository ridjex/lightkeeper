"""A plain-text editor; all file reads and validated saves use the app's disk queue."""
from pathlib import Path

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea

from .editing import edit_file, read_file, save_text
from .repos import Confirm, error_text
from .rich_view import multiline as text


def open_editor(app, path, validate, edited, *, config_dir):
    path = Path(path)

    def read():
        original = read_file(path)
        return original, (original or b"").decode("utf-8")

    def loaded(result, error):
        if error:
            edited(None, error)
        else:
            app.push_screen(Editor(path, *result, validate, config_dir),
                            lambda saved: edited(None, None) if saved else None)

    app.run_disk(read, loaded)


class Editor(ModalScreen):
    DEFAULT_CSS = """
    Editor { align: center middle; }
    Editor #editor-dialog { width: 100; max-width: 100%; height: 90%; border: solid $accent; background: $surface; }
    Editor #editor-title, Editor #editor-message { height: 1; }
    Editor TextArea { height: 1fr; }
    """
    BINDINGS = [Binding("ctrl+s", "save", "Save", priority=True),
                Binding("escape", "close", "Close", priority=True),
                Binding("ctrl+e", "external", "Open in $EDITOR", priority=True)]

    def __init__(self, path, original, content, validate, config_dir):
        super().__init__()
        self.path, self.original, self.initial_text = path, original, content
        self.validate, self.config_dir = validate, config_dir
        self.busy = False
        self.message = ""

    def compose(self):
        with Vertical(id="editor-dialog"):
            yield Static(text(self.path.name), markup=False, id="editor-title")
            # Tab moves focus in both formats: TOML needs no tab indentation either.
            yield TextArea(self.initial_text, soft_wrap=self.path.suffix == ".md",
                           tab_behavior="focus", show_line_numbers=False, id="editor-text")
            yield Static(text(), markup=False, id="editor-message")
            yield Static(text("Ctrl-S save · Esc close · Ctrl-E open in $EDITOR", "dim"),
                         markup=False, classes="modal-navigation")

    def on_mount(self):
        self.query_one(TextArea).focus()

    @property
    def dirty(self):
        return self.query_one(TextArea).text != self.initial_text

    def show_message(self, message):
        self.message = message
        self.query_one("#editor-message", Static).update(text(message))

    def action_close(self):
        if self.busy:
            return
        if self.dirty:
            self.app.push_screen(Confirm("Discard unsaved changes?"),
                                 lambda discard: self.dismiss(False) if discard else None)
        else:
            self.dismiss(False)

    def completed(self, result, error):
        self.busy = False
        self.query_one(TextArea).read_only = False
        message = error_text(error) if error else result
        if message is not None:
            self.show_message(message)
        else:
            self.dismiss(True)

    def action_save(self):
        if self.busy:
            return
        content = self.query_one(TextArea).text
        self.busy = True
        self.query_one(TextArea).read_only = True
        self.app.run_disk(lambda: save_text(self.path, content, self.original, self.validate,
                                          config_dir=self.config_dir), self.completed)

    def action_external(self):
        if self.busy:
            return
        if self.dirty:
            self.show_message("save or close first")
            return
        self.busy = True
        self.query_one(TextArea).read_only = True
        app = self.app
        app.run_disk(lambda: edit_file(app, self.path, self.validate, config_dir=self.config_dir), self.completed)
