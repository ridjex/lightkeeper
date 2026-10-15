"""Opt-in screen diagnostics; no configuration or journal writes."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path


@dataclass
class Durations:
    calls: int = 0
    total: float = 0
    maximum: float = 0

    def add(self, elapsed):
        self.calls += 1
        self.total += elapsed
        self.maximum = max(self.maximum, elapsed)

    def summary(self):
        return {"calls": self.calls, "max": self.maximum,
                "mean": self.total / self.calls if self.calls else 0}


class ScreenTrace:
    def __init__(self):
        self.reset()

    def reset(self):
        self.rendered = 0
        self.skipped = 0
        self.max_lateness = 0
        self.durations = {name: Durations() for name in ("refresh_snapshot", "animate_idle")}

    def duration(self, name, elapsed):
        self.durations[name].add(elapsed)

    def write(self, directory, interval, size):
        record = {"utc": datetime.now(timezone.utc).isoformat(), "interval": interval,
                  "rendered": self.rendered, "skipped": self.skipped, "max_lateness": self.max_lateness,
                  **{name: values.summary() for name, values in self.durations.items()},
                  "width": size.width, "height": size.height}
        self.reset()  # Each line describes only the window since the previous attempt.
        if not directory:
            return
        try:
            fd = os.open(Path(directory) / "screen.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(json.dumps(record) + "\n")
        except OSError:
            pass  # Diagnostics must not break the screen or create a missing state directory.
