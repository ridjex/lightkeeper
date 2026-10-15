"""Synthetic journal projection, bounded layouts and runner integration. No network or model calls."""
import dataclasses
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from lightkeeper.__main__ import Runner, configure_logging, live_screen_class, log, main, _terminate
from lightkeeper.github import GhError
from lightkeeper.journal import Journal
from lightkeeper.models import CICounts, Decision, Review, marker_line
from lightkeeper.ui.model import Alert, ItemRef, ItemRow, Runtime, Stats
from lightkeeper.ui.project import Observer, Projection, label
from tests.helpers import FakeGh, FakeReviewer, HEAD, make_settings, make_pr, make_run, PENDING_PROD

FIXTURE = Path(__file__).parent / "fixtures/tui.jsonl"
NOW = datetime.fromisoformat("2026-10-10T10:00:00+00:00").timestamp()


class UTCDateTime(datetime):
    @classmethod
    def fromtimestamp(cls, timestamp, tz=timezone.utc):
        return super().fromtimestamp(timestamp, tz)


@patch("lightkeeper.ui.project.datetime", UTCDateTime)
def fixture_screen():
    p = Projection(log_path="state/run.log")
    for line in FIXTURE.read_text().splitlines():
        p.feed(json.loads(line))
    runtime = Runtime(phase="sleeping", phase_since=100, next_check_at=142, started_at=0,
                      login="reviewer", mode="live", model="opus", effort="medium", policy_hash="bbbbbb",
                      repos=("example/widget",), last_check_at=100, last_check_ok=True,
                      alerts=(Alert("search_saturated", "warning", "search hit its result limit", 90),))
    return p.screen(runtime, NOW)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("lightkeeper.ui.project.datetime", UTCDateTime))
        self.p = Projection()
        self.runtime = Runtime(repos=("example/widget",))
        self.rec = dict(at="2026-10-10T09:00:00Z", kind="pr", key=["example/widget", 1, "aaaaaaa"], policy="aaaaaa")

    def feed(self, event, **extra):
        self.p.feed({**self.rec, "event": event, **extra})

    def row(self):
        return self.p.screen(self.runtime, NOW).watching[0]

    def test_fixture_states_stats_history(self):
        s = fixture_screen()
        by = {r.item.number: r for r in s.watching}
        self.assertNotIn(6, by)
        self.assertEqual((by[1].state, by[1].needs_you), ("changes", "check the retry limit"))
        self.assertEqual(by[1].since, datetime.fromisoformat("2026-10-10T09:01:00+00:00").timestamp())
        self.assertEqual(by[2].state, "waiting")
        self.assertEqual(by[4].dry_run, True)
        self.assertEqual(dict(s.stats.posted), {"approve": 1, "request_changes": 1})
        self.assertEqual(dict(s.stats.dry_run), {"comment": 1})
        self.assertFalse(any(h.item.number == 3 for h in s.history))
        self.assertEqual(next(h.repeats for h in s.history if h.item.number == 1), 2)

    def test_startup_replay_matches_live_snapshot_with_one_publication(self):
        runtime = Runtime(repos=("example/widget",))
        live = Observer(Projection(), runtime, mono=lambda: 100, wall=lambda: NOW)
        batch = Observer(Projection(), runtime, mono=lambda: 100, wall=lambda: NOW)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'journal.jsonl'
            path.write_bytes(FIXTURE.read_bytes())
            expected = Journal(path, observer=live.feed).replay()
            journal = Journal(path)
            with patch.object(batch, 'publish', wraps=batch.publish) as publish:
                self.assertEqual(batch.replay(journal), expected)
                publish.assert_called_once_with()
                self.assertEqual(batch.snapshot, live.snapshot)
                journal.append(kind='pr', key=('example/widget', 9, 'new'), event='defer',
                               mode='dry-run', policy='rules', reason='pending checks')
                self.assertEqual(publish.call_count, 2)

    def test_head_notify_and_supersession(self):
        self.feed("nothing", reason="look")
        self.feed("notify", text="look here")
        self.feed("assessment", action="approve")
        self.assertEqual((self.row().state, self.row().needs_you), ("silent", "look here"))
        for event, extra in (("applied", {"action": "approve"}), ("satisfied", {}), ("ignore", {})):
            self.feed("notify", text="look here")
            self.feed(event, **extra)
            self.assertIsNone(self.row().needs_you)
        self.feed("notify", text="old head")
        self.rec["key"] = ["example/widget", 1, "bbbbbbb"]
        self.feed("seen")
        self.assertEqual((self.row().state, self.row().why, self.row().needs_you),
                         ("waiting", "new head; queued", None))
        self.feed("applied", key=["example/widget", 1, "aaaaaaa"], action="approve", source="observed")
        self.assertEqual(self.row().item.head, "bbbbbbb")
        self.feed("closed")
        self.assertFalse(self.p.screen(self.runtime, NOW).watching)

    def test_since_repeats_rules_and_bounded_history(self):
        self.p = Projection(history_limit=2)
        self.feed("nothing", reason="look")
        since = self.row().since
        self.feed("nothing", reason="look", at="2026-10-10T09:05:00Z", policy="bbbbbb")
        self.assertEqual(self.row().since, since)
        self.feed("would_comment", reason="new", policy="cccccc")
        s = self.p.screen(self.runtime, NOW)
        self.assertEqual(len(s.history), 2)
        self.assertTrue(s.history[0].new_rules)
        self.assertEqual(s.history[1].repeats, 2)
        self.feed("error", reason="failed")
        self.assertEqual(len(self.p.screen(self.runtime, NOW).history), 2)

    def test_today_is_local_day_and_deduplicated(self):
        now = UTCDateTime.fromtimestamp(NOW)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        def stamp(local):
            return local.astimezone().isoformat()
        self.feed("applied", action="approve", at=stamp(midnight - timedelta(seconds=1)))
        self.feed("applied", action="comment", at=stamp(midnight))
        self.feed("applied", action="comment", at=stamp(now))
        self.feed("applied", action="request_changes", at=stamp(now), source="observed")
        self.feed("would_approve", at=stamp(now))
        s = self.p.screen(self.runtime, NOW)
        self.assertEqual(s.stats.posted, (("comment", 1),))
        self.assertEqual(s.stats.dry_run, (("approve", 1),))

    def test_gate_disappears_and_repo_labels(self):
        self.feed("seen", kind="deployment", key=["example/widget", 100, 1, 10], environment="staging")
        self.assertEqual(len(self.p.screen(self.runtime, NOW).watching), 1)
        self.feed("gate_gone", kind="deployment", key=["example/widget", 100, 1, 10])
        self.assertFalse(self.p.screen(self.runtime, NOW).watching)
        item = ItemRef("pr", "example/widget", 42)
        self.assertEqual(label(item, ("example/widget",)), "widget#42")
        self.assertEqual(label(item, ("example/widget", "other/widget")), "example/widget#42")

    def test_journal_observer_replay_and_append(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "journal.jsonl"
            path.write_text(FIXTURE.read_text())
            received = []
            j = Journal(path, observer=received.append)
            self.assertEqual(j.replay(), 13)
            j.append(kind="pr", key=["example/widget", 8, "abcdefa"], event="seen", mode="live", policy="abcdef")
            self.assertEqual(len(received), 14)

    def test_observer_snapshot_is_immutable_and_condition_lifecycle(self):
        observer = Observer(self.p, self.runtime, mono=lambda: 10, wall=lambda: NOW)
        old = observer.snapshot
        observer("condition", key="policy_error", text="invalid rules")
        self.assertFalse(old.alerts)
        self.assertEqual(observer.snapshot.alerts[0].since, 10)
        observer("condition", key="policy_error", text=None)
        self.assertFalse(observer.snapshot.alerts)
        observer("sleeping", seconds=60)
        self.assertEqual(observer.snapshot.runtime.next_check_at, 70)


@unittest.skipUnless(importlib.util.find_spec("rich"), "Rich is optional")
class RenderTests(unittest.TestCase):
    def test_github_identity_header(self):
        screen = fixture_screen()
        for actual, expected in (("me", "gh me ✓"), ("other", "gh ✗ other"), (None, "gh …")):
            runtime = dataclasses.replace(screen.runtime, login="me", gh_login=actual, gh_confirmed=True if actual else None)
            shown = dataclasses.replace(screen, runtime=runtime)
            self.assertIn(expected, self.render_text(shown, width=140))
        runtime = dataclasses.replace(screen.runtime, login="me", gh_login="me", gh_confirmed=True)
        self.assertIn("gh me ok", self.render_text(dataclasses.replace(screen, runtime=runtime), ascii=True))

    def render_text(self, screen, width=100, height=30, ascii=False, mono=100):
        from rich.console import Console
        from lightkeeper.ui.rich_view import render
        console = Console(width=width, height=height, record=True, file=io.StringIO(), color_system=None)
        console.print(render(screen, mono, width, height, ascii))
        return console.export_text()

    def test_all_glyphs_are_one_cell(self):
        from rich.cells import cell_len
        from lightkeeper.ui.rich_view import STATES, SPINNER
        for g in [*(s[0] for s in STATES.values()), *(s[1] for s in STATES.values()), *SPINNER, "◆", "▲"]:
            self.assertEqual(cell_len(g), 1, g)

    def test_author_ci_width_ascii_and_history(self):
        from rich.cells import cell_len
        from lightkeeper.ui.model import HistoryRow
        from lightkeeper.ui.rich_view import ci_text
        item = ItemRef("pr", "example/widget", 42, "abcdefa", "A sample change", author="contributor-long-name")
        rows = (ItemRow(item, "waiting", "checks running", NOW, ci=CICounts(14, 12, 1, 1)),
                ItemRow(dataclasses.replace(item, number=43, author="writer"), "waiting", "draft", NOW, ci=CICounts()),
                ItemRow(dataclasses.replace(item, number=44, author="writer"), "waiting", "draft", NOW,
                        ci=CICounts(4, 1, 1, 1, 1)))
        screen = dataclasses.replace(fixture_screen(), watching=rows,
                                     history=(HistoryRow(item, "approved", "", NOW),))
        for width in (100, 160):
            for ascii in (False, True):
                output = self.render_text(screen, width=width, ascii=ascii)
                watching, history = output.split("HISTORY")
                self.assertTrue(all(cell_len(line) <= width for line in output.splitlines()))
                self.assertIn("contributor-l…", watching)
                self.assertEqual("contributor-l…" in history, width >= 140)
                self.assertIn("+12 o1 x1" if ascii else "✓12 ○1 ✗1", watching)
                self.assertIn("+1 o1 x1 -1" if ascii else "✓1 ○1 ✗1 –1", watching)
                no_checks = next(line for line in watching.splitlines() if "#43" in line)
                self.assertIn("-" if ascii else "–", no_checks)
                if ascii:
                    self.assertNotIn("✓", output)
                    self.assertNotIn("○", output)
                    self.assertNotIn("✗", output)
        styled = ci_text(CICounts(3, 1, 1, 1))
        self.assertEqual({span.style for span in styled.spans}, {"green", "yellow", "red"})
        self.assertEqual(ci_text(None).plain, "?")

    def test_wake_phase_and_author_controls_are_literal(self):
        screen = fixture_screen()
        row = ItemRow(ItemRef("pr", "example/widget", 42, author="writer\x1b[2J"), "waiting", "draft", NOW)
        screen = dataclasses.replace(screen, watching=(row,), runtime=dataclasses.replace(
            screen.runtime, phase="checking", phase_reason="new push on example/widget#42"))
        output = self.render_text(screen)
        self.assertIn("checking now (new push on example/widget#42)", output)
        self.assertIn("writer", output)
        self.assertNotIn("\x1b", output)

    def test_width_height_priority_and_literal_text(self):
        from rich.cells import cell_len
        base = fixture_screen()
        extra = tuple(ItemRow(ItemRef("pr", "example/widget", n), "waiting", "reason " * 20, NOW - n)
                      for n in range(20, 50))
        screen = dataclasses.replace(base, watching=base.watching + extra,
                                     history=(base.history * 3)[:15])
        for width in (100, 160):
            for height in (20, 30):
                with self.subTest(width=width, height=height):
                    output = self.render_text(screen, width, height)
                    self.assertLessEqual(len(output.splitlines()), height)
                    self.assertTrue(all(cell_len(line) <= width for line in output.splitlines()))
                    self.assertIn("◆", output)
                    self.assertIn("check the retry limit", output)
                    self.assertIn("▲ search hit its result limit", output)
                    self.assertIn("more", output)
        output = self.render_text(base, height=40)
        self.assertIn("[bold red]x[/]", output)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("[2J", output)

    def test_multiple_priority_rows_and_alerts_survive(self):
        screen = fixture_screen()
        rows = tuple(ItemRow(ItemRef("pr", "example/widget", n), "waiting", "wait", NOW,
                             f"priority {n}" if n in (25, 28, 29) else None) for n in range(20, 50))
        alerts = tuple(Alert(str(n), "warning", f"condition {n}", 0) for n in range(4))
        screen = dataclasses.replace(screen, watching=screen.watching + rows, alerts=alerts,
                                     stats=Stats(dry_run=(("approve", 100), ("request_changes", 100), ("comment", 100))))
        for height in (20, 30):
            for width in (100, 160):
                output = self.render_text(screen, width, height)
                self.assertLessEqual(len(output.splitlines()), height)
                for n in (25, 28, 29):
                    self.assertIn(f"priority {n}", output)
                self.assertIn("◆ 4 needs you", output.splitlines()[1])
                self.assertIn("+1 more alerts", output)
                for n in range(3):
                    self.assertIn(f"▲ condition {n}", output)

    def test_ascii_overdue_long_label_and_no_color(self):
        from rich.cells import cell_len
        s = fixture_screen()
        row = ItemRow(ItemRef("pr", "example/" + "long" * 20, 123456), "waiting", "wait", NOW, "look")
        s = dataclasses.replace(s, watching=(row,))
        output = self.render_text(s, ascii=True, mono=200)
        self.assertIn("overdue", output)
        self.assertIn("#123456", output)
        self.assertNotIn("◆", output)
        self.assertTrue(all(cell_len(line) <= 100 for line in output.splitlines()))

    def test_last_check_clock_is_stable_between_frames(self):
        screen = dataclasses.replace(fixture_screen(), mono_at=100)
        first = self.render_text(screen, width=160, mono=100)
        later = self.render_text(screen, width=160, mono=130)
        self.assertEqual(first.split("last check ")[1].splitlines()[0],
                         later.split("last check ")[1].splitlines()[0])

    def test_exit_summary(self):
        from rich.console import Console
        from lightkeeper.ui.rich_view import print_summary
        console = Console(width=100, file=io.StringIO(), record=True)
        print_summary(fixture_screen(), console)
        output = console.export_text()
        self.assertIn("lightkeeper stopped", output)
        self.assertIn("needs you: widget#1", output)
        self.assertIn("alerts: search hit its result limit", output)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        from tests.helpers import isolate_cli
        isolate_cli(self, self.temp.name)
        self.journal = Journal(Path(self.temp.name) / "journal.jsonl")
        self.gh = FakeGh()
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        self.notifier = Mock()

    def runner(self, decisions, observer=None):
        return Runner(make_settings(), self.gh, self.journal, FakeReviewer(decisions), notifier=self.notifier,
                      observer=observer, live_lock_held=True)

    def test_dedupe_policy_change_and_block_warning(self):
        d = Decision("nothing", "same verdict", "ai", notify="look at this")
        r = self.runner([d, d, dataclasses.replace(d, outcome="comment", summary="Please check", why="Not approving yet: CI is pending.")])
        r.tick()
        r.refresh_settings(make_settings(rules="changed rules"))
        r.tick()
        self.assertEqual(self.notifier.call_count, 1)
        r.refresh_settings(make_settings(rules="different rules"))
        r.tick()
        self.assertEqual(self.notifier.call_count, 2)
        old = Review("me", "CHANGES_REQUESTED", "0" * 40, marker_line("opus", "abcdef", "0" * 40))
        self.gh.pr_snapshots[("org/repo", 8)] = [make_pr(number=8, reviews=(old,))]
        r.reviewer = FakeReviewer([d])
        r.process_pr("org/repo", 8)
        self.assertEqual(self.notifier.call_count, 3)
        self.assertIn("still blocks", self.notifier.call_args.args[1])
        self.assertTrue(self.journal.was_notified(("org/repo", 8, HEAD), "block-active"))

    def test_observer_phase_order_and_conditions(self):
        events = []
        r = self.runner([Decision("approve", "ok", "ai")], lambda event, **kw: events.append((event, kw)))
        r.tick()
        phases = [kw["phase"] for event, kw in events if event == "phase"]
        self.assertEqual(phases, ["checking", "checking", "reviewing", "checking", "writing"])
        self.assertEqual(events[-1][0], "tick_done")
        self.assertTrue(events[-1][1]["ok"])
        self.gh.search_review_requests = Mock(side_effect=GhError([], 1, "HTTP 429 rate limited"))
        r.tick()
        self.assertTrue(any(kw.get("key") == "tick_failed" and kw.get("text") for _, kw in events))
        r.sleeping(120, "backoff")
        self.assertEqual(events[-1], ("sleeping", {"seconds": 120, "phase": "backoff"}))

    def test_interrupted_posts_are_unknown(self):
        for exc in (KeyboardInterrupt(), SystemExit(143)):
            with self.subTest(exc=type(exc)):
                key = ("org/repo", 7, HEAD)
                self.journal = Journal(Path(self.temp.name) / (type(exc).__name__ + ".jsonl"))
                self.gh.fail_write = exc
                r = self.runner([Decision("approve", "ok", "ai")])
                with self.assertRaises(type(exc)):
                    r.tick()
                self.assertTrue(self.journal.is_unknown(key))
        self.gh.pr_snapshots.clear()
        run = make_run()
        self.gh.runs = [run]
        self.gh.pending[(run.repo, run.run_id)] = [PENDING_PROD]
        r = self.runner([Decision("approve", "ok", "ai")])
        with self.assertRaises(SystemExit):
            r.process_run(run.repo, run.run_id)
        self.assertTrue(self.journal.is_unknown((run.repo, run.run_id, run.attempt, PENDING_PROD.env_id)))

    def test_pending_gate_disappearance(self):
        self.gh.pr_snapshots.clear()
        run = make_run()
        self.gh.runs = [run]
        self.gh.notifications = [(run.repo, run.run_id, "updated")]
        self.gh.pending[(run.repo, run.run_id)] = [PENDING_PROD]
        p = Projection()
        self.journal.observer = p.feed
        r = self.runner([Decision("ignore", "manual", "ai")])
        r.tick()
        self.assertEqual(len(p.screen(Runtime(repos=(run.repo,)), NOW).watching), 1)
        self.gh.pending[(run.repo, run.run_id)] = []
        r.tick()
        self.assertFalse(p.screen(Runtime(repos=(run.repo,)), NOW).watching)

    def test_gate_can_reappear_at_same_attempt(self):
        self.gh.pr_snapshots.clear()
        run = make_run()
        self.gh.runs = [run]
        self.gh.pending[(run.repo, run.run_id)] = [PENDING_PROD]
        p = Projection()
        self.journal.observer = p.feed
        r = self.runner([Decision("ignore", "manual", "ai")])
        r.process_run(run.repo, run.run_id)
        self.gh.pending[(run.repo, run.run_id)] = []
        r.process_run(run.repo, run.run_id)
        self.gh.pending[(run.repo, run.run_id)] = [PENDING_PROD]
        r.process_run(run.repo, run.run_id)
        self.assertEqual(len(p.screen(Runtime(repos=(run.repo,)), NOW).watching), 1)
        self.assertTrue(self.journal.pending_gates())

    def test_approval_note_reaches_history(self):
        p = Projection()
        self.journal.observer = p.feed
        self.runner([Decision("approve", "safe", "ai", summary="Please adjust the description.")]).tick()
        s = p.screen(Runtime(repos=("org/repo",)), NOW)
        self.assertEqual(s.history[0].why, "Please adjust the description.")

    def test_child_stdin_is_devnull_and_log_rotation_stays_private(self):
        from lightkeeper.__main__ import macos_notify, _logger
        from lightkeeper.github import Gh
        child = Mock(return_value=subprocess.CompletedProcess([], 0, "ok", ""))
        self.assertEqual(Gh(runner=child).run(["version"]), "ok")
        self.assertEqual(child.call_args.kwargs["stdin"], subprocess.DEVNULL)
        with patch("lightkeeper.__main__.subprocess.run", child), patch("sys.platform", "darwin"):
            macos_notify("title", "text")
        self.assertEqual(child.call_args.args[0][0], "osascript")
        self.assertEqual(child.call_args.kwargs["stdin"], subprocess.DEVNULL)
        configure_logging(Path(self.temp.name))
        handler = _logger.handlers[0]
        self.assertEqual((handler.maxBytes, handler.backupCount), (5 * 1024 * 1024, 3))
        log("before rollover")
        handler.doRollover()
        log("after rollover")
        self.assertEqual((Path(self.temp.name) / "run.log.1").stat().st_mode & 0o777, 0o600)
        self.assertEqual((Path(self.temp.name) / "run.log").stat().st_mode & 0o777, 0o600)

    def test_plain_mode_and_signal(self):
        stdout = Mock()
        stdout.isatty.return_value = False
        self.assertIsNone(live_screen_class(stdout=stdout))
        stdout.isatty.return_value = True
        self.assertIsNone(live_screen_class(True, stdout))
        with patch.dict(os.environ, TERM="dumb"):
            self.assertIsNone(live_screen_class(stdout=stdout))
        with patch.dict(os.environ, TERM="xterm"), patch.dict("sys.modules", {"lightkeeper.ui.textual_app": None}):
            self.assertIsNone(live_screen_class(stdout=stdout))
        with self.assertRaises(SystemExit) as ctx:
            _terminate(signal.SIGTERM, None)
        self.assertEqual(ctx.exception.code, 143)

    def test_once_status_plain_and_private_log(self):
        path = Path(self.temp.name)
        with patch("lightkeeper.__main__.load_settings", return_value=make_settings()), \
             patch("lightkeeper.__main__.Gh", return_value=self.gh), \
             patch("lightkeeper.__main__.ClaudeReviewer", return_value=FakeReviewer([Decision("approve", "ok", "ai")])), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["--once", "--plain", "--config-dir", str(path / "config"), "--state-dir", str(path)]), 0)
            self.assertIn("would post approve", output.getvalue())
            self.assertFalse(self.gh.writes)
            self.assertEqual(main(["--status", "--config-dir", str(path / "config"), "--state-dir", str(path)]), 0)
        self.assertEqual((path / "run.log").stat().st_mode & 0o777, 0o600)
        configure_logging(path, plain=False)
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            log("file only")
            self.assertEqual(output.getvalue(), "")
        self.assertIn("file only", (path / "run.log").read_text())
        configure_logging(path, plain=True)


