import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from lightkeeper.__main__ import REVIEWER_PAUSE_S, Runner
from lightkeeper.github import GhError, GhTimeout
from lightkeeper.journal import Journal
from lightkeeper.models import Check, Decision, MARKER, PendingEnvironment, Review, marker_line
from lightkeeper.reviewer import ReviewerError, UnclearAnswer
from tests.helpers import (APPLY_JOB, FakeGh, FakeReviewer, HEAD, PENDING_PROD, PLAN_JOB, PLAN_LOG_OK, make_file,
                           make_settings, make_pr, make_run)

APPROVE = Decision("approve", "looks fine", "ai", why="Approved: the tests cover the change.")
IGNORE = Decision("ignore", "unclear", "ai")
NOTHING = Decision("nothing", "unclear", "ai")
COMMENT = Decision("comment", "ci broke", "ai", summary="CI failed in sast: secret pattern in config.py.",
                   why="Not approving yet: CI failed in sast.")
BLOCK = Decision("request_changes", "drops a table", "ai", summary="Migration drops `users`; please make it additive.",
                 notify="org/repo#7 drops a table", why="Requesting changes: the migration drops a table.")


class LifecycleBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.dir.name) / "journal.jsonl")
        self.gh = FakeGh()
        self.now = 1_000_000.0
        self.notified = []

    def tearDown(self):
        self.dir.cleanup()

    def runner(self, policy=None, reviewer=None):
        return Runner(policy or make_settings(), self.gh, self.journal, reviewer, clock=lambda: self.now,
                      notifier=lambda title, text: self.notified.append(text), live_lock_held=True)

    def events(self):
        if not self.journal.path.exists():
            return []
        return [(r["event"], r.get("reason", "")) for r in
                (json.loads(line) for line in self.journal.path.read_text().splitlines())]


