"""Immutable requests and a bounded, non-blocking UI inbox."""
from dataclasses import dataclass
import queue
import threading


class StopEvent(threading.Event):
    """A terminal stop also interrupts the runner's command wait."""
    def __init__(self, wake):
        super().__init__()
        self.wake = wake

    def set(self):
        super().set()
        self.wake.set()


@dataclass(frozen=True)
class Command:
    id: str
    kind: str
    key: tuple | None = None
    head: str = ""
    text: str = ""


@dataclass(frozen=True)
class CommandResult:
    id: str
    outcome: str
    text: str


class CommandInbox(queue.Queue):
    def __init__(self, wake):
        super().__init__(maxsize=16)
        self.wake = wake
        self._outstanding = set()
        self._reviewing = set()
        self._checks = {}

    @staticmethod
    def _target(item):
        if item.kind in ("wake", "probe_engine", "check_updates"):
            return (item.kind,)
        if item.kind == "owner_approve":
            return ("owner", item.key)
        if item.kind == "reopen":
            return ("pr", item.key)
        return ("id", item.id)

    def put(self, item, block=False, timeout=None):
        if item.kind == "check_repo":
            return self.put_check(item) == item.id
        # Never stall the UI. The mutex makes coalescing atomic with get().
        with self.not_full:
            target = self._target(item)
            if target in self._outstanding or target in self._reviewing:
                self.wake.set()
                return False
            if self._qsize() >= self.maxsize:
                raise queue.Full
            self._put(item)
            self._outstanding.add(target)
            self.unfinished_tasks += 1
            self.wake.set()
            self.not_empty.notify()
            return True

    def put_check(self, item):
        """Return the id of the queued or running check, atomically with enqueue."""
        with self.not_full:
            if item.key in self._checks:
                return self._checks[item.key]
            if self._qsize() >= self.maxsize:
                raise queue.Full
            self._put(item)
            self._checks[item.key] = item.id
            self._outstanding.add(self._target(item))
            self.unfinished_tasks += 1
            self.wake.set()
            self.not_empty.notify()
            return item.id

    def complete(self, item, *, retain=False):
        with self.mutex:
            if item.kind == "check_repo":
                self._checks.pop(item.key, None)
            if not retain:
                self._outstanding.discard(self._target(item))
        self.task_done()

    def release(self, key):
        with self.mutex:
            self._outstanding.discard(("pr", tuple(key)))

    def reviewing(self, key, active):
        with self.mutex:
            target = ("pr", tuple(key))
            if active:
                self._reviewing.add(target)
            else:
                self._reviewing.discard(target)

    def targets(self, key):
        with self.mutex:
            return ("pr", tuple(key)) in self._outstanding