class RealJournalRegressions(unittest.TestCase):
    """Journal event shapes with missing or historical metadata."""

    def feed(self, *records):
        projection = Projection()
        for i, (event, extra) in enumerate(records):
            projection.feed({"kind": "pr", "key": ["org/widget", 1, "a" * 40], "event": event,
                             "at": f"2026-10-10T10:{i:02d}:00Z", "policy": extra.pop("policy", "p1"), **extra})
        return projection.screen(Runtime(repos=("org/widget",)), 1_791_626_400)

    def test_block_survives_a_silent_reevaluation(self):
        screen = self.feed(("seen", {}), ("applied", {"action": "request_changes", "reason": "secret"}),
                           ("nothing", {"reason": "my block still applies", "policy": "p2"}))
        self.assertEqual(screen.watching[0].state, "changes")
        self.assertEqual([h.state for h in screen.history], ["silent", "changes"])   # not a second posted block

    def test_needs_you_drops_the_repeated_item_name(self):
        screen = self.feed(("seen", {}), ("notify", {"text": "org/widget#1: look at the retry limit"}))
        self.assertEqual(screen.watching[0].needs_you, "look at the retry limit")

    def test_closing_is_never_marked_new_rules(self):
        screen = self.feed(("seen", {}), ("nothing", {"reason": "x"}), ("closed", {"reason": "pull request is merged", "policy": "p2"}))
        self.assertFalse(screen.history[0].new_rules)
