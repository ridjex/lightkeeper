"""Opt-in screen diagnostics as `screen-trace` lines in run.log; the screen writes no file of its own."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging


@dataclass
class Durations:
    calls: int = 0
    maximum: float = 0

    def add(self, elapsed):
        self.calls += 1
        self.maximum = max(self.maximum, elapsed)


class ScreenTrace:
    def __init__(self):
        self.reset()

    def reset(self):
        self.rendered = 0
        self.skipped = 0
        self.max_lateness = 0
        self.backlog = 0
        self.durations = {name: Durations() for name in ("refresh_snapshot", "animate_idle")}

    def duration(self, name, elapsed):
        self.durations[name].add(elapsed)

    def write(self, interval, size):
        """One line per window: high lateness with low durations means the terminal, not the app, is slow."""
        record = {"utc": datetime.now(timezone.utc).isoformat(), "interval": interval,
                  "rendered": self.rendered, "skipped": self.skipped, "max_lateness": self.max_lateness,
                  "backlog": self.backlog,
                  **{name: {"calls": d.calls, "max": d.maximum} for name, d in self.durations.items()},
                  "width": size.width, "height": size.height}
        self.reset()  # Each line describes only the window since the previous one.
        logging.getLogger("lightkeeper").info("screen-trace %s", json.dumps(record))
