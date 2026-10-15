import json
import os
import subprocess
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from lightkeeper.github import (PR_VIEW_FIELDS, Gh, GhError, GhTimeout, clean_job_log, parse_approval_requests, parse_jobs,
                                      parse_pending, parse_pull_request, parse_runs)

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


class ParseTests(unittest.TestCase):
    def test_parse_pull_request(self):
        pr = parse_pull_request("org/repo", load("pr_view.json"), load("pr_files.json"), load("pr_reviews.json"))
        self.assertEqual((pr.repo, pr.number, pr.author, pr.state, pr.is_draft), ("org/repo", 42, "alice", "OPEN", False))
        self.assertEqual(pr.requested_users, ("me",))
        self.assertEqual(pr.requested_teams, ("org/reviewers", "org/already-qualified"))
        self.assertEqual(pr.reviews[0].author, "bob")
        self.assertEqual(pr.reviews[0].commit_sha, pr.head_sha)
        self.assertEqual((pr.reviews[0].state, pr.reviews[0].body), ("COMMENTED", "looks fine"))
        self.assertEqual((pr.reviews[1].author, pr.reviews[1].commit_sha, pr.reviews[1].body), ("", "", ""))
        checks = {c.name: c for c in pr.checks}
        self.assertEqual((checks["pytest"].status, checks["pytest"].conclusion), ("completed", "success"))
        self.assertEqual((checks["lint"].status, checks["lint"].conclusion), ("in_progress", ""))
        self.assertEqual((checks["ci/legacy"].status, checks["ci/legacy"].conclusion), ("completed", "success"))
        files = {f.path: f for f in pr.files}
        self.assertEqual(files["docs/wiki/new.md"].previous_path, "docs/wiki/old.md")
        self.assertTrue(files["docs/wiki/new.md"].is_complete)
        self.assertFalse(files["docs/logo.png"].is_complete)

    def test_parse_runs_uses_workflow_basename(self):
        runs = parse_runs("org/repo", [{"id": 1, "run_attempt": 2, "path": ".github/workflows/infra-apply.yml",
                                        "event": "workflow_dispatch", "head_branch": "main", "head_sha": "abc",
                                        "status": "waiting"}])
        self.assertEqual((runs[0].workflow_file, runs[0].attempt, runs[0].status), ("infra-apply.yml", 2, "waiting"))

    def test_parse_jobs_and_pending(self):
        jobs = parse_jobs([{"id": 5, "name": "plan", "status": "completed", "conclusion": "success"}])
        self.assertEqual(jobs[0].job_id, 5)
        pending = parse_pending([{"environment": {"id": 17, "name": "production"},
                                  "wait_timer": 0, "current_user_can_approve": True, "reviewers": []}])
        self.assertEqual((pending[0].env_id, pending[0].name, pending[0].can_approve), (17, "production", True))

    def test_clean_job_log_strips_prefixes(self):
        raw = "plan\tRun terraform plan\t2026-10-10T12:00:00.1234567Z \x1b[36;1mPlan: 3 to add, 1 to change, 0 to destroy.\x1b[0m\n" \
              "2026-10-10T12:00:01.0000000Z No changes.\nplain line\n"
        self.assertEqual(clean_job_log(raw).splitlines(),
                         ["Plan: 3 to add, 1 to change, 0 to destroy.", "No changes.", "plain line"])


class FakeRunner:
    def __init__(self, stdout="", returncode=0, stderr="", raise_timeout=False):
        self.calls = []
        self.stdout, self.returncode, self.stderr, self.raise_timeout = stdout, returncode, stderr, raise_timeout

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


