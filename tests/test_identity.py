"""Identity retries affect reporting, never permission to write."""
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from lightkeeper import __main__ as cli
from lightkeeper.github import GhError, GhTimeout
from lightkeeper.journal import Journal
from lightkeeper.ui.model import Runtime
from lightkeeper.ui.project import Observer, Projection
from tests.helpers import FakeGh, make_settings


class IdentityRetryTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.gh = FakeGh()
        self.gh.login = Mock(return_value="me")
        self.observer = Observer(Projection(), Runtime(login="me"))
        self.sleep = Mock()
        self.runner = cli.Runner(make_settings(repos=[]), self.gh, Journal(root / "journal.jsonl"),
                                 observer=self.observer, sleeper=self.sleep, notifier=Mock())
        self.tick = self.enterContext(patch.object(self.runner, "_tick", return_value={"nothing": 1}))
        self.log = self.enterContext(patch.object(cli, "log"))
        self.error = GhError(["api", "user"], 1, "unexpected EOF")

    def fail_tick(self):
        self.gh.login.side_effect = self.error
        self.assertEqual(self.runner.tick(), {})
        self.assertFalse(self.runner._identity_checked)
        self.assertFalse(self.observer.runtime.last_check_ok)

    def test_retry_recovers_inside_tick_without_log_or_alert(self):
        for error in (self.error, GhTimeout(["api", "user"], 15), ValueError("malformed output")):
            with self.subTest(error=error):
                self.gh.login.reset_mock()
                self.sleep.reset_mock()
                self.tick.reset_mock()
                self.gh.login.side_effect = [error, "me"]
                self.assertEqual(self.runner.tick(), {"nothing": 1})
                self.assertEqual(self.gh.login.call_args_list, [call(timeout_s=15)] * 2)
                self.sleep.assert_called_once_with(1)
                self.tick.assert_called_once_with()
                self.log.assert_not_called()
                self.assertFalse(self.observer.snapshot.alerts)
                self.assertTrue(self.runner._identity_checked)
                self.assertTrue(self.observer.runtime.gh_confirmed)

    def test_exhausted_check_skips_tick_retains_login_and_logs_once(self):
        self.assertIsNone(self.runner._check_identity())
        self.gh.login.reset_mock()
        self.fail_tick()
        self.assertEqual(self.gh.login.call_args_list, [call(timeout_s=15)] * 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(3)])
        self.assertEqual(self.runner._identity_failures, 1)
        self.assertEqual(self.observer.runtime.gh_login, "me")
        self.assertFalse(self.observer.runtime.gh_confirmed)
        self.assertFalse(self.observer.snapshot.alerts)
        self.log.assert_called_once_with(f"gh unreachable, retrying next tick: {self.error}")
        self.tick.assert_not_called()
        self.fail_tick()
        self.assertFalse(self.observer.snapshot.alerts)
        self.assertEqual(self.log.call_count, 1)

    def test_three_failed_checks_alert_then_success_clears_and_resets(self):
        for _ in range(3):
            self.fail_tick()
        self.assertEqual(self.gh.login.call_count, 9)
        alert, = self.observer.snapshot.alerts
        self.assertEqual((alert.key, alert.level), ("gh_identity", "error"))
        self.assertIn(str(self.error), alert.text)
        self.tick.assert_not_called()
        self.assertEqual(self.log.call_count, 1)
        self.gh.login.side_effect = None
        self.assertEqual(self.runner.tick(), {"nothing": 1})
        self.assertFalse(self.observer.snapshot.alerts)
        self.assertEqual(self.runner._identity_failures, 0)
        self.assertTrue(self.runner._identity_checked)
        self.assertTrue(self.observer.runtime.gh_confirmed)
        self.fail_tick()
        self.assertFalse(self.observer.snapshot.alerts)
        self.assertEqual(self.runner._identity_failures, 1)
        self.assertEqual(self.log.call_count, 2)

    def test_mismatch_is_immediate_and_never_retried(self):
        self.gh.login.return_value = "other"
        self.assertEqual(self.runner.tick(), {})
        self.gh.login.assert_called_once_with(timeout_s=15)
        self.sleep.assert_not_called()
        self.tick.assert_not_called()
        self.assertFalse(self.runner._identity_checked)
        self.assertTrue(self.observer.runtime.gh_confirmed)
        self.assertEqual(self.observer.runtime.gh_login, "other")
        alert, = self.observer.snapshot.alerts
        self.assertEqual((alert.level, alert.text), ("error", "gh login is other, expected me"))
        self.assertEqual(self.runner._identity_failures, 0)

    def test_successful_mismatch_resets_lookup_streak(self):
        self.fail_tick()
        self.gh.login.side_effect = None
        self.gh.login.return_value = "other"
        self.runner.tick()
        self.assertEqual(self.runner._identity_failures, 0)
        self.assertEqual(self.gh.login.call_count, 4)
        self.assertEqual(self.observer.snapshot.alerts[0].text, "gh login is other, expected me")

    def test_rate_limit_is_named_in_log_reason_and_alert(self):
        self.error = GhError(["api", "user"], 1, "HTTP 403: secondary rate limit")
        for _ in range(3):
            self.fail_tick()
        self.log.assert_called_once_with(f"gh unreachable, retrying next tick: GitHub rate limited: {self.error}")
        self.assertIn("GitHub rate limited", self.runner._identity_reason)
        self.assertIn("GitHub rate limited", self.observer.snapshot.alerts[0].text)

    @unittest.skipUnless(importlib.util.find_spec("rich"), "Rich is optional")
    def test_header_distinguishes_unconfirmed_alert_and_mismatch_without_color(self):
        from rich.console import Console
        from lightkeeper.ui.rich_view import header_lines

        def header(expected):
            for ascii in (False, True):
                output = io.StringIO()
                console = Console(width=180, file=output, color_system=None, no_color=True)
                console.print(header_lines(self.observer.snapshot, 0, 180, ascii))
                self.assertIn(expected.replace("✗", "x") if ascii else expected, output.getvalue())

        self.fail_tick()
        header("gh ? unknown (unconfirmed)")
        self.gh.login.side_effect = None
        self.runner._check_identity()
        self.fail_tick()
        header("gh ? me (unconfirmed)")
        self.fail_tick()
        header("gh ? me (unconfirmed)")
        self.fail_tick()
        header("gh ✗ unknown (expected me)")
        self.gh.login.side_effect = None
        self.gh.login.return_value = "other"
        self.runner._check_identity()
        header("gh ✗ other (expected me)")