class PullRequestLifecycle(LifecycleBase):
    def test_comment_then_push_then_approve_is_final_on_third_tick(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        reviewer = FakeReviewer([COMMENT, APPROVE])
        r = self.runner(reviewer=reviewer)
        self.assertEqual(r.tick(), {"applied": 1})
        comment = Review("me", "COMMENTED", HEAD, self.gh.writes[0][5])
        h2 = make_pr(head_sha="f" * 40, requested_users=(), reviews=(comment,))
        self.gh.search_results = []
        self.gh.pr_snapshots[("org/repo", 7)] = [h2]
        self.assertEqual(r.tick(), {"applied": 1})
        approval = Review("me", "APPROVED", h2.head_sha, self.gh.writes[1][5])
        self.gh.pr_snapshots[("org/repo", 7)] = [dataclasses.replace(h2, reviews=(comment, approval))]
        self.assertEqual(r.tick(), {"final": 1})
        self.assertEqual([w[3:5] for w in self.gh.writes], [(HEAD, "comment"), (h2.head_sha, "approve")])
        self.assertEqual(reviewer.calls, 2)

    def test_dismissed_approval_notifies_once_even_after_restart(self):
        reviewer = FakeReviewer([APPROVE])
        r = self.runner(reviewer=reviewer)
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        self.assertEqual(r.tick(), {"applied": 1})
        approval = Review("me", "APPROVED", HEAD, self.gh.writes[0][5])
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(requested_users=(), reviews=(approval,))]
        self.assertEqual(r.tick(), {"final": 1})
        self.assertEqual(self.notified, [])
        dismissed = dataclasses.replace(approval, state="DISMISSED")
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(requested_users=(), reviews=(dismissed,))]
        self.assertEqual(r.tick(), {"final": 1})
        self.journal = Journal(self.journal.path)
        self.journal.replay()
        r = self.runner(reviewer=reviewer)
        r.refresh_settings(make_settings(rules="# Rules\n- changed rules\n"))
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(body="edited", reviews=(dismissed,))]
        self.assertEqual(r.tick(), {"final": 1})
        self.assertEqual((reviewer.calls, len(self.gh.writes), len(self.notified)), (1, 1, 1))
        self.assertIn("dismissed on this head", self.notified[0])

    def test_observed_approval_then_dismissal_notifies_without_model(self):
        approval = Review("me", "APPROVED", HEAD, marker_line("opus", "abcdef", HEAD))
        reviewer = FakeReviewer([])
        r = self.runner(reviewer=reviewer)
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(reviews=(approval,))]
        self.assertEqual(r.tick(), {"satisfied": 1})
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(reviews=(dataclasses.replace(approval, state="DISMISSED"),))]
        self.assertEqual(r.tick(), {"final": 1})
        self.assertEqual(r.tick(), {"final": 1})
        self.assertEqual((reviewer.calls, self.gh.writes, len(self.notified)), (0, [], 1))

    def test_dismissed_head_without_prior_journal_cannot_be_approved(self):
        dismissed = Review("me", "DISMISSED", HEAD, marker_line("opus", "abcdef", HEAD))
        reviewer = FakeReviewer([])
        r = self.runner(reviewer=reviewer)
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(reviews=(dismissed,))]
        self.assertEqual(r.tick(), {"ignore": 1})
        self.assertEqual(r.tick(), {"final": 1})
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(body="edited", reviews=(dismissed,))]
        self.assertEqual(r.tick(), {"ignore": 1})
        self.assertEqual((reviewer.calls, self.gh.writes, len(self.notified)), (0, [], 1))

    def test_approval_on_next_head_with_or_without_old_dismissal(self):
        reviewer = FakeReviewer([APPROVE] * 4)
        r = self.runner(reviewer=reviewer)
        for number, state in ((7, "APPROVED"), (8, "DISMISSED")):
            with self.subTest(state=state):
                self.gh.pr_snapshots[("org/repo", number)] = [make_pr(number=number)]
                self.assertEqual(r.process_pr("org/repo", number), "applied")
                own = Review("me", state, HEAD, self.gh.writes[-1][5])
                self.gh.pr_snapshots[("org/repo", number)] = [make_pr(
                    number=number, head_sha="f" * 40, requested_users=(), reviews=(own,))]
                self.assertEqual(r.process_pr("org/repo", number), "applied")
                self.assertEqual(self.gh.writes[-1][3:5], ("f" * 40, "approve"))
                self.assertEqual(r.process_pr("org/repo", number), "final")
        self.assertEqual((reviewer.calls, len(self.gh.writes), self.notified), (4, 4, []))

    def test_dismissal_during_assessment_prevents_write(self):
        dismissed = Review("me", "DISMISSED", HEAD, marker_line("opus", "abcdef", HEAD))
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(), make_pr(reviews=(dismissed,))]
        reviewer = FakeReviewer([APPROVE])
        r = self.runner(reviewer=reviewer)
        self.assertEqual(r.tick(), {"defer": 1})
        self.assertEqual(r.tick(), {"ignore": 1})
        self.assertEqual((reviewer.calls, self.gh.writes, len(self.notified)), (1, [], 1))

    def test_dismissal_notification_requires_own_automated_review_at_head(self):
        auto = marker_line("opus", "abcdef", HEAD)
        cases = (Review("me", "DISMISSED", HEAD, "manual review"),
                 Review("bob", "DISMISSED", HEAD, auto),
                 Review("me", "DISMISSED", "0" * 40, auto))
        r = self.runner(reviewer=FakeReviewer([NOTHING, NOTHING]))
        for number, review in enumerate(cases, start=7):
            self.gh.pr_snapshots[("org/repo", number)] = [make_pr(number=number, reviews=(review,))]
            r.process_pr("org/repo", number)
        self.assertEqual(self.notified, [])

    def test_superseded_block_does_not_warn(self):
        block = Review("me", "CHANGES_REQUESTED", "0" * 40, marker_line("opus", "abcdef", "0" * 40))
        approval = Review("me", "APPROVED", "a" * 40, marker_line("opus", "abcdef", "a" * 40))
        comment = Review("me", "COMMENTED", "a" * 40, approval.body)
        dismissed = dataclasses.replace(block, state="DISMISSED")
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(reviews=(block, approval, comment, dismissed))]
        self.assertEqual(self.runner(reviewer=FakeReviewer([COMMENT])).tick(), {"applied": 1})
        self.assertEqual(self.notified, [])

    def test_quarantine_is_per_item_including_kind(self):
        self.gh.pr_snapshots[("org/repo", 8)] = [make_pr(number=8)]
        fetch = self.gh.pull_request

        def fetch_or_fail(repo, number):
            if number == 7:
                raise GhError(["pr", "view"], 1, "HTTP 404: Not Found")
            return fetch(repo, number)

        self.gh.pull_request = Mock(side_effect=fetch_or_fail)
        self.gh.search_results = [("org/repo", 7), ("org/repo", 8)]
        self.gh.notifications = [("org/repo", 7, "updated")]
        self.gh.pending_deployments = Mock(return_value=[])
        r = self.runner(reviewer=FakeReviewer([APPROVE]))
        self.assertEqual(r.tick(), {"quarantined": 1, "applied": 1})
        self.assertEqual(r.tick(), {"final": 1})
        self.assertEqual(r.quarantined, {("pr", "org/repo", 7)})
        self.assertEqual(sum(c.args == ("org/repo", 7) for c in self.gh.pull_request.call_args_list), 1)
        self.assertEqual(self.gh.pending_deployments.call_count, 2)
        self.assertEqual(self.gh.writes[0][2], 8)

    def test_reconciliation_skips_removed_repositories(self):
        for kind, key in (("pr", ("org/private", 7, HEAD)), ("deployment", ("org/private", 7, 1, 555))):
            self.journal.append(kind=kind, key=key, event="unknown", mode="live", policy="old")
        self.gh.pull_request = Mock(side_effect=AssertionError("must not fetch"))
        self.gh.run_by_id = Mock(side_effect=AssertionError("must not fetch"))
        r = self.runner()
        r.refresh_settings(make_settings(repos=["org/repo"]))
        self.assertEqual(r.tick(), {})
        self.assertEqual(r.tick(), {})
        self.gh.pull_request.assert_not_called()
        self.gh.run_by_id.assert_not_called()
        self.assertEqual(len(self.journal.unknown_records()), 2)

    def test_bare_429_write_retries_without_settling(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        reviewer = FakeReviewer([APPROVE, APPROVE])
        r = self.runner(reviewer=reviewer)
        self.gh.fail_write = GhError(["api"], 1, "HTTP 429")
        self.assertEqual(r.tick(), {"error": 1})
        self.assertFalse(any(e in ("error", "unknown") for e, _ in self.events()))
        self.assertEqual(r.quarantined, set())
        self.gh.fail_write = None
        self.assertEqual(r.tick(), {"applied": 1})
        self.assertEqual((reviewer.calls, len(self.gh.writes)), (2, 1))

    def test_model_approve_writes_once_with_marker_and_sha(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        reviewer = FakeReviewer([APPROVE])
        r = self.runner(reviewer=reviewer)
        self.assertEqual(r.process_pr("org/repo", 7), "applied")
        self.assertEqual(r.process_pr("org/repo", 7), "final")
        self.assertEqual((reviewer.calls, len(self.gh.writes)), (1, 1))
        kind, repo, number, sha, action, body = self.gh.writes[0]
        self.assertEqual((kind, sha, action), ("pr", HEAD, "approve"))
        self.assertEqual(body, "🔭 Approved: the tests cover the change.\n\n"
                         "Automated review for @me (lightkeeper)\n\n"      # disclosure is on by default
                         + f"<!-- lightkeeper v2 head={HEAD[:7]} "
                         f"judgement={r.settings.judgement('org/repo').hash} engine=claude/opus -->")      # the private reason stays private
        self.assertEqual(self.notified, [])

    def test_model_nothing_is_settled_and_silent(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        reviewer = FakeReviewer([NOTHING])
        r = self.runner(reviewer=reviewer)
        self.assertEqual(r.process_pr("org/repo", 7), "nothing")
        self.assertEqual(r.process_pr("org/repo", 7), "final")
        self.assertEqual((reviewer.calls, self.gh.writes, self.notified), (1, [], []))

    def test_comment_is_posted_once_and_pr_stays_watched(self):
        failed = Check("sast", "completed", "failure", "https://github.com/org/repo/actions/runs/1/job/77")
        self.gh.logs[77] = "Error: secret pattern"
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(checks=(failed,))]
        reviewer = FakeReviewer([COMMENT, APPROVE])
        r = self.runner(reviewer=reviewer)
        self.assertEqual(r.process_pr("org/repo", 7), "applied")
        self.assertEqual(reviewer.last_failed_logs, [(failed, "Error: secret pattern")])
        kind, repo, number, sha, action, body = self.gh.writes[0]
        self.assertEqual(action, "comment")
        self.assertTrue(body.startswith("🔭 Not approving yet: CI failed in sast."))
        self.assertIn(f"\n\n<!-- {MARKER} v2 ", body)
        self.assertEqual(r.process_pr("org/repo", 7), "final")                   # same head, same checks: silent
        self.assertEqual(self.journal.watched_prs(), [("org/repo", 7)])
        # the author pushes a fix: new head, nobody re-requests the review, still evaluated
        own = Review("me", "COMMENTED", HEAD, body="CI failed\n\n" + marker_line("opus", "abcdef", HEAD))
        fixed = make_pr(head_sha="f" * 40, requested_users=(), reviews=(own,))
        self.gh.pr_snapshots[("org/repo", 7)] = [fixed]
        self.assertEqual(r.process_pr("org/repo", 7), "applied")
        self.assertEqual(self.gh.writes[1][3:5], ("f" * 40, "approve"))
        self.assertEqual(reviewer.calls, 2)

    def test_request_changes_notifies_and_is_superseded_on_next_head(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        r = self.runner(reviewer=FakeReviewer([BLOCK, APPROVE]))
        self.assertEqual(r.process_pr("org/repo", 7), "applied")
        self.assertEqual(self.gh.writes[0][4], "request_changes")
        self.assertEqual(self.notified, ["org/repo#7 drops a table"])
        blocked = make_pr(head_sha="f" * 40, requested_users=(),
                          reviews=(Review("me", "CHANGES_REQUESTED", HEAD, body="text\n\n" + marker_line("opus", "abcdef", HEAD)),))
        self.gh.pr_snapshots[("org/repo", 7)] = [blocked]
        self.assertEqual(r.process_pr("org/repo", 7), "applied")                 # automated block does not stop it
        self.assertEqual(self.gh.writes[1][4], "approve")

    def test_owner_manual_review_stops_the_tool(self):
        manual = make_pr(reviews=(Review("me", "CHANGES_REQUESTED", "0" * 40, body="no, rethink this"),))
        self.gh.pr_snapshots[("org/repo", 7)] = [manual]
        reviewer = FakeReviewer([APPROVE])
        self.assertEqual(self.runner(reviewer=reviewer).process_pr("org/repo", 7), "ignore")
        self.assertEqual((reviewer.calls, self.gh.writes), (0, []))

    def test_closed_pr_is_forgotten(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(checks=(Check("tests", "in_progress", ""),))]
        r = self.runner(reviewer=FakeReviewer([]))
        self.assertEqual(r.process_pr("org/repo", 7), "defer")
        self.assertEqual(self.journal.watched_prs(), [("org/repo", 7)])
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(state="MERGED")]
        self.assertEqual(r.process_pr("org/repo", 7), "closed")
        self.assertEqual(self.journal.watched_prs(), [])
        self.assertEqual(r.process_pr("org/repo", 7), "closed")
        self.assertEqual([e for e, _ in self.events()], ["seen", "defer", "closed"])     # closed is journaled once

    def test_tick_includes_watched_prs_not_in_search(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(checks=(Check("tests", "in_progress", ""),))]
        r = self.runner(reviewer=FakeReviewer([APPROVE]))
        self.assertEqual(r.tick(), {"defer": 1})
        own = Review("me", "COMMENTED", "0" * 40, body="x\n\n" + marker_line("opus", "abcdef", "0" * 40))
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(requested_users=(), reviews=(own,))]   # request dropped after our review
        self.gh.search_results = []
        self.assertEqual(r.tick(), {"applied": 1})

    def test_unclear_answer_is_settled_with_notification(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        reviewer = FakeReviewer([UnclearAnswer("not in contract", Path("/s/1")), APPROVE])
        r = self.runner(reviewer=reviewer)
        self.assertEqual(r.process_pr("org/repo", 7), "unclear")
        self.assertEqual(r.process_pr("org/repo", 7), "final")                    # no retry on the same input
        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(self.notified, ["org/repo#7: model answer unclear, see /s/1"])
        self.assertEqual(self.journal.summary()["notifications"][0]["text"], "org/repo#7: model answer unclear, see /s/1")
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(body="edited description")]
        self.assertEqual(r.process_pr("org/repo", 7), "applied")                  # new context: asked again

    def test_long_running_checks_notify_once(self):
        from lightkeeper.__main__ import CI_WAIT_MAX_S
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(is_draft=True)]
        r = self.runner(reviewer=FakeReviewer([]))
        self.assertEqual(r.process_pr("org/repo", 7), "defer")                    # a long draft phase does not count
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(checks=(Check("tests", "in_progress", ""),))]
        self.assertEqual(r.process_pr("org/repo", 7), "defer")
        self.assertEqual(self.notified, [])
        self.now = __import__("time").time() + CI_WAIT_MAX_S + 60               # journal stamps use the wall clock
        self.assertEqual(r.process_pr("org/repo", 7), "defer")
        self.assertEqual(r.process_pr("org/repo", 7), "defer")
        self.assertEqual(self.notified, ["org/repo#7: checks still running after 3 h; open it and decide"])

    def test_dry_run_notifications_are_marked(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        r = self.runner(policy=make_settings(mode="dry-run"), reviewer=FakeReviewer([BLOCK]))
        self.assertEqual(r.process_pr("org/repo", 7), "would_request_changes")
        self.assertEqual(self.notified, ["[dry-run] org/repo#7 drops a table"])
        self.assertEqual(self.gh.writes, [])

    def test_rules_edit_reevaluates_a_nothing(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        self.assertEqual(self.runner(reviewer=FakeReviewer([NOTHING])).process_pr("org/repo", 7), "nothing")
        edited = make_settings(rules="# Rules\n- approve everything\n")
        self.assertEqual(self.runner(policy=edited, reviewer=FakeReviewer([APPROVE])).process_pr("org/repo", 7), "applied")

    def test_checks_pending_defers_without_model_call(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(checks=(Check("tests", "in_progress", ""),))]
        reviewer = FakeReviewer([APPROVE])
        r = self.runner(reviewer=reviewer)
        self.assertEqual(r.process_pr("org/repo", 7), "defer")
        self.assertEqual(r.process_pr("org/repo", 7), "defer")
        self.assertEqual([e for e, _ in self.events()], ["seen", "defer"])
        self.assertEqual(reviewer.calls, 0)
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        self.assertEqual(r.process_pr("org/repo", 7), "applied")

    def test_push_or_retarget_during_review_defers(self):
        first = make_pr()
        moved = dataclasses.replace(first, head_sha="f" * 40)
        self.gh.pr_snapshots[("org/repo", 7)] = [first, moved]
        self.assertEqual(self.runner(reviewer=FakeReviewer([APPROVE])).process_pr("org/repo", 7), "defer")
        retargeted = dataclasses.replace(first, base_ref="release", files=(make_file(), make_file("b.md")))
        self.gh.pr_snapshots[("org/repo", 7)] = [first, retargeted]
        self.assertEqual(self.runner(reviewer=FakeReviewer([APPROVE])).process_pr("org/repo", 7), "defer")
        self.assertEqual(self.gh.writes, [])

    def test_manual_approval_and_crash_recovery_are_satisfied(self):
        first = make_pr()
        approved = dataclasses.replace(first, reviews=(Review("me", "APPROVED", HEAD),), requested_users=())
        self.gh.pr_snapshots[("org/repo", 7)] = [first, approved]
        self.assertEqual(self.runner(reviewer=FakeReviewer([APPROVE])).process_pr("org/repo", 7), "satisfied")   # approved by hand mid-flight
        self.gh.pr_snapshots[("org/repo", 8)] = [dataclasses.replace(approved, number=8)]
        self.assertEqual(self.runner(reviewer=FakeReviewer([])).process_pr("org/repo", 8), "satisfied")
        self.assertEqual(self.gh.writes, [])

    def test_reviewer_failure_pauses_then_resumes(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        reviewer = FakeReviewer([ReviewerError("bad json"), APPROVE])
        r = self.runner(reviewer=reviewer)
        self.assertEqual(r.process_pr("org/repo", 7), "defer")
        self.assertIn("bad json", self.notified[-1])                  # the owner learns why, not just that it paused
        self.now += REVIEWER_PAUSE_S - 1
        self.assertEqual(r.process_pr("org/repo", 7), "defer")
        self.assertEqual(reviewer.calls, 1)
        self.now += 2
        self.assertEqual(r.process_pr("org/repo", 7), "applied")

    def test_dry_run_then_live(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        dry = self.runner(policy=make_settings(mode="dry-run"), reviewer=FakeReviewer([APPROVE]))
        self.assertEqual(dry.process_pr("org/repo", 7), "would_approve")
        self.assertEqual(dry.process_pr("org/repo", 7), "final")
        self.assertEqual(self.gh.writes, [])
        live = self.runner(reviewer=FakeReviewer([APPROVE]))
        self.assertEqual(live.process_pr("org/repo", 7), "applied")

    def test_write_errors_and_unknown_reconciliation(self):
        pr = make_pr()
        self.gh.pr_snapshots[("org/repo", 7)] = [pr]
        r = self.runner(reviewer=FakeReviewer([APPROVE, APPROVE, APPROVE]))
        self.gh.fail_write = GhError(["api"], 1, "HTTP 422: stale")
        self.assertEqual(r.process_pr("org/repo", 7), "error")
        self.assertEqual(r.process_pr("org/repo", 7), "final")                    # a definite rejection is not retried
        self.assertEqual(len(self.notified), 1)
        self.gh.pr_snapshots[("org/repo", 7)] = [dataclasses.replace(pr, body="edited")]
        pr = self.gh.pr_snapshots[("org/repo", 7)][0]
        self.gh.fail_write = GhError(["api"], 1, "HTTP 502: Bad Gateway")
        self.assertEqual(r.process_pr("org/repo", 7), "unknown")                  # 5xx: GitHub may have accepted it
        self.gh.fail_write = None
        self.assertEqual(r.process_pr("org/repo", 7), "unknown")
        history = (Review("bot", "COMMENTED", HEAD, "status"),) * 149 + (Review("me", "APPROVED", HEAD),)
        self.gh.pr_snapshots[("org/repo", 7)] = [dataclasses.replace(pr, reviews=history, requested_users=())]
        self.assertEqual(r.reconcile_unknown(), 1)
        self.assertEqual(self.gh.writes, [])

    def test_item_error_alerts_but_does_not_back_off(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        self.gh.pull_request = lambda repo, number: (_ for _ in ()).throw(RuntimeError("broken item"))
        seen = []
        r = Runner(make_settings(), self.gh, self.journal, FakeReviewer([]), clock=lambda: self.now,
                   notifier=lambda t, x: None, observer=lambda event, **f: seen.append((event, f)), live_lock_held=True)
        for _ in range(3):
            self.assertEqual(r.tick(), {"error": 1})
        self.assertEqual(r._tick_failures, 0)                   # the loop's backoff stays at the normal interval
        alerts = [f["text"] for e, f in seen if e == "condition" and f["key"] == "tick_failed" and f["text"]]
        self.assertTrue(alerts and "3 ticks" in alerts[-1])

    def test_tick_isolates_failures_and_quarantines(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        self.gh.pr_snapshots[("other/repo", 1)] = [make_pr(repo="other/repo", number=1)]
        self.assertEqual(self.runner(reviewer=FakeReviewer([APPROVE])).tick(), {"applied": 1})   # other repo: skipped

        class FlakyGh(FakeGh):
            def pull_request(self, repo, number):
                if number == 1:
                    raise GhError(["pr", "view"], 1, "HTTP 502: Bad Gateway")
                if repo == "org/private":
                    raise GhError(["pr", "view"], 1, "HTTP 404: Not Found")
                return super().pull_request(repo, number)
        flaky = FlakyGh()
        flaky.pr_snapshots[("org/repo", 1)] = [make_pr(number=1)]
        flaky.pr_snapshots[("org/repo", 7)] = [make_pr()]
        flaky.pr_snapshots[("org/private", 2)] = [make_pr(repo="org/private", number=2)]
        r = Runner(make_settings(), flaky, Journal(Path(self.dir.name) / "j2.jsonl"), FakeReviewer([APPROVE]),
                   clock=lambda: self.now, live_lock_held=True)
        self.assertEqual(r.tick(), {"error": 1, "applied": 1, "quarantined": 1})
        self.assertIn(("pr", "org/private", 2), r.quarantined)

    def test_approve_floor_overrides_a_talked_into_model(self):
        failing = make_pr(checks=(Check("sast", "completed", "failure", "https://x/actions/runs/1/job/9"),))
        self.gh.pr_snapshots[("org/repo", 7)] = [failing]
        r = self.runner(reviewer=FakeReviewer([APPROVE]))
        self.assertEqual(r.process_pr("org/repo", 7), "nothing")
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(self.notified, ["org/repo#7: the model wanted to approve despite failed check: sast; open it and decide"])
        self.assertEqual(r.process_pr("org/repo", 7), "final")

    def test_missing_patch_floor_names_file_and_owner_action_once(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(files=(make_file('report.pdf', patch=None),))]
        r = self.runner(reviewer=FakeReviewer([APPROVE]))
        self.assertEqual(r.process_pr("org/repo", 7), "nothing")
        self.assertEqual(self.gh.writes, [])
        expected = ("org/repo#7: the model wanted to approve but report.pdf has no patch to read; "
                    "open it, and press A in its detail to approve as owner if it is fine")
        self.assertEqual(self.notified, [expected])
        records = [json.loads(line) for line in self.journal.path.read_text().splitlines()]
        self.assertEqual(next(rec['text'] for rec in records if rec['event'] == 'notify'), expected)
        self.assertIn(('nothing', 'approve refused by the floor: patch not visible: report.pdf'), self.events())
        self.assertEqual(r.process_pr("org/repo", 7), "final")
        self.assertEqual(self.notified, [expected])

    def test_already_posted_review_is_not_posted_again(self):
        # the tool posted a comment, then died before journaling it
        own = Review("me", "COMMENTED", HEAD, body="CI failed\n\n" + marker_line("opus", "abcdef", HEAD))
        history = (Review("bot", "COMMENTED", HEAD, "status"),) * 149 + (own,)
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(reviews=history)]
        r = self.runner(reviewer=FakeReviewer([COMMENT]))
        self.assertEqual(r.process_pr("org/repo", 7), "already")
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(r.process_pr("org/repo", 7), "final")

    def test_teammate_review_during_assessment_defers(self):
        first = make_pr()
        warned = dataclasses.replace(first, reviews=(Review("bob", "CHANGES_REQUESTED", HEAD, body="drops prod data"),))
        self.gh.pr_snapshots[("org/repo", 7)] = [first, warned]
        self.assertEqual(self.runner(reviewer=FakeReviewer([APPROVE])).process_pr("org/repo", 7), "defer")
        self.assertEqual(self.gh.writes, [])

    def test_removed_then_rerequested_resumes_and_reopen_keeps_watching(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(requested_users=("bob",))]
        r = self.runner(reviewer=FakeReviewer([NOTHING, APPROVE]))
        self.assertEqual(r.process_pr("org/repo", 7), "defer")
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]                        # re-requested
        self.assertEqual(r.process_pr("org/repo", 7), "nothing")
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(state="CLOSED")]
        self.assertEqual(r.process_pr("org/repo", 7), "closed")
        self.assertEqual(self.journal.watched_prs(), [])
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]                        # reopened, same context
        self.assertEqual(r.process_pr("org/repo", 7), "final")                     # same answer as before
        self.assertEqual(self.journal.watched_prs(), [("org/repo", 7)])            # but watched again
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(head_sha="f" * 40)]
        self.assertEqual(r.process_pr("org/repo", 7), "applied")

    def test_rate_limited_write_is_retried_not_settled(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        r = self.runner(reviewer=FakeReviewer([APPROVE, APPROVE]))
        self.gh.fail_write = GhError(["api"], 1, "HTTP 403: API rate limit exceeded")
        with self.assertRaises(GhError):
            r.process_pr("org/repo", 7)
        self.gh.fail_write = None
        self.assertEqual(r.process_pr("org/repo", 7), "applied")

    def test_closed_then_reopened_is_reviewed_again(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(state="CLOSED")]
        r = self.runner(reviewer=FakeReviewer([APPROVE]))
        self.journal.append(kind="pr", key=("org/repo", 7, HEAD), event="defer", mode="live", policy="p", reason="draft")
        self.assertEqual(r.process_pr("org/repo", 7), "closed")
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        self.assertEqual(r.process_pr("org/repo", 7), "applied")

    def test_active_automated_block_is_reported_when_new_head_is_not_approved(self):
        block = Review("me", "CHANGES_REQUESTED", "0" * 40, body="no\n\n" + marker_line("opus", "abcdef", "0" * 40))
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(reviews=(block,))]
        r = self.runner(reviewer=FakeReviewer([NOTHING, COMMENT]))
        self.assertEqual(r.process_pr("org/repo", 7), "nothing")
        self.assertEqual(len([n for n in self.notified if "still blocks" in n]), 1)   # told once, right away
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr(reviews=(block,), body="edited")]
        self.assertEqual(r.process_pr("org/repo", 7), "applied")
        self.assertEqual(len([n for n in self.notified if "still blocks" in n]), 1)

    def test_search_failure_still_runs_watched_prs(self):
        class NoSearch(FakeGh):
            def search_review_requests(self, login, repos):
                raise GhError(["search"], 1, "HTTP 502")
        gh = NoSearch()
        gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        self.journal.append(kind="pr", key=("org/repo", 7, HEAD), event="defer", mode="live", policy="p", reason="draft")
        r = Runner(make_settings(), gh, self.journal, FakeReviewer([APPROVE]), clock=lambda: self.now,
                   notifier=lambda t, x: None, live_lock_held=True)
        self.assertEqual(r.tick(), {"error": 1, "applied": 1})

    def test_unexpected_exception_does_not_stop_the_tick(self):
        class Broken(FakeGh):
            def pull_request(self, repo, number):
                if number == 1:
                    raise KeyError("headRefOid")
                return super().pull_request(repo, number)
        gh = Broken()
        gh.pr_snapshots[("org/repo", 1)] = [make_pr(number=1)]
        gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        r = Runner(make_settings(), gh, self.journal, FakeReviewer([APPROVE]), clock=lambda: self.now,
                   notifier=lambda t, x: None, live_lock_held=True)
        self.assertEqual(r.tick(), {"error": 1, "applied": 1})

    def test_rate_limit_is_not_quarantine(self):
        class Limited(FakeGh):
            def pull_request(self, repo, number):
                raise GhError(["pr"], 1, "HTTP 403: API rate limit exceeded")
        gh = Limited()
        gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        r = Runner(make_settings(), gh, self.journal, FakeReviewer([]), clock=lambda: self.now, notifier=lambda t, x: None, live_lock_held=True)
        self.assertEqual(r.tick(), {"error": 1})
        self.assertEqual(r.quarantined, set())