class GhTests(unittest.TestCase):
    def test_all_subprocesses_override_host_including_release_lookup(self):
        from tempfile import TemporaryDirectory
        from lightkeeper.updates import UpdateChecker
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, '{"tag_name":"v1.0.0"}', ''))
        gh = Gh(runner=runner)
        with patch.dict(os.environ, GH_HOST='enterprise.example'), TemporaryDirectory() as directory:
            gh.run(['api', 'user'])
            UpdateChecker(directory, gh).check()
            self.assertEqual(os.environ['GH_HOST'], 'enterprise.example')
        for call in runner.call_args_list:
            self.assertEqual(call.kwargs['env']['GH_HOST'], 'github.com')
        self.assertEqual(runner.call_args.kwargs['timeout'], 10)

    def test_pull_request_fetches_every_review_page(self):
        from lightkeeper.policy import evaluate_pr
        from tests.helpers import make_policy
        view = load("pr_view.json")
        owner = {"user": {"login": "me"}, "state": "APPROVED", "commit_id": view["headRefOid"], "body": "LGTM"}
        others = [load("pr_reviews.json")[0]] * 149
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, json.dumps(data), "") for data in
                                   (view, [load("pr_files.json")], [others[:100], others[100:] + [owner]])])
        pr = Gh(runner=runner).pull_request("org/repo", 42)
        self.assertEqual(len(pr.reviews), 150)
        self.assertEqual(pr.reviews[149].author, "me")
        self.assertEqual(evaluate_pr(pr, make_policy()).outcome, "satisfied")
        self.assertNotIn("reviews", PR_VIEW_FIELDS.split(","))
        self.assertEqual(runner.call_args_list[-1].args[0],
                         ["gh", "api", "repos/org/repo/pulls/42/reviews?per_page=100", "--paginate", "--slurp"])

    def test_throttling_classification(self):
        for message, limited, no_access in (
                ("HTTP 429", True, False),
                ("HTTP 403: API rate limit exceeded", True, False),
                ("HTTP 403: Secondary throttling", True, False),
                ("HTTP 403: Forbidden", False, True),
                ("HTTP 404: Not Found", False, True)):
            with self.subTest(message=message):
                error = GhError(["api"], 1, message)
                self.assertEqual((error.rate_limited, error.no_access), (limited, no_access))

    def test_submit_review_posts_event_commit_id_and_body(self):
        pr = parse_pull_request("org/repo", load("pr_view.json"), load("pr_files.json"), load("pr_reviews.json"))
        for action, event in (("approve", "APPROVE"), ("request_changes", "REQUEST_CHANGES"), ("comment", "COMMENT")):
            runner = FakeRunner(stdout='{"id": 99}')
            review_id = Gh(runner=runner).submit_review(pr, action, "lightkeeper: rule")
            self.assertEqual(review_id, 99)
            args, kwargs = runner.calls[0]
            self.assertEqual(args[:4], ["gh", "api", "-X", "POST"])
            self.assertIn("repos/org/repo/pulls/42/reviews", args)
            self.assertEqual(args[-2:], ["--input", "-"])
            self.assertNotIn("lightkeeper: rule", " ".join(args))
            self.assertNotIn("stdin", kwargs)
            self.assertEqual(json.loads(kwargs["input"]),
                             {"commit_id": pr.head_sha, "event": event, "body": "lightkeeper: rule"})

    def test_failed_check_logs_tail_by_job_link(self):
        from lightkeeper.models import Check
        pr = parse_pull_request("org/repo", load("pr_view.json"), load("pr_files.json"), load("pr_reviews.json"))
        failed = Check("sast", "completed", "failure", "https://github.com/org/repo/actions/runs/1/job/77")
        pr = __import__("dataclasses").replace(pr, checks=(failed, Check("ok", "completed", "success", "https://x/runs/1/job/78")))
        runner = FakeRunner(stdout="\n".join(f"line {i}" for i in range(200)))
        logs = Gh(runner=runner).failed_check_logs(pr)
        self.assertEqual([c.name for c, _ in logs], ["sast"])
        self.assertIn("repos/org/repo/actions/jobs/77/logs", runner.calls[0][0])
        self.assertTrue(logs[0][1].startswith("(… 80 earlier lines omitted)"))
        self.assertTrue(logs[0][1].endswith("line 199"))

    def test_approve_deployment_sends_single_environment(self):
        runner = FakeRunner(stdout="[]")
        Gh(runner=runner).approve_deployment("org/repo", 100, 555, "lightkeeper: gate")
        args = runner.calls[0][0]
        self.assertIn("repos/org/repo/actions/runs/100/pending_deployments", args)
        self.assertEqual(args[:4], ["gh", "api", "-X", "POST"])
        self.assertEqual(args[-2:], ["--input", "-"])
        self.assertNotIn("lightkeeper: gate", " ".join(args))
        self.assertNotIn("stdin", runner.calls[0][1])
        self.assertEqual(json.loads(runner.calls[0][1]["input"]),
                         {"environment_ids": [555], "state": "approved", "comment": "lightkeeper: gate"})

    def test_job_log_uses_rest_endpoint(self):
        runner = FakeRunner(stdout="2026-10-10T12:00:00.1234567Z Plan: 1 to add, 0 to change, 0 to destroy.\n")
        text = Gh(runner=runner).job_log("org/repo", 5)
        self.assertEqual(text, "Plan: 1 to add, 0 to change, 0 to destroy.")
        self.assertIn("repos/org/repo/actions/jobs/5/logs", runner.calls[0][0])
        self.assertNotIn("run", runner.calls[0][0][:3])

    def test_login_timeout_and_malformed_output(self):
        runner = FakeRunner(stdout='{"login": "me"}')
        self.assertEqual(Gh(runner=runner).login(timeout_s=15), "me")
        self.assertEqual(runner.calls[0][1]["timeout"], 15)
        for output in ('{', '{}', '[]', '{"login": null}', '{"login": 42}', '{"login": " "}'):
            with self.subTest(output=output), self.assertRaises((ValueError, KeyError, TypeError)):
                Gh(runner=FakeRunner(stdout=output)).login(timeout_s=15)

    def test_nonzero_exit_raises_gh_error(self):
        with self.assertRaises(GhError) as ctx:
            Gh(runner=FakeRunner(returncode=1, stderr="HTTP 404: Not Found")).login()
        self.assertIn("404", ctx.exception.stderr)
        self.assertEqual((ctx.exception.status, ctx.exception.no_access, ctx.exception.definite), (404, True, True))

    def test_error_classification(self):
        limit = GhError(["api"], 1, "HTTP 403: API rate limit exceeded for user")
        self.assertEqual((limit.no_access, limit.definite), (False, True))     # back off, do not quarantine
        gateway = GhError(["api"], 1, "HTTP 502: Bad Gateway")
        self.assertEqual((gateway.status, gateway.definite), (502, False))     # GitHub may have accepted the write
        lost = GhError(["api"], 1, "error connecting to api.github.com")
        self.assertEqual((lost.status, lost.definite, lost.no_access), (None, False, False))

    def test_timeout_raises_gh_timeout(self):
        with self.assertRaises(GhTimeout):
            Gh(runner=FakeRunner(raise_timeout=True)).login()

    def test_search_reports_saturation(self):
        page = json.dumps([{"repository": {"nameWithOwner": "org/repo"}, "number": i} for i in range(100)])
        found, saturated = Gh(runner=FakeRunner(stdout=page)).search_review_requests("me", ("org/repo",))
        self.assertEqual(len(found), 100)
        self.assertTrue(saturated)

    def test_search_is_one_query_scoped_to_repos(self):
        runner = FakeRunner(stdout="[]")
        Gh(runner=runner).search_review_requests("me", ("org/a", "org/b"))
        self.assertEqual(len(runner.calls), 1)
        args = runner.calls[0][0]
        self.assertEqual(args[args.index("--repo") + 1], "org/a")
        self.assertEqual(args.count("--repo"), 2)
        self.assertIn("user-review-requested:me", args)              # direct requests only

    def test_parse_approval_requests(self):
        items = [
            {"reason": "approval_requested", "updated_at": "2026-10-10T12:00:00Z",
             "subject": {"type": "WorkflowRun", "url": "https://api.github.com/repos/org/repo/actions/runs/100"},
             "repository": {"full_name": "org/repo"}},
            {"reason": "ci_activity", "subject": {"type": "CheckSuite", "url": None}, "repository": {"full_name": "org/repo"}},
            {"reason": "review_requested", "subject": {"type": "PullRequest", "url": "https://api.github.com/repos/org/repo/pulls/1"},
             "repository": {"full_name": "org/repo"}},
        ]
        self.assertEqual(parse_approval_requests(items), [("org/repo", 100, "2026-10-10T12:00:00Z")])


if __name__ == "__main__":
    unittest.main()
