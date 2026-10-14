"""Runner lifetime: finish writes, cancel a slow engine, and always join before unlocking."""
import logging
import threading
import time


class RunnerThread:
    def __init__(self, runner, observer, target, join_timeout=20):
        self.runner, self.observer, self.target = runner, observer, target
        self.join_timeout = join_timeout
        self.deadline = None
        self.exit_code = 0
        self._cancelled_engine = False
        self.thread = threading.Thread(target=self._run, name="runner", daemon=False)

    def _run(self):
        try:
            self.target()
        except BaseException:
            logging.getLogger("lightkeeper").exception("runner crashed")
            self.runner.condition("runner_crashed", "runner crashed; see run.log", level="error")
            self.exit_code = 1

    def request_stop(self):
        if self.deadline is None:
            self.deadline = time.monotonic() + self.join_timeout
        self.runner.stop.set()
        self.runner.wake.set()

    def poll_join(self):
        if (self.thread.is_alive() and self.deadline is not None
                and time.monotonic() >= self.deadline
                and self.observer.snapshot.runtime.phase != "writing" and not self._cancelled_engine):
            engine = getattr(self.runner.reviewer, "engine", None)
            if engine is not None:
                engine.cancel()
            self._cancelled_engine = True
        self.thread.join(timeout=0)
        return not self.thread.is_alive()

    def join(self):
        self.request_stop()
        while not self.poll_join():
            self.thread.join(timeout=0.05)
