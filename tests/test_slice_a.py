"""Compatible safety boundaries: no network, real locks under a fake home."""
import dataclasses
import importlib.util
import io
import itertools
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from lightkeeper import __main__ as cli
from lightkeeper.github import GhError, GhTimeout
from lightkeeper.journal import Journal, JournalError, Lock
from lightkeeper.models import Decision, Review, is_automated, marker_line
from lightkeeper.policy import evaluate_pr
from lightkeeper.ui.model import Runtime
from lightkeeper.ui.project import Observer, Projection
from tests.helpers import FakeGh, FakeReviewer, HEAD, PENDING_PROD, make_settings, make_pr, make_run

V1 = marker_line("opus", "abcdef", HEAD)
V2 = f"<!-- lightkeeper v2 head={HEAD} judgement=abcdef engine=claude/opus -->"


class MarkerCompatibilityTests(unittest.TestCase):
    def test_last_unquoted_line_only(self):
        for marker in (V1, V2):
            for body, expected in ((marker, True), ("review\n\n" + marker + "\n \n", True),
                                   ("> " + marker, False), ("  > " + marker, False),
                                   ("mentioned " + marker, False), (marker + "\nhuman text", False)):
                with self.subTest(body=body):
                    self.assertEqual(is_automated(body), expected)
        self.assertTrue(is_automated(V2 + "\n" + V1))  # valid final v1, not the earlier v2
        self.assertTrue(is_automated(V1 + "\n" + V2))
        for body in (V2.replace(HEAD, "a" * 6), V2.replace(HEAD, "a" * 41),
                     V2.replace("abcdef", "abcde"), V2.replace("abcdef", "a" * 65),
                     V2.replace("abcdef", "ghijkl"), V2.replace("claude/opus", "two tokens"),
                     V2 + " prose", "lightkeeper v2 head=1234567 judgement=abcdef engine=opus"):
            with self.subTest(body=body):
                self.assertFalse(is_automated(body))
        self.assertTrue(is_automated(V2.replace(HEAD, "A" * 7).replace("abcdef", "F" * 64)))

    def test_fingerprint_and_policy_takeover(self):
        pr = make_pr()
        for marker in (V1, V2):
            auto = dataclasses.replace(pr, requested_users=(), reviews=(Review("me", "COMMENTED", HEAD, marker),))
            self.assertEqual(pr.fingerprint, auto.fingerprint)
            self.assertEqual(evaluate_pr(auto, make_settings()).outcome, "review")
            for body in ("> " + marker, marker + "\nhuman text"):
                manual = dataclasses.replace(auto, reviews=(Review("me", "COMMENTED", HEAD, body),))
                self.assertNotEqual(pr.fingerprint, manual.fingerprint)
                self.assertEqual(evaluate_pr(manual, make_settings()).outcome, "ignore")

    def test_mixed_history_dedupe_dismissal_and_reconciliation(self):
        for marker in (V1, V2):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                gh, journal = FakeGh(), Journal(Path(directory) / "journal.jsonl")
                notifier = Mock()
                runner = cli.Runner(make_settings(), gh, journal, notifier=notifier, live_lock_held=True)
                key = ("org/repo", 7, HEAD)
                old = Review("me", "COMMENTED", "0" * 40, V1 if marker == V2 else V2)
                comment = Review("me", "COMMENTED", HEAD, marker)
                pr = make_pr(reviews=(old, comment))
                gh.pr_snapshots[key[:2]] = [pr]
                self.assertIn("already", runner._write_pr(pr, Decision("comment", "private", "ai", summary="note")))
                self.assertIn("applied", runner._write_pr(pr, Decision("approve", "private", "ai")))
                self.assertEqual(len(gh.writes), 1)  # comment does not dedupe approval
                self.assertTrue(gh.writes[0][-1].endswith(f"<!-- lightkeeper v2 head={HEAD[:7]} "
                                f"judgement={runner.settings.judgement('org/repo').hash} engine=claude/opus -->"))
                journal.append(kind="pr", key=key, event="unknown", mode="live", policy="abcdef", action="comment")
                self.assertEqual(runner.reconcile_unknown(), 1)
                self.assertFalse(journal.is_unknown(key))
                gh.pr_snapshots[key[:2]] = [make_pr(reviews=(dataclasses.replace(comment, state="DISMISSED"),))]
                self.assertEqual(runner.process_pr(*key[:2]), "ignore")
                self.assertEqual(runner.process_pr(*key[:2]), "final")
                notifier.assert_called_once()
                self.assertIn("dismissed", notifier.call_args.args[1])


