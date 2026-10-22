"""Small GraphQL snapshots and deterministic sleep tests; no network or real time waits."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from lightkeeper.__main__ import FAST_POLL_S, Runner, _loop
from lightkeeper.github import Gh, GhError, GhTimeout, PRStatus, parse_pr_statuses, parse_pull_request
from lightkeeper.journal import Journal
from lightkeeper.models import CICounts, Check, Decision
from lightkeeper.ui.model import Runtime
from lightkeeper.ui.project import Observer, Projection
from tests.helpers import FakeGh, make_settings, make_pr
from tests.test_github import FakeRunner

FIXTURE = Path(__file__).parent / "fixtures/pr_statuses.json"


class StatusTests(unittest.TestCase):
    def test_response_missing_pr_no_checks_and_full_fetch_agree(self):
        response = json.loads(FIXTURE.read_text())
        statuses = parse_pr_statuses(response, [("example/widget", i) for i in (1, 2, 3)])
        self.assertEqual([s.number for s in statuses], [1, 3])
        first, empty = statuses
        self.assertEqual(first.ci, CICounts(8, 3, 2, 1, 2))
        self.assertEqual((first.author, first.title), ("contributor", "Update the sample widget"))
        self.assertEqual((empty.ci, empty.author), (CICounts(), ""))
        pr = response["data"]["pr0"]["pullRequest"]
        contexts = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        full = PRStatus.from_pr(parse_pull_request("example/widget", {
            **pr, "number": 1, "statusCheckRollup": contexts["nodes"]
        }, [], []))
        self.assertEqual(first.counts, full.counts)
        self.assertEqual(first.ci, full.ci)
        self.assertEqual(first.wake_key, full.wake_key)

    def test_ci_groups_do_not_change_eligibility(self):
        states = ("success", "queued", "in_progress", "pending", "failure", "error", "timed_out",
                  "cancelled", "action_required", "startup_failure", "skipped", "neutral", "stale")
        checks = tuple(Check(s, s if s in ("queued", "in_progress", "pending") else "completed",
                             "" if s in ("queued", "in_progress", "pending") else s) for s in states)
        self.assertEqual(PRStatus.from_pr(make_pr(checks=checks)).ci, CICounts(13, 1, 3, 6, 3))
        self.assertTrue(checks[-1].failed)  # stale remains a policy failure

    def test_unfinished_commit_status_matches_full_fetch_normalization(self):
        response = json.loads(FIXTURE.read_text())
        response.pop("errors")
        pr = response["data"]["pr0"]["pullRequest"]
        contexts = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        contexts["nodes"] = [{"context": "test", "state": "EXPECTED"}]
        light = parse_pr_statuses(response, [("example/widget", 1)])[0]
        full = PRStatus.from_pr(parse_pull_request("example/widget", {
            **pr, "number": 1, "statusCheckRollup": contexts["nodes"]
        }, [], []))
        self.assertEqual(light.counts, full.counts)
        self.assertEqual(light.ci, full.ci)
        self.assertEqual(light.wake_key, full.wake_key)

    def test_unreported_declared_check_does_not_wake(self):
        response = json.loads(FIXTURE.read_text())
        response.pop("errors")
        pr = response["data"]["pr0"]["pullRequest"]
        contexts = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        contexts["nodes"] = [{"name": "unit", "status": "COMPLETED", "conclusion": "SUCCESS"}]
        light = parse_pr_statuses(response, [("example/widget", 1)])[0]
        full = PRStatus.from_pr(parse_pull_request("example/widget", {
            **pr, "number": 1, "statusCheckRollup": contexts["nodes"]
        }, [], [], {"unit": False}, ["external"]))
        self.assertEqual(light.counts, full.counts)
        self.assertEqual(light.ci, full.ci)
        self.assertEqual(light.wake_key, full.wake_key)
        self.assertEqual(full.ci.running, 0)

    def test_superseded_runs_match_full_fetch(self):
        old = dict(__typename="CheckRun", name="unit", status="COMPLETED", conclusion="CANCELLED",
                   startedAt="2026-10-20T10:00:00Z", completedAt="2026-10-20T12:00:00Z")
        new = dict(old, conclusion="SUCCESS", startedAt="2026-10-20T11:00:00Z")
        queued = dict(old, status="QUEUED", conclusion=None, startedAt=None, completedAt=None)
        legacy = [{"__typename": "StatusContext", "context": "legacy", "state": state}
                  for state in ("ERROR", "SUCCESS")]
        for runs, expected in (([old, new], CICounts(1, 1)), ([new, old], CICounts(1, 1)),
                               ([new, queued], CICounts(1, running=1)), (legacy, CICounts(1, 1))):
            with self.subTest(runs=runs):
                response = json.loads(FIXTURE.read_text())
                pr = response["data"]["pr0"]["pullRequest"]
                contexts = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
                contexts["nodes"] = runs
                light = parse_pr_statuses(response, [("example/widget", 1)])[0]
                full = PRStatus.from_pr(parse_pull_request("example/widget", {
                    **pr, "number": 1, "statusCheckRollup": runs
                }, [], []))
                self.assertEqual(light.ci, expected)
                self.assertEqual(light.counts, full.counts)
                self.assertEqual(light.ci, full.ci)
                self.assertEqual(light.wake_key, full.wake_key)

    def test_truncated_contexts_skip_only_that_pr_and_warn_once_per_head(self):
        response = json.loads(FIXTURE.read_text())
        pr = response["data"]["pr0"]["pullRequest"]
        contexts = pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        contexts["pageInfo"]["hasNextPage"] = True
        prs = [("example/widget", i) for i in (1, 2, 3)]
        self.assertEqual([s.number for s in parse_pr_statuses(response, prs)], [3])
        child = FakeRunner(stdout=json.dumps(response))
        gh = Gh(runner=child)
        with self.assertLogs("lightkeeper", level="WARNING") as logs:
            for _ in range(2):
                self.assertEqual([s.number for s in gh.pr_statuses(prs)], [3])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("example/widget#1", logs.output[0])
        self.assertIn("incomplete; skipping", logs.output[0])
        pr["headRefOid"] = "c" * 40
        child.stdout = json.dumps(response)
        with self.assertLogs("lightkeeper", level="WARNING"):
            gh.pr_statuses(prs)
        contexts["pageInfo"]["hasNextPage"] = False
        child.stdout = json.dumps(response)
        with self.assertNoLogs("lightkeeper", level="WARNING"):
            self.assertEqual([s.number for s in gh.pr_statuses(prs)], [1, 3])

    def test_batches_variables_and_partial_cli_error(self):
        child = FakeRunner(stdout='{"data": {}}')
        gh = Gh(runner=child)
        gh.pr_statuses([("example/widget", n) for n in range(51)])
        self.assertEqual(len(child.calls), 2)
        for (args, _), count in zip(child.calls, (50, 1)):
            query = next(a for a in args if a.startswith("query="))
            self.assertEqual(query.count("pullRequest(number:"), count)
            self.assertIn("$owner0:String!", query)
            self.assertIn("$number0:Int!", query)
            self.assertNotIn("example", query)
            self.assertNotIn("widget", query)
            self.assertIn("owner0=example", args)
            self.assertIn("name0=widget", args)
            self.assertIn("contexts(first:100)", query)
            self.assertIn("pageInfo { hasNextPage }", query)
            self.assertIn("name status conclusion startedAt completedAt", query)
            self.assertIn("context state", query)
            self.assertNotIn("CountsByState", query)
        self.assertIn("number0=50", child.calls[1][0])
        child = FakeRunner(stdout=FIXTURE.read_text(), returncode=1, stderr="gh: Could not resolve a PullRequest")
        self.assertEqual(len(Gh(runner=child).pr_statuses([("example/widget", i) for i in (1, 2, 3)])), 2)
        child.stdout = '{"data": {}, "errors": [{"type":"RATE_LIMITED"}]}'
        with self.assertRaises(ValueError):
            Gh(runner=child).pr_statuses([("example/widget", 1)])
        child.calls.clear()
        self.assertEqual(Gh(runner=child).pr_statuses([]), [])
        self.assertFalse(child.calls)

    def test_batch_sequence_shares_one_timeout_budget(self):
        child = FakeRunner(stdout='{"data": {}}')
        with patch("lightkeeper.github.time.monotonic", side_effect=[0, 1, 11]):
            with self.assertRaises(GhTimeout):
                Gh(runner=child).pr_statuses([("example/widget", n) for n in range(51)], timeout_s=10)
        self.assertEqual(len(child.calls), 1)
        self.assertEqual(child.calls[0][1]["timeout"], 9)
        child = FakeRunner(stdout='{"data": {}}')
        with patch("lightkeeper.github.time.monotonic", side_effect=[0, 1, 7]):
            Gh(runner=child).pr_statuses([("example/widget", n) for n in range(51)], timeout_s=10)
        self.assertEqual([kw["timeout"] for _, kw in child.calls], [9, 3])

    def test_bad_stdout_preserves_transport_error(self):
        child = FakeRunner(stdout="<html>gateway failure</html>", stderr="HTTP 502", returncode=1)
        with self.assertRaises(GhError) as error:
            Gh(runner=child).pr_statuses([("example/widget", 1)])
        self.assertEqual(error.exception.status, 502)


class ClockStop:
    def __init__(self):
        self.now = 0
        self.waits = []
        self.stopped = False

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        return self.stopped

    def is_set(self):
        return self.stopped


class PollTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "journal.jsonl"
        self.observer = Observer(Projection(), Runtime(repos=("org/repo",)))
        self.journal = Journal(self.path, observer=self.observer.feed)
        self.gh = FakeGh()
        self.pr = make_pr(is_draft=True, author="contributor")
        self.gh.pr_snapshots[(self.pr.repo, self.pr.number)] = [self.pr]
        self.runner = Runner(make_settings(), self.gh, self.journal, observer=self.observer, notifier=Mock(), live_lock_held=True)
        self.runner.process_pr(self.pr.repo, self.pr.number)
        self.status = PRStatus.from_pr(self.pr)
        self.gh.pr_statuses = Mock(return_value=[self.status])

    def test_author_replayed_and_ci_runtime_only(self):
        self.assertEqual(self.observer.snapshot.watching[0].ci, CICounts(1, 1))
        projection = Projection()
        journal = Journal(self.path, observer=projection.feed)
        journal.replay()
        row = projection.screen(Runtime(repos=(self.pr.repo,)), 0).watching[0]
        self.assertEqual(row.item.author, "contributor")
        self.assertIsNone(row.ci)
        self.assertFalse(journal.observation_changed("pr", (self.pr.repo, self.pr.number, self.pr.head_sha),
                                                     self.pr.title, author=self.pr.author))
        self.assertNotIn('"ci"', self.path.read_text())

    def test_wake_rule_and_metadata_refresh(self):
        changed = replace(self.status, updated_at="2026-10-10T12:00:00Z", title="New title", author="writer")
        self.gh.pr_statuses.return_value = [changed]
        records = self.journal.records
        self.runner.sleeping(60)
        deadline = self.observer.snapshot.runtime.next_check_at
        self.assertIsNone(self.runner.light_poll())
        self.assertEqual(self.observer.snapshot.runtime.next_check_at, deadline)
        row = self.observer.snapshot.watching[0]
        self.assertEqual((row.item.author, row.item.title), ("writer", "New title"))
        self.assertEqual(self.journal.records, records)
        for fields, reason in (({"head": "c" * 40}, "new push"), ({"is_draft": False}, "draft changed"),
                               ({"state": "CLOSED"}, "state changed"),
                               ({"counts": (("failure", 1),), "ci": CICounts(1, failed=1)}, "CI ")):
            changed = replace(changed, **fields)
            self.gh.pr_statuses.return_value = [changed]
            self.assertIn(reason, self.runner.light_poll())
            self.assertIsNone(self.runner.light_poll())
        self.assertIn((self.pr.repo, self.pr.number), self.journal.watched_prs())

    def test_prewrite_full_fetch_refreshes_baseline(self):
        fresh = replace(self.pr, head_sha="d" * 40)
        self.gh.pr_snapshots[(self.pr.repo, self.pr.number)] = [fresh]
        self.assertEqual(self.runner._write_pr(self.pr, Decision("comment", "note", "ai", body="Check this")),
                         {"defer": "head moved during assessment"})
        self.gh.pr_statuses.return_value = [PRStatus.from_pr(fresh)]
        self.assertIsNone(self.runner.light_poll())
        self.assertFalse(self.gh.writes)

    def test_missing_no_scope_and_quarantined(self):
        self.gh.pr_statuses.return_value = []
        self.assertIsNone(self.runner.light_poll())
        self.runner.quarantined.add(("pr", self.pr.repo, self.pr.number))
        self.gh.pr_statuses.reset_mock()
        self.runner.light_poll()
        self.gh.pr_statuses.assert_not_called()
        self.runner.quarantined.clear()
        self.runner.refresh_settings(make_settings(repos=["example/elsewhere"]))
        self.runner.light_poll()
        self.gh.pr_statuses.assert_not_called()

    def test_failure_interval_cap_recovery_and_single_log(self):
        for expected in (30, 60, 60):
            self.gh.pr_statuses.side_effect = GhError([], 1, "HTTP 429")
            with patch("lightkeeper.__main__.log") as log:
                self.assertIsNone(self.runner.light_poll())
            log.assert_called_once()
            self.assertEqual(self.runner.fast_poll_interval, expected)
        self.gh.pr_statuses.side_effect = None
        self.runner.light_poll()
        self.assertEqual(self.runner.fast_poll_interval, FAST_POLL_S)

    def test_sleep_deadline_early_wake_backoff_and_poll_spacing(self):
        clock = ClockStop()
        self.runner.wake.wait = clock.wait
        self.gh.pr_statuses.return_value = [replace(self.status, head="c" * 40)]
        self.runner.sleeping(60)
        self.runner.wait(clock, 60, mono=lambda: clock.now)
        self.assertEqual(clock.now, 15)
        self.assertIn("new push", self.observer.snapshot.runtime.phase_reason)
        self.gh.pr_statuses.return_value = [replace(self.status, head="d" * 40)]
        self.runner.wait(clock, 60, mono=lambda: clock.now)
        self.assertEqual(clock.now, 30)  # never two wakes inside fifteen seconds
        self.gh.pr_statuses.return_value = [replace(self.status, head="e" * 40)]
        self.runner.wait(clock, 120, allow_wake=False, mono=lambda: clock.now)
        self.assertEqual(clock.now, 150)
        self.assertTrue(all(s <= 15 for s in clock.waits))
        self.gh.pr_statuses.side_effect = ValueError("broken JSON")
        times = []
        def broken(prs, **kwargs):
            times.append(clock.now)
            raise ValueError("broken JSON")
        self.gh.pr_statuses.side_effect = broken
        self.runner.wait(clock, 120, mono=lambda: clock.now)
        self.assertEqual(times, [165, 195, 255])

    def test_poll_budget_fits_remaining_sleep_and_rate_limit_skips(self):
        clock = ClockStop()
        self.runner.wake.wait = clock.wait
        self.runner.wait(clock, 20, mono=lambda: clock.now)
        self.gh.pr_statuses.assert_called_once_with([(self.pr.repo, self.pr.number)], timeout_s=5)
        self.gh.pr_statuses.reset_mock()
        self.runner._rate_limited = True
        self.runner.wait(clock, 600, allow_wake=False, mono=lambda: clock.now)
        self.gh.pr_statuses.assert_not_called()
        self.assertEqual(clock.now, 620)

    def test_loop_once_and_failures_do_not_wake(self):
        stop = ClockStop()
        args = SimpleNamespace(policy="unused", once=True, dry_run=False, allow_live=False)
        with patch("lightkeeper.__main__.load_settings", return_value=make_settings()):
            self.assertEqual(_loop(args, make_settings(), self.runner, stop), 0)
            self.gh.pr_statuses.assert_not_called()
            self.assertFalse(stop.waits)
            args.once = False
            for whole_tick in (False, True):
                stop.stopped = False
                def tick():
                    self.runner._tick_failures = int(whole_tick)
                    self.runner._error_ticks = 1
                    if whole_tick:
                        raise GhError([], 1, "HTTP 429")
                    return {"error": 1}
                self.runner.tick = tick
                self.runner.wait = Mock(side_effect=lambda *a, **kw: setattr(stop, "stopped", True))
                _loop(args, make_settings(), self.runner, stop)
                self.runner.wait.assert_called_once_with(stop, 120 if whole_tick else 60, allow_wake=False)

    def test_stop_interrupts_wait_without_polling(self):
        stop = ClockStop()
        stop.stopped = True
        self.runner.wait(stop, 60, mono=lambda: stop.now)
        self.gh.pr_statuses.assert_not_called()


class WakeOnlyOnMeaningfulCI(unittest.TestCase):
    def status(self, passed, running, failed):
        from lightkeeper.github import PRStatus
        from lightkeeper.models import CICounts
        return PRStatus("org/widget", 1, "OPEN", "a" * 40, False, "dev", "t", (),
                        CICounts(passed + running + failed, passed, running, failed, 0))

    def test_running_progress_does_not_wake_but_finish_and_failure_do(self):
        self.assertEqual(self.status(3, 5, 0).wake_key, self.status(6, 2, 0).wake_key)    # progress only
        self.assertNotEqual(self.status(6, 2, 0).wake_key, self.status(8, 0, 0).wake_key)  # all finished
        self.assertNotEqual(self.status(6, 2, 0).wake_key, self.status(5, 2, 1).wake_key)  # a check failed
