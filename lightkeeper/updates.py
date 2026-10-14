"""Best-effort release reminders, checked only by the runner."""
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import time

from . import __version__

UPDATE_REPO = "ridjex/lightkeeper"
CHECK_INTERVAL = 24 * 60 * 60
UPGRADE = "brew upgrade lightkeeper or pipx upgrade lightkeeper"


def version_parts(tag):
    if not isinstance(tag, str) or not re.fullmatch(r"v?[0-9]+(?:\.[0-9]+)*", tag):
        raise ValueError("expected a numeric release tag")
    parts = tuple(int(part) for part in tag.removeprefix("v").split("."))
    while len(parts) > 1 and parts[-1] == 0:
        parts = parts[:-1]
    return parts


def newer(tag, current=None):
    try:
        return version_parts(tag) > version_parts(__version__ if current is None else current)
    except ValueError as exc:
        logging.getLogger("lightkeeper").debug("update comparison skipped: %s", exc)
        return None


class UpdateChecker:
    def __init__(self, state_dir, gh, clock=time.time):
        self.path = Path(state_dir) / "updates.json"
        self.gh, self.clock = gh, clock
        self.checked_at = None
        self.latest_tag = ""
        self.available = ""
        self.loaded = False

    def check(self, *, enabled=True, force=False):
        if not enabled:
            self.available = ""
            return "update checks disabled"
        error = None
        fetch_failed = False
        if not self.loaded:
            self.loaded = True
            try:
                record = json.loads(self.path.read_text(encoding="utf-8"))
                stamp, tag = record["checked_at"], record["latest_tag"]
                if type(stamp) not in (int, float) or not math.isfinite(stamp):
                    raise ValueError("invalid update check time")
                if tag:
                    version_parts(tag)
                elif tag != "":
                    raise ValueError("invalid cached release tag")
                self.checked_at, self.latest_tag = stamp, tag
            except FileNotFoundError:
                pass
            except Exception as exc:
                error = exc
        now = self.clock()
        if force or self.checked_at is None or not 0 <= now - self.checked_at < CHECK_INTERVAL:
            self.checked_at = now  # Failed attempts are throttled too.
            try:
                tag = self.gh.json(["api", f"repos/{UPDATE_REPO}/releases/latest"], timeout_s=10)["tag_name"]
                version_parts(tag)
                self.latest_tag = tag
            except Exception as exc:
                error = exc
                fetch_failed = True
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, name = tempfile.mkstemp(prefix=".updates-", dir=self.path.parent)
                temporary = Path(name)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump({"checked_at": now, "latest_tag": self.latest_tag}, stream)
                    os.replace(temporary, self.path)
                finally:
                    temporary.unlink(missing_ok=True)
            except Exception as exc:
                error = exc
        comparison = newer(self.latest_tag) if self.latest_tag else None
        self.available = self.latest_tag.removeprefix("v") if comparison else ""
        if error:
            logging.getLogger("lightkeeper").debug("update check failed: %s", error)
        if fetch_failed:
            return "update check unavailable; try again later"
        if self.available:
            return f"update {self.available} available: {UPGRADE}"
        return "lightkeeper is up to date" if comparison is not None else "no release information available"