class IdentityTests(unittest.TestCase):
    def test_identity_at_tick_and_both_write_boundaries(self):
        for kind, actual in itertools.product(("pr", "deployment"),
                                              ("me", "other", GhTimeout([], 10), GhError([], 1, "lookup failed"),
                                               ValueError("malformed identity"), OSError("gh unavailable"))):
            with self.subTest(kind=kind, actual=actual), tempfile.TemporaryDirectory() as directory:
                gh = FakeGh()
                key = ("org/repo", 7, HEAD) if kind == "pr" else ("org/repo", 100, 1, 555)
                if kind == "pr":
                    gh.pr_snapshots[key[:2]] = [make_pr()]
                else:
                    gh.runs = [make_run()]
                    gh.pending[key[:2]] = [PENDING_PROD]
                    gh.notifications = [("org/repo", 100, "updated")]
                gh.login = Mock(side_effect=["me"] + [actual] * 3)
                observer = Observer(Projection(), Runtime(login="me"))
                journal = Journal(Path(directory) / "journal.jsonl")
                runner = cli.Runner(make_settings(), gh, journal, FakeReviewer([Decision("approve", "ok", "ai")]),
                                    observer=observer, notifier=Mock(), live_lock_held=True, sleeper=Mock())
                with patch.object(cli, "log") as log:
                    self.assertEqual(runner.tick(), {"applied" if actual == "me" else "defer": 1})
                self.assertEqual(gh.login.call_count, 4 if isinstance(actual, Exception) else 2)
                self.assertEqual(len(gh.writes), int(actual == "me"))
                alerts = {a.key: a for a in observer.snapshot.alerts}
                if actual == "me":
                    self.assertNotIn("gh_identity", alerts)
                    self.assertEqual(observer.runtime.gh_login, "me")
                else:
                    reason = ("gh login is other, expected me" if actual == "other" else
                              f"gh login lookup failed, expected me: {actual}")
                    self.assertEqual(journal.defer_reason(key), reason)
                    self.assertFalse(journal.is_unknown(key))
                    self.assertFalse(runner._identity_checked)
                    if actual == "other":
                        self.assertEqual(alerts["gh_identity"].level, "error")
                        self.assertEqual(alerts["gh_identity"].text, reason)
                        log.assert_any_call(reason)
                    else:
                        self.assertNotIn("gh_identity", alerts)
                        self.assertEqual(observer.runtime.gh_login, "me")
                        self.assertFalse(observer.runtime.gh_confirmed)
                        log.assert_any_call(f"gh unreachable, writes wait: {actual}")
                    gh.login = Mock(return_value="me")
                    runner.reviewer = FakeReviewer([Decision("approve", "ok", "ai")])
                    self.assertEqual(runner.tick(), {"applied": 1})
                    self.assertFalse(observer.snapshot.alerts)

    def test_invalid_identity_skips_whole_tick_and_logs_once_per_streak(self):
        with tempfile.TemporaryDirectory() as directory:
            gh = Mock(wraps=FakeGh())
            gh.login.side_effect = ["other", "other", "other", "me", "other"]
            observer = Observer(Projection(), Runtime(login="me"))
            runner = cli.Runner(make_settings(), gh, Journal(Path(directory) / "journal.jsonl"),
                                observer=observer, notifier=Mock(), live_lock_held=True, sleeper=Mock())
            with patch.object(runner, "_tick", return_value={}) as tick, patch.object(cli, "log") as log:
                for _ in range(3):
                    self.assertEqual(runner.tick(), {})
                    tick.assert_not_called()
                    self.assertEqual(observer.snapshot.alerts[0].key, "gh_identity")
                    self.assertEqual(observer.snapshot.alerts[0].level, "error")
                    self.assertFalse(observer.runtime.last_check_ok)
                self.assertEqual(gh.mock_calls, [call.login(timeout_s=15)] * 3)
                log.assert_called_once_with("gh login is other, expected me")
                self.assertEqual(runner.tick(), {})
                tick.assert_called_once_with()
                self.assertFalse(observer.snapshot.alerts)
                self.assertEqual(runner.tick(), {})
                self.assertEqual(log.call_count, 2)
                tick.assert_called_once_with()

    def test_tick_skips_multiple_items_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            gh = FakeGh()
            gh.login = Mock(return_value="other")
            gh.pr_snapshots = {("org/repo", n): [make_pr(number=n)] for n in (7, 8)}
            journal = Journal(Path(directory) / "journal.jsonl")
            reviewer = FakeReviewer([Decision("nothing", "no action", "ai")] * 2)
            runner = cli.Runner(make_settings(), gh, journal, reviewer, notifier=Mock(), live_lock_held=True)
            self.assertEqual(runner.tick(), {})
            gh.login.assert_called_once_with(timeout_s=15)
            self.assertEqual(reviewer.calls, 0)
            self.assertEqual(journal.records, 0)
            gh.login.return_value = "me"
            self.assertEqual(runner.tick(), {"nothing": 2})
            self.assertEqual(gh.login.call_count, 2)

    def test_wrong_account_cannot_quarantine_watched_pr_and_recovery_writes(self):
        for status in (403, 404):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                fake = FakeGh()
                fake.search_results = []  # recovery must use the durable watch
                fake.pr_snapshots[("org/repo", 7)] = [make_pr()]
                gh = Mock(wraps=fake)
                gh.login.return_value = "other"
                def fetch(repo, number):
                    if gh.login.return_value == "other":
                        raise GhError([], 1, f"HTTP {status}: no access")
                    return fake.pull_request(repo, number)
                gh.pull_request.side_effect = fetch
                journal = Journal(Path(directory) / "journal.jsonl")
                key = ("org/repo", 7, HEAD)
                journal.append(kind="pr", key=key, event="defer", mode="live", policy="abcdef",
                               reason="checks pending")
                before = journal.path.read_bytes()
                runner = cli.Runner(make_settings(), gh, journal,
                                    FakeReviewer([Decision("approve", "ok", "ai")]), notifier=Mock(), live_lock_held=True)
                self.assertEqual(runner.tick(), {})
                self.assertEqual(gh.mock_calls, [call.login(timeout_s=15)])
                self.assertFalse(runner.quarantined)
                self.assertEqual(journal.path.read_bytes(), before)
                self.assertEqual(journal.watched_prs(), [key[:2]])
                self.assertEqual(journal.deferred_keys("pr"), [list(key)])
                gh.login.return_value = "me"
                self.assertEqual(runner.tick(), {"applied": 1})
                self.assertEqual(len(fake.writes), 1)
                self.assertEqual(fake.writes[0][:5], ("pr", *key, "approve"))
                self.assertFalse(runner.quarantined)

    def test_wrong_account_preserves_pending_gate_without_notification_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeGh()
            fake.runs = [make_run()]
            key = ("org/repo", 100, 1, 555)
            fake.pending[key[:2]] = [dataclasses.replace(PENDING_PROD, can_approve=False)]
            gh = Mock(wraps=fake)
            gh.login.return_value = "other"
            journal = Journal(Path(directory) / "journal.jsonl")
            journal.append(kind="deployment", key=key, event="defer", mode="live", policy="abcdef",
                           reason="jobs still running")
            before = journal.path.read_bytes()
            journal = Journal(journal.path)
            journal.replay()
            runner = cli.Runner(make_settings(), gh, journal,
                                FakeReviewer([Decision("approve", "ok", "ai")]), notifier=Mock(), live_lock_held=True)
            self.assertEqual(runner.tick(), {})
            self.assertEqual(gh.mock_calls, [call.login(timeout_s=15)])
            self.assertEqual(journal.path.read_bytes(), before)
            self.assertEqual(journal.pending_gates(), [key])
            self.assertEqual(journal.deferred_keys("deployment"), [list(key)])
            gh.login.return_value = "me"
            fake.pending[key[:2]] = [PENDING_PROD]
            self.assertFalse(fake.notifications)
            self.assertEqual(runner.tick(), {"applied": 1})
            self.assertEqual(len(fake.writes), 1)
            self.assertEqual(fake.writes[0][:4], ("deployment", "org/repo", 100, 555))
            self.assertNotIn('"gate_gone"', journal.path.read_text())

    @unittest.skipUnless(importlib.util.find_spec("rich"), "Rich is optional")
    def test_header_is_dim_until_checked_and_red_on_lookup_failure(self):
        from rich.console import Console
        from lightkeeper.ui.rich_view import render
        observer = Observer(Projection(), Runtime(login="me"))
        console = Console(width=140, height=30)
        for ascii in (False, True):
            for failed in (False, True):
                observer("condition", key="gh_identity", text="lookup failed" if failed else None)
                segments = list(console.render(render(observer.snapshot, 0, 140, 30, ascii)))
                identity = next(s for s in segments if "gh " in s.text)
                if failed:
                    self.assertIn("gh x unknown" if ascii else "gh ✗ unknown", identity.text)
                    self.assertEqual(identity.style.color.name, "red")
                else:
                    self.assertIn("gh ..." if ascii else "gh …", identity.text)
                    self.assertTrue(identity.style.dim)
                    self.assertIsNone(identity.style.color)


class ModeAndLockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        from tests.helpers import isolate_cli
        isolate_cli(self, self.home)
        home = patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        self.default = cli.default_state_root() / "me"
        self.first, self.second = self.home / "first", self.home / "second"

    def test_effective_mode_matrix(self):
        for state, mode, once, dry_run, allow in itertools.product(
                (self.default, self.first), ("live", "dry-run"), (False, True), (False, True), (False, True)):
            with self.subTest(state=state.name, mode=mode, once=once, dry_run=dry_run, allow=allow):
                policy = make_settings(mode=mode)
                effective, reason, notifications = cli.effective_mode(policy, state, once=once,
                                                                         dry_run=dry_run, allow_live=allow)
                forced = once or dry_run or (state != self.default and not allow)
                self.assertEqual(effective, "dry-run" if forced else "live")
                self.assertEqual(cli.can_be_live(policy, effective), not forced and mode == "live")
                self.assertEqual(notifications, state == self.default or allow)
                self.assertTrue(reason)

    def test_resolved_symlink_and_relative_paths(self):
        self.default.mkdir(parents=True)
        alias = self.home / "alias"
        alias.symlink_to(self.default, target_is_directory=True)
        for state in (alias, Path(os.path.relpath(self.default)), self.default / ".." / "me"):
            policy, _, notifications = cli.effective_mode(make_settings(), state)
            self.assertEqual(policy, "live")
            self.assertTrue(notifications)

    def run_main(self, argv, loop, policy=None):
        with patch.object(cli, "load_settings", return_value=policy or make_settings()), \
             patch.object(cli, "Gh", return_value=FakeGh()), patch.object(cli, "ClaudeReviewer"), \
             patch.object(cli, "configure_diagnostics"), patch.object(cli, "_loop", side_effect=loop), \
             patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
            return cli.main(["--plain", "--config-dir", str(self.home / "config"), *argv])

    def test_startup_refuses_identity_mismatch(self):
        gh = Mock(wraps=FakeGh())
        gh.login.return_value = "other"
        with patch.object(cli, "load_settings", return_value=make_settings()), \
             patch.object(cli, "Gh", return_value=gh), patch.object(cli, "ClaudeReviewer") as reviewer, \
             patch.object(cli, "configure_diagnostics"), patch.object(cli, "_loop") as loop, \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(cli.main(["--plain", "--config-dir", str(self.home / "config"), "--state-dir", str(self.first)]), 2)
        self.assertEqual(stderr.getvalue(), "gh is logged in as 'other' but settings expects 'me'\n")
        self.assertEqual(gh.mock_calls, [call.login()])
        reviewer.assert_not_called()
        loop.assert_not_called()

    def test_independent_processes_cannot_share_state_or_live_lock(self):
        journal = self.first / "journal.jsonl"
        Journal(journal).append(kind="pr", key=("org/repo", 7, HEAD), event="seen", mode="live", policy="abcdef")
        before = journal.read_bytes()
        holder_script = (
            "import sys\n"
            "from lightkeeper.journal import Journal, Lock\n"
            "with Lock(sys.argv[1]):\n"
            "    Journal(sys.argv[2]).replay()\n"
            "    print('locked', flush=True)\n"
            "    sys.stdin.readline()\n"
        )
        contender_script = (
            "import sys\n"
            "from lightkeeper.journal import Journal, JournalError, Lock\n"
            "try:\n"
            "    with Lock(sys.argv[1]):\n"
            "        Journal(sys.argv[2]).replay()\n"
            "except JournalError:\n"
            "    print('JournalError', flush=True)\n"
            "    sys.exit(3)\n"
        )
        for path in (self.first / "lock", cli.live_lock_path("me")):
            with self.subTest(lock=path):
                holder = subprocess.Popen([sys.executable, "-c", holder_script, str(path), str(journal)],
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True)
                try:
                    self.assertEqual(holder.stdout.readline().strip(), "locked")
                    contender = subprocess.run([sys.executable, "-c", contender_script, str(path), str(journal)],
                                               capture_output=True, text=True, timeout=5)
                    self.assertEqual(contender.returncode, 3, contender.stderr)
                    self.assertEqual(contender.stdout.strip(), "JournalError")
                    self.assertIsNone(holder.poll())
                    _, stderr = holder.communicate("release\n", timeout=5)
                    self.assertEqual(holder.returncode, 0, stderr)
                    recovered = subprocess.run([sys.executable, "-c", contender_script, str(path), str(journal)],
                                               capture_output=True, text=True, timeout=5)
                    self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    self.assertEqual(journal.read_bytes(), before)
                finally:
                    if holder.poll() is None:
                        holder.kill()
                    holder.communicate(timeout=5)

    def test_state_lock_always_live_lock_shared_across_state_dirs(self):
        def outer(args, policy, runner, stop):
            self.assertTrue(runner.live_lock_held)
            with self.assertRaises(JournalError), Lock(self.first / "lock"):
                pass
            self.assertEqual(self.run_main(["--state-dir", str(self.second), "--allow-live"], Mock()), 3)
            def dry_loop(args, policy, runner, stop):
                self.assertEqual(runner.mode("org/repo"), "dry-run")
                self.assertFalse(runner.live_lock_held)
                with self.assertRaises(JournalError), Lock(self.second / "lock"):
                    pass
                return 0
            self.assertEqual(self.run_main(["--state-dir", str(self.second)], dry_loop), 0)
            return 0
        self.assertEqual(self.run_main(["--state-dir", str(self.first), "--allow-live"], outer), 0)
        lock = cli.live_lock_path("me")
        inode = lock.stat().st_ino
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        self.assertEqual(lock.parent.stat().st_mode & 0o777, 0o700)
        with Lock(lock):
            pass
        self.assertEqual(lock.stat().st_ino, inode)
        # An old instance holding only its state lock still excludes the new runner.
        with Lock(self.default / "lock"):
            self.assertEqual(self.run_main([], Mock()), 3)
        with Lock(self.first / "lock"):
            self.assertEqual(self.run_main(["--once", "--state-dir", str(self.first)], Mock()), 3)

    def test_custom_state_notifications_and_env_override(self):
        with patch.dict(os.environ, LIGHTKEEPER_STATE=str(self.first)):
            self.assertEqual(cli.live_lock_path("me"), self.home / ".local/state/lightkeeper/locks/me")
            for extra, enabled in (([], False), (["--allow-live"], True), (["--once", "--allow-live"], True),
                                   (["--dry-run", "--allow-live"], True)):
                def loop(args, policy, runner, stop):
                    expected_live = enabled and not (args.once or args.dry_run)
                    self.assertEqual(runner.mode("org/repo"), "live" if expected_live else "dry-run")
                    self.assertEqual(runner.live_lock_held, expected_live)
                    self.assertEqual(runner.journal.path.parent, (self.first / "me").resolve())
                    runner._notify("pr", ("org/repo", 7, HEAD), "look")
                    return 0
                with patch.object(cli, "macos_notify") as notify:
                    self.assertEqual(self.run_main(extra, loop), 0)
                    self.assertEqual(notify.call_count, int(enabled))

    def test_loop_reapplies_effective_mode_on_reload(self):
        for state, allow, once, dry_run, held, expected in (
                (self.first, False, False, False, True, "dry-run"),
                (self.default, False, False, False, False, "dry-run"),
                (self.first, True, False, False, True, "live"),
                (self.first, True, True, False, True, "dry-run"),
                (self.first, True, False, True, True, "dry-run")):
            with self.subTest(state=state.name, allow=allow, once=once, dry_run=dry_run, held=held):
                policy = make_settings(mode="dry-run")
                runner = cli.Runner(policy, FakeGh(), Journal(state / "journal.jsonl"), live_lock_held=held,
                                    global_mode=cli.effective_mode(policy, state, once=once, dry_run=dry_run, allow_live=allow)[0])
                args = SimpleNamespace(policy="unused", once=once, dry_run=dry_run, allow_live=allow)
                stop = threading.Event()
                def tick():
                    self.assertEqual(runner.mode("org/repo"), expected)
                    stop.set()
                    return {}
                runner.tick = tick
                runner.wait = Mock()
                with patch.object(cli, "load_settings", return_value=make_settings()):
                    self.assertEqual(cli._loop(args, policy, runner, stop), 0)

    def test_reload_cannot_enable_live_without_lock(self):
        for held in (False, True):
            runner = cli.Runner(make_settings(mode="dry-run"), FakeGh(), Journal(self.first / "journal.jsonl"),
                                live_lock_held=held)
            with patch.object(cli, "log") as log:
                runner.refresh_settings(make_settings())
            self.assertEqual(runner.mode("org/repo"), "live" if held else "dry-run")
            if not held:
                self.assertTrue(any("live lock not held" in c.args[0] for c in log.call_args_list))
            runner.refresh_settings(make_settings(mode="dry-run"))
            self.assertEqual(runner.mode("org/repo"), "dry-run")

    def test_process_exit_releases_lock_without_unlinking(self):
        path = cli.live_lock_path("me")
        script = ("import sys, time; from lightkeeper.journal import Lock; "
                  "lock = Lock(sys.argv[1]); lock.__enter__(); print('locked', flush=True); time.sleep(30)")
        child = subprocess.Popen([sys.executable, "-c", script, str(path)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            inode = path.stat().st_ino
            with self.assertRaises(JournalError), Lock(path):
                pass
        finally:
            child.kill()
            child.wait(timeout=5)
            child.stdout.close()
        with Lock(path):
            self.assertEqual(path.stat().st_ino, inode)
