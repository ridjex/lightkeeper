"""Full-screen, offline experiment kit for the dashboard's idle picture."""
from dataclasses import replace
from datetime import datetime, timezone
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from .idle import DEFAULT_LOOK, event_at, event_start
from .model import Runtime, Screen
from .rich_view import idle_panel, text


class IdlePreview(App):
    """Each gene can be judged alone or in combination, without changing settings."""

    CSS = "Screen { layout: vertical; } #idle { height: auto; } #footer { dock: bottom; height: auto; }"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("c", "checking", "Checking"),
        Binding("plus,equals_sign", "repos(1)", "More repos"),
        Binding("minus", "repos(-1)", "Fewer repos"),
        Binding("left_square_bracket", "tempo(-0.1)", "Slower"),
        Binding("right_square_bracket", "tempo(0.1)", "Faster"),
        Binding("d", "toggle('daytime')", "Clock palette"),
        Binding("comma", "hour(-1)", "Earlier"),
        Binding("full_stop", "hour(1)", "Later"),
        Binding("m", "toggle('moon')", "Moon"),
        Binding("e", "toggle('events')", "Events"),
        Binding("E", "event", "Next event now"),
    ]
    KEYS = "c checking  +/- repos  [ ] tempo  d daytime  , . hour  m moon  e events  E next event  q quit"

    def __init__(self, repos=3, ascii=False):
        super().__init__()
        self.repos = max(0, repos)
        self.ascii = ascii
        self.checking = False
        self.look = DEFAULT_LOOK
        self.hour_offset = 0
        self.event_slot = None
        self.event_started = None
        self.started = time.monotonic()

    def compose(self) -> ComposeResult:
        yield Static(text(), markup=False, id="idle")
        yield Static(text(), markup=False, id="footer")

    def on_mount(self):
        self.timer = self.set_interval(1 / 10, self.repaint)
        self.repaint()

    def on_resize(self):
        self.repaint()

    def repaint(self):
        now = time.monotonic()
        date = datetime.now(timezone.utc)
        local = time.localtime()
        hour = (local.tm_hour + local.tm_min / 60 + local.tm_sec / 3600 + self.hour_offset) % 24
        event_time = None
        if self.event_started is not None:
            event_time = event_start(self.event_slot) + (now - self.event_started) * self.look.tempo / DEFAULT_LOOK.tempo
            if event_at(event_time) is None:
                self.event_started = None
                event_time = None
        fields = [f"tempo={self.look.tempo:.1f}"]
        fields.extend(name for name in ('daytime', 'moon', 'events') if getattr(self.look, name))
        state = (f" Look({', '.join(fields)})  hour={hour:05.2f}"
                 + ("  checking" if self.checking else ""))
        event = event_at(now * self.look.tempo / DEFAULT_LOOK.tempo if event_time is None else event_time)
        if self.look.events and event:
            state += f"  event={event[0]}"
        keys = self.KEYS if self.size.width > len(self.KEYS) else 'c +/- [ ] d , . m e E q'
        footer = state + ('\n ' if len(state) + len(keys) + 2 > self.size.width else '  ') + keys
        self.query_one("#footer", Static).update(Text(footer, 'dim', no_wrap=True, overflow='crop'))
        runtime = Runtime(phase="checking" if self.checking else "idle", next_check_at=self.started + 300,
                          repos=tuple(f"org/repo-{i + 1}" for i in range(self.repos)))
        panel = idle_panel(Screen(runtime=runtime), now, self.size.width, self.ascii,
                           look=self.look, hour=hour, date=date, event_time=event_time,
                           preview=True, no_color=self.no_color)
        self.query_one("#idle", Static).update(panel)

    def action_checking(self):
        self.checking = not self.checking
        self.repaint()

    def action_repos(self, delta: int):
        self.repos = max(0, self.repos + delta)
        self.repaint()

    def action_tempo(self, delta: float):
        self.look = replace(self.look, tempo=round(max(.2, min(1.5, self.look.tempo + delta)), 1))
        self.repaint()

    def action_toggle(self, name: str):
        self.look = replace(self.look, **{name: not getattr(self.look, name)})
        self.repaint()

    def action_hour(self, delta: int):
        self.hour_offset = (self.hour_offset + delta) % 24
        self.repaint()

    def action_event(self):
        now = time.monotonic()
        self.event_slot = (int(now * self.look.tempo / DEFAULT_LOOK.tempo // 120) if self.event_slot is None else self.event_slot) + 1
        self.event_started = now
        self.look = replace(self.look, events=True)
        self.repaint()


def run_preview(repos=3, ascii=False) -> int:
    IdlePreview(repos, ascii).run()
    return 0
