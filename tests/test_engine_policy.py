"""Engine pause policy with an injected wall clock; no CLI, network or real waiting."""
import dataclasses
import io
from pathlib import Path
import tempfile
import unittest
import importlib.util
from unittest.mock import Mock, patch

from lightkeeper.__main__ import REVIEWER_PAUSE_S, Runner
from lightkeeper.engines.base import (Answer, AnswerError, AuthError, Diagnostics, Identity, InputError,
                                           QuotaError, TransportError)
from lightkeeper.journal import Journal
from lightkeeper.reviewer import ClaudeReviewer, Reviewer, ReviewerError
from lightkeeper.ui.model import Runtime
from lightkeeper.ui.project import Observer, Projection
from tests.helpers import APPLY_JOB, PENDING_PROD, PLAN_JOB, FakeGh, make_settings, make_pr, make_run

OK = Identity(True, detail="authenticated")
BAD = Identity(False, detail="not logged in", fix_command="claude auth login")
ANSWER = Answer({"action": "approve", "why": "Approved: the change is covered by tests.", "watch": "", "summary": "", "findings": [], "reason": "safe"}, Diagnostics("raw", "", 0, []))


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1_000_000.0
        self.engine = Mock(label="claude/opus")
        self.engine.probe.return_value = OK
        self.engine.ask.return_value = ANSWER
        self.notified = []
        self.observer = Observer(Projection(), Runtime(), mono=lambda: 0, wall=lambda: self.now)
        gh = FakeGh()
        gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        self.runner = Runner(make_settings(mode="dry-run"), gh, Journal(Path(self.temp.name) / "journal.jsonl"),
                             Reviewer(self.engine, "rules"), clock=lambda: self.now,
                             notifier=lambda _, text: self.notified.append(text), observer=self.observer, live_lock_held=True)

    def pause(self, cause):
        self.runner._pause_reviewer(ReviewerError(str(cause), cause))

    def arm_deployment(self):
        run = make_run()
        gh = self.runner.gh
        gh.runs = [run]
        gh.notifications = [(run.repo, run.run_id, "2026-10-13T10:00:00Z")]
        gh.pending[(run.repo, run.run_id)] = [PENDING_PROD]
        gh.jobs_by_attempt[(run.repo, run.run_id, run.attempt)] = [PLAN_JOB, APPLY_JOB]
        return run

    def arm_multiple_items(self):
        for number in (8, 9):
            self.runner.gh.pr_snapshots[("org/repo", number)] = [make_pr(number=number)]
        self.arm_deployment()
        self.runner._probe_engine()
        self.engine.probe.reset_mock()

    def test_auth_pauses_until_probe_passes_notifies_once(self):
        self.arm_multiple_items()
        self.engine.probe.return_value = BAD
        self.pause(AuthError("OAuth expired", fix_command="claude auth login"))
        self.assertEqual(self.notified, ["engine login expired: OAuth expired; run: claude auth login"])
        for _ in range(3):
            self.now += REVIEWER_PAUSE_S
            self.assertEqual(self.runner.tick(), {"defer": 4})
        self.assertEqual(self.engine.probe.call_count, 4)
        self.engine.ask.assert_not_called()
        self.assertEqual(len(self.notified), 1)
        self.runner.gh.notifications.clear()
        self.runner.gh.pending.clear()
        self.runner.gh.pr_snapshots = {("org/repo", 7): [make_pr()]}
        for number in (8, 9):
            self.runner.gh.pr_snapshots[("org/repo", number)] = [make_pr(number=number, state="CLOSED")]
        self.engine.probe.return_value = OK
        self.runner.tick()
        self.engine.ask.assert_called_once()
        self.assertFalse(self.runner._auth_paused)
        self.assertFalse(any(a.key == "engine_identity" for a in self.observer.snapshot.alerts))
        self.engine.probe.return_value = BAD
        self.pause(AuthError("OAuth expired", fix_command="claude auth login"))
        self.assertEqual(len(self.notified), 2)

    def test_passing_probe_with_repeated_ask_auth_errors_does_not_notify_again(self):
        self.arm_multiple_items()
        self.engine.ask.side_effect = AuthError("OAuth expired", fix_command="claude auth login")
        for attempt in range(1, 4):
            self.assertEqual(self.runner.tick(), {"defer": 4})
            self.assertEqual(self.engine.ask.call_count, attempt)
            self.assertEqual(self.engine.probe.call_count, attempt)
            self.assertIn("OAuth expired", self.runner.journal.defer_reason(("org/repo", 9, make_pr().head_sha)))
        self.assertEqual(self.engine.ask.call_count, 3)
        self.assertEqual(len(self.notified), 1)

    def test_later_probe_recovery_resets_streak_without_a_model_call(self):
        self.engine.probe.return_value = BAD
        self.pause(AuthError("OAuth expired", fix_command="claude auth login"))
        self.engine.probe.return_value = OK
        self.runner._probe_engine()
        self.engine.probe.return_value = BAD
        self.pause(AuthError("OAuth expired", fix_command="claude auth login"))
        self.assertEqual(len(self.notified), 2)
        self.engine.ask.assert_not_called()

    def test_quota_deadline_and_auth_probe_cannot_shorten_it(self):
        reset = self.now + 3600
        self.pause(QuotaError("usage limit", reset_at=reset))
        self.assertEqual(self.runner.reviewer_paused_until, reset)
        self.assertIn("resets at", self.notified[0])
        self.engine.probe.return_value = BAD
        self.runner._probe_engine()
        self.engine.probe.return_value = OK
        self.now = reset - 1
        self.runner.tick()
        self.assertFalse(self.runner._auth_paused)
        self.engine.ask.assert_not_called()
        self.assertEqual(self.runner.reviewer_paused_until, reset)
        self.now = reset
        self.runner.tick()
        self.engine.ask.assert_called_once()

    def test_unknown_quota_and_transport_pause_fifteen_minutes(self):
        for cause in (QuotaError("quota"), TransportError("connection failed")):
            with self.subTest(cause=cause):
                self.pause(cause)
                self.assertEqual(self.runner.reviewer_paused_until, self.now + REVIEWER_PAUSE_S)
                self.assertEqual(self.runner._ask_about_pr(make_pr()).outcome, "defer")
        self.assertEqual(self.engine.probe.call_count, 2)

    def test_stale_quota_multi_item_ticks_use_default_and_notify_once_per_streak(self):
        self.arm_multiple_items()
        self.engine.ask.side_effect = QuotaError("quota", reset_at=self.now - 1)
        for attempt in range(1, 4):
            self.assertEqual(self.runner.tick(), {"defer": 4})
            self.assertEqual(self.engine.ask.call_count, attempt)
            self.assertEqual(self.engine.probe.call_count, attempt)
            self.assertEqual(self.runner.reviewer_paused_until, self.now + REVIEWER_PAUSE_S)
            self.assertEqual(len(self.notified), 1)
            self.assertEqual(self.runner.tick(), {"defer": 4})
            self.assertEqual(self.engine.ask.call_count, attempt)
            self.assertEqual(self.engine.probe.call_count, attempt)
            self.now += REVIEWER_PAUSE_S
        self.engine.ask.side_effect = None
        self.runner.gh.notifications = []
        self.runner.gh.pending.clear()
        self.runner.tick()
        self.pause(QuotaError("quota"))
        self.assertEqual(len(self.notified), 2)

    def test_equal_quota_reset_uses_default(self):
        self.pause(QuotaError("quota", reset_at=self.now))
        self.assertEqual(self.runner.reviewer_paused_until, self.now + REVIEWER_PAUSE_S)

    def test_transport_failure_defers_all_other_items_in_tick(self):
        self.arm_multiple_items()
        self.engine.ask.side_effect = TransportError("connection failed")
        self.assertEqual(self.runner.tick(), {"defer": 4})
        self.engine.ask.assert_called_once()
        self.engine.probe.assert_called_once()
        self.assertEqual(len(self.notified), 1)

    def test_deadline_expiring_during_tick_does_not_allow_another_call(self):
        self.arm_multiple_items()
        self.engine.ask.side_effect = QuotaError("quota", reset_at=self.now + 1)
        def recover():
            self.now += 2
            return OK
        self.engine.probe.side_effect = recover
        self.assertEqual(self.runner.tick(), {"defer": 4})
        self.engine.ask.assert_called_once()
        self.engine.probe.assert_called_once()

    def test_start_probe_failure_warns_once_without_pausing(self):
        self.engine.probe.return_value = BAD
        with patch("lightkeeper.__main__.log") as logged:
            self.runner._probe_engine()
            self.runner._probe_engine()
        logged.assert_called_once()
        alert = next(a for a in self.observer.snapshot.alerts if a.key == "engine_identity")
        self.assertEqual(alert.level, "warning")
        self.assertIn(BAD.detail, alert.text)
        self.assertIn(BAD.fix_command, alert.text)
        self.assertFalse(self.runner._auth_paused)
        self.runner.tick()
        self.runner.tick()
        self.assertEqual(self.engine.probe.call_count, 2)
        self.engine.ask.assert_called_once()
        self.assertEqual(len(self.notified), 0)
        self.assertFalse(self.observer.runtime.engine_identity.ok)

    def test_probe_after_unclear_and_settle_once(self):
        self.engine.ask.return_value = Answer({"action": "not-an-action"}, Diagnostics("bad", "", 0, []))
        self.runner.tick()
        self.runner.tick()
        self.engine.ask.assert_called_once()
        self.assertEqual(self.engine.probe.call_count, 2)  # startup and failed answer
        self.assertEqual(self.runner.reviewer_paused_until, 0)
        self.assertEqual(self.runner.journal.settled_outcome(("org/repo", 7, make_pr().head_sha)), "unclear")

    def test_deployment_typed_input_and_answer_failures_settle_once(self):
        self.runner.gh.pr_snapshots.clear()
        run = self.arm_deployment()
        for cause in (InputError("prompt is too long"), AnswerError("invalid JSON")):
            with self.subTest(cause=cause):
                run = dataclasses.replace(run, attempt=run.attempt + 1)
                self.runner.gh.runs = [run]
                self.runner.gh.jobs_by_attempt[(run.repo, run.run_id, run.attempt)] = [PLAN_JOB, APPLY_JOB]
                key = (run.repo, run.run_id, run.attempt, PENDING_PROD.env_id)
                self.engine.ask.reset_mock()
                self.engine.probe.reset_mock()
                self.engine.ask.side_effect = cause
                self.assertEqual(self.runner.process_run(run.repo, run.run_id), "unclear")
                self.assertEqual(self.runner.process_run(run.repo, run.run_id), "final")
                self.engine.ask.assert_called_once()
                self.engine.probe.assert_called_once()
                self.assertEqual(self.runner.journal.settled_outcome(key), "unclear")
                self.assertFalse(self.runner._auth_paused)
                self.assertEqual(self.runner.reviewer_paused_until, 0)
        self.assertEqual(len(self.notified), 2)

    def test_failed_reload_probe_warns_and_still_calls_model(self):
        reviewer = ClaudeReviewer("opus", "rules", "/tmp/old-profile")
        reviewer.engine.probe = Mock(return_value=BAD)
        reviewer.engine.ask = Mock(return_value=ANSWER)
        self.runner.reviewer = reviewer
        self.runner._engine_probed = True
        self.runner.refresh_settings(dataclasses.replace(self.runner.settings, agent=dataclasses.replace(self.runner.settings.agent, profile="/tmp/new-profile")))
        self.runner.tick()
        reviewer.engine.probe.assert_called_once()
        reviewer.engine.ask.assert_called_once()
        self.assertFalse(self.runner._auth_paused)
        alert = next(a for a in self.observer.snapshot.alerts if a.key == "engine_identity")
        self.assertEqual(alert.level, "warning")
        self.assertIn(BAD.fix_command, alert.text)
        self.assertEqual(self.notified, [])

    def test_profile_reload_probes_new_profile_without_clearing_quota(self):
        reviewer = ClaudeReviewer("opus", "rules", "old-profile")
        reviewer.engine.probe = Mock(return_value=OK)
        self.runner.reviewer = reviewer
        self.runner._probe_engine()
        self.runner.reviewer_paused_until = self.now + 3600
        self.runner._auth_notified = True
        self.runner.refresh_settings(dataclasses.replace(self.runner.settings, agent=dataclasses.replace(self.runner.settings.agent, profile="new-profile", effort="high")))
        self.assertIsNone(self.observer.runtime.engine_identity)
        self.assertFalse(self.runner._auth_notified)
        self.runner.tick()
        self.assertEqual(reviewer.engine.profile, str((self.runner.settings.config_dir / "new-profile").resolve()))
        self.assertEqual(self.runner.settings.agent.profile, "new-profile")
        self.assertEqual(reviewer.engine.effort, "high")
        self.assertEqual(reviewer.engine.probe.call_count, 2)
        self.assertEqual(self.runner.reviewer_paused_until, self.now + 3600)
        self.assertEqual(self.observer.runtime.engine_profile, "new-profile")

    @unittest.skipUnless(importlib.util.find_spec("rich"), "Rich is optional")
    def test_identity_header_pending_ok_failed_and_ascii(self):
        from rich.console import Console
        from lightkeeper.ui.rich_view import render
        for identity, mark, ascii in ((None, "…", False), (OK, "✓", False), (BAD, "✗", False), (OK, "ok", True)):
            with self.subTest(mark=mark):
                self.observer("engine_identity", identity=identity, name="claude", profile="test-profile")
                console = Console(width=160, record=True, file=io.StringIO(), color_system=None)
                console.print(render(self.observer.snapshot, 0, 160, 30, ascii))
                self.assertIn(f"claude test-profile {mark}", console.export_text())