class DeploymentLifecycle(LifecycleBase):
    def arm(self, run=None, pending=(PENDING_PROD,), log=PLAN_LOG_OK, updated="2026-09-17T10:00:00Z"):
        run = run or make_run()
        self.gh.runs = [run]
        self.gh.notifications = [(run.repo, run.run_id, updated)]
        self.gh.pending[(run.repo, run.run_id)] = list(pending)
        self.gh.jobs_by_attempt[(run.repo, run.run_id, run.attempt)] = [PLAN_JOB, APPLY_JOB]
        self.gh.logs[PLAN_JOB.job_id] = log
        self.gh.files_at[(run.repo, f".github/workflows/{run.workflow_file}")] = "on: workflow_dispatch\n"
        return run

    def test_running_jobs_defer_without_model_call(self):
        from lightkeeper.models import Job
        run = self.arm()
        self.gh.jobs_by_attempt[(run.repo, run.run_id, 1)] = [Job(1, "plan", "in_progress", ""), APPLY_JOB]
        reviewer = FakeReviewer([APPROVE])
        r = self.runner(reviewer=reviewer)
        self.assertEqual(r.process_run(run.repo, run.run_id), "defer")
        self.assertEqual(reviewer.calls, 0)
        self.gh.jobs_by_attempt[(run.repo, run.run_id, 1)] = [PLAN_JOB, APPLY_JOB]
        self.assertEqual(r.process_run(run.repo, run.run_id), "applied")

    def test_ignore_is_reopened_when_jobs_change(self):
        from lightkeeper.models import Job
        run = self.arm()
        self.gh.jobs_by_attempt[(run.repo, run.run_id, 1)] = [PLAN_JOB, Job(2, "apply", "waiting", ""), Job(3, "lint", "completed", "failure")]
        reviewer = FakeReviewer([IGNORE, APPROVE])
        r = self.runner(reviewer=reviewer)
        self.assertEqual(r.process_run(run.repo, run.run_id), "ignore")
        self.assertEqual(r.process_run(run.repo, run.run_id), "final")
        self.gh.jobs_by_attempt[(run.repo, run.run_id, 1)] = [PLAN_JOB, Job(2, "apply", "waiting", ""), Job(3, "lint", "completed", "success")]
        self.assertEqual(r.process_run(run.repo, run.run_id), "applied")      # a re-run lint changed the context

    def test_transport_failure_on_write_is_unknown_and_reconciled_by_env_id(self):
        run = self.arm()
        r = self.runner(reviewer=FakeReviewer([APPROVE]))
        self.gh.fail_write = GhError(["api"], 1, "error connecting to api.github.com")   # no HTTP status
        self.assertEqual(r.process_run(run.repo, run.run_id), "unknown")
        self.gh.fail_write = None
        self.gh.approvals[(run.repo, run.run_id)] = [("me", "approved", (999,))]          # other environment
        self.assertEqual(r.reconcile_unknown(), 0)
        self.gh.approvals[(run.repo, run.run_id)] = [("me", "approved", (555,))]
        self.assertEqual(r.reconcile_unknown(), 1)

    def test_failed_run_is_not_remembered_as_seen(self):
        self.arm()

        class FlakyPending(FakeGh):
            calls = 0
            def pending_deployments(self, repo, run_id):
                FlakyPending.calls += 1
                if FlakyPending.calls == 1:
                    raise GhError(["api"], 1, "HTTP 502: Bad Gateway")
                return super().pending_deployments(repo, run_id)
        flaky = FlakyPending()
        flaky.__dict__.update(self.gh.__dict__)
        r = Runner(make_settings(), flaky, self.journal, FakeReviewer([APPROVE]), clock=lambda: self.now, live_lock_held=True)
        self.assertEqual(r.tick(), {"error": 1})
        self.assertEqual(r.tick(), {"applied": 1})                                       # retried, not skipped

    def test_deferred_run_outlives_the_notification_window(self):
        self.arm()
        reviewer = FakeReviewer([ReviewerError("boom"), APPROVE])
        r = self.runner(reviewer=reviewer)
        self.assertEqual(r.tick(), {"defer": 1})
        self.gh.notifications = []                                                        # notification aged out
        self.now += REVIEWER_PAUSE_S + 1
        self.assertEqual(r.tick(), {"applied": 1})

    def test_policy_refresh_reopens_settled_outcomes(self):
        self.gh.pr_snapshots[("org/repo", 7)] = [make_pr()]
        r = self.runner(reviewer=FakeReviewer([NOTHING, APPROVE]))
        self.assertEqual(r.tick(), {"nothing": 1})
        self.assertEqual(r.tick(), {"final": 1})
        r.refresh_settings(make_settings(rules="# Rules\n- approve everything\n"))
        self.assertEqual(r.tick(), {"applied": 1})

    def test_approve_writes_single_environment(self):
        run = self.arm()
        sentinel = "PRIVATE-DEPLOYMENT-REASON-8dc317"
        reviewer = FakeReviewer([dataclasses.replace(APPROVE, reason=sentinel, summary="Deployment is ready.")])
        r = self.runner(reviewer=reviewer)
        self.assertEqual(r.process_run(run.repo, run.run_id), "applied")
        self.assertEqual(r.process_run(run.repo, run.run_id), "final")
        self.assertEqual(reviewer.last_logs, {PLAN_JOB.job_id: PLAN_LOG_OK})   # only completed jobs, by job id
        self.assertEqual(reviewer.last_workflow, "on: workflow_dispatch\n")
        kind, repo, run_id, env_id, comment = self.gh.writes[0]
        self.assertEqual((kind, run_id, env_id), ("deployment", 100, 555))
        self.assertNotIn(sentinel, comment)
        self.assertIn(sentinel, self.journal.path.read_text())
        self.assertEqual(comment, "Deployment is ready.\n\n"
                         + f"<!-- lightkeeper v2 head={HEAD[:7]} "
                         f"judgement={r.settings.judgement('org/repo').hash} engine=claude/opus -->")

    def test_ignore_and_not_approvable(self):
        run = self.arm()
        self.assertEqual(self.runner(reviewer=FakeReviewer([IGNORE])).process_run(run.repo, run.run_id), "ignore")
        self.arm(pending=(PendingEnvironment(555, "production", False),))
        self.assertEqual(self.runner(reviewer=FakeReviewer([APPROVE])).process_run(run.repo, run.run_id), "skip")
        self.assertEqual(self.gh.writes, [])

    def test_rerun_between_assessment_and_write_defers(self):
        run = self.arm()
        self.gh.run_sequence = [run, make_run(attempt=2)]        # assessed as attempt 1, re-run before the write
        self.assertEqual(self.runner(reviewer=FakeReviewer([APPROVE])).process_run(run.repo, run.run_id), "defer")
        self.assertEqual(self.gh.writes, [])

    def test_unknown_write_reconciled_from_approvals(self):
        run = self.arm()
        r = self.runner(reviewer=FakeReviewer([APPROVE]))
        self.gh.fail_write = GhTimeout(["api"], 60)
        self.assertEqual(r.process_run(run.repo, run.run_id), "unknown")
        self.gh.fail_write = None
        self.gh.pending[(run.repo, run.run_id)] = []
        self.gh.approvals[(run.repo, run.run_id)] = [("me", "approved", (555,))]
        self.assertEqual(r.tick(), {"reconciled": 1})
        self.assertEqual(self.gh.writes, [])

    def test_tick_refreshes_pending_gates_without_reassessing(self):
        run = self.arm()
        reviewer = FakeReviewer([APPROVE, APPROVE])
        r = self.runner(reviewer=reviewer)
        self.assertEqual(r.tick(), {"applied": 1})
        self.assertEqual(r.tick(), {"final": 1})                            # pending gates stay visible until gone
        self.gh.notifications = [(run.repo, run.run_id, "2026-09-17T11:00:00Z")]
        self.assertEqual(r.tick(), {"final": 1})                            # re-examined, already decided
        self.assertEqual(reviewer.calls, 1)

    def test_deferred_run_is_re_examined_without_a_new_notification(self):
        self.arm()
        reviewer = FakeReviewer([ReviewerError("boom"), APPROVE])
        r = self.runner(reviewer=reviewer)
        self.assertEqual(r.tick(), {"defer": 1})
        self.now += REVIEWER_PAUSE_S + 1
        self.assertEqual(r.tick(), {"applied": 1})


if __name__ == "__main__":
    unittest.main()
