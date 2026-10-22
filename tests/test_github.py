import json
import os
import subprocess
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

from lightkeeper.github import (PR_VIEW_FIELDS, Gh, GhError, GhTimeout, clean_job_log, parse_approval_requests, parse_jobs,
                                      parse_pending, parse_pull_request, parse_required_checks, parse_runs)

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


class ParseTests(unittest.TestCase):
    def test_latest_check_run_replaces_cancelled_run(self):
        from lightkeeper.github import PRStatus
        from lightkeeper.policy import approve_floor
        from lightkeeper.reviewer import build_pr_prompt

        old = dict(name="tests", status="COMPLETED", conclusion="CANCELLED",
                   startedAt="2026-10-20T10:00:00Z", completedAt="2026-10-20T12:00:00Z",
                   detailsUrl="https://github.com/org/repo/actions/runs/1/job/1")
        new = dict(old, conclusion="SUCCESS", startedAt="2026-10-20T11:00:00Z",
                   detailsUrl="https://github.com/org/repo/actions/runs/2/job/2")
        for rollup in ([old, new], [new, old]):
            for flags, declared in ((None, None), ({"tests": True}, []), ({"tests": False}, ["tests"])):
                with self.subTest(rollup=rollup, flags=flags):
                    view = dict(load("pr_view.json"), statusCheckRollup=rollup)
                    pr = parse_pull_request("org/repo", view, [], [], flags, declared)
                    self.assertIsNone(approve_floor(pr))
                    self.assertEqual(PRStatus.from_pr(pr).ci.total, 1)
                    self.assertEqual(pr.checks[0].details_url, new["detailsUrl"])
                    self.assertTrue(pr.checks[0].required)
                    gh = Gh()
                    gh.job_log = Mock()
                    self.assertEqual(gh.failed_check_logs(pr), [])
                    gh.job_log.assert_not_called()
                    context = build_pr_prompt(pr)
                    self.assertIn(new["detailsUrl"], context)
                    self.assertNotIn(old["detailsUrl"], context)

    def test_check_timestamp_fallbacks_and_status_contexts(self):
        for fields in (dict(name="tests", status="COMPLETED", conclusion="SUCCESS"),
                       dict(context="legacy", state="SUCCESS")):
            for dates in (({"completedAt": "2026-10-20T11:00:00Z"},
                           {"completedAt": "2026-10-20T10:00:00Z"}, "first"),
                          ({}, {}, "second"),
                          ({"startedAt": "2026-10-20T10:00:00Z"},
                           {"startedAt": "2026-10-20T10:00:00Z"}, "second")):
                first, second, expected = dates
                with self.subTest(fields=fields, dates=dates):
                    view = dict(load("pr_view.json"), statusCheckRollup=[
                        dict(fields, **first, targetUrl="first"), dict(fields, **second, targetUrl="second")])
                    pr = parse_pull_request("org/repo", view, [], [], {}, [])
                    self.assertEqual(len(pr.checks), 1)
                    self.assertEqual(pr.checks[0].details_url, expected)
                    self.assertFalse(pr.checks[0].required)

    def test_active_rerun_beats_completed_run_in_either_order(self):
        from lightkeeper.policy import approve_floor, evaluate_pr
        from tests.helpers import make_settings
        for status in ("QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED"):
            for conclusion in ("SUCCESS", "CANCELLED"):
                old = dict(name="tests", status="COMPLETED", conclusion=conclusion,
                           startedAt="2026-10-20T10:00:00Z")
                for missing in (None, "", "0001-01-01T00:00:00Z"):
                    active = dict(name="tests", status=status, startedAt=missing, completedAt=missing)
                    for runs in ([old, active], [active, old]):
                        with self.subTest(status=status, conclusion=conclusion, runs=runs):
                            view = dict(load("pr_view.json"), statusCheckRollup=runs)
                            pr = parse_pull_request("org/repo", view, [], [], {"tests": True}, [])
                            self.assertEqual(len(pr.checks), 1)
                            self.assertEqual(pr.checks[0].status, status.lower())
                            self.assertIsNone(approve_floor(pr))
                            self.assertEqual(evaluate_pr(pr, make_settings()).reason, "checks still running: tests")

    def test_same_named_jobs_in_different_workflows_keep_failure_and_logs(self):
        from lightkeeper.policy import approve_floor
        from lightkeeper.reviewer import build_pr_prompt

        failed = dict(name="build", workflowName="CI", status="COMPLETED", conclusion="FAILURE",
                      startedAt="2026-10-20T10:00:00Z",
                      detailsUrl="https://github.com/org/repo/actions/runs/1/job/1")
        success = dict(failed, workflowName="Release", conclusion="SUCCESS", startedAt="2026-10-20T10:01:00Z",
                       detailsUrl="https://github.com/org/repo/actions/runs/2/job/2")
        for runs in ([failed, success], [success, failed]):
            for flags, declared in ((None, None), ({"build": True}, []), ({"build": False}, ["build"])):
                with self.subTest(runs=runs, flags=flags):
                    view = dict(load("pr_view.json"), statusCheckRollup=runs)
                    pr = parse_pull_request("org/repo", view, [], [], flags, declared)
                    self.assertEqual(len(pr.checks), 2)
                    self.assertTrue(all(check.required for check in pr.checks))
                    self.assertIsNotNone(approve_floor(pr))
                    gh = Gh()
                    gh.job_log = Mock(return_value="failed build log")
                    failed_check = next(check for check in pr.checks if check.failed)
                    self.assertEqual(gh.failed_check_logs(pr), [(failed_check, "failed build log")])
                    gh.job_log.assert_called_once_with("org/repo", 1)
                    context = build_pr_prompt(pr)
                    self.assertIn(failed["detailsUrl"], context)
                    self.assertIn(success["detailsUrl"], context)

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

    def test_required_flags_match_both_context_types_and_missing_name_is_optional(self):
        view = load("pr_view.json")
        flags, declared = parse_required_checks(load("pr_required_checks.json"), view["headRefOid"])
        self.assertEqual(flags, {"pytest": True, "lint": False, "ci/legacy": True})
        self.assertEqual(declared, ["pytest", "ci/legacy"])
        view["statusCheckRollup"].append({"name": "unlisted", "status": "IN_PROGRESS"})
        pr = parse_pull_request("org/repo", view, [], [], flags, declared)
        self.assertEqual({c.name: c.required for c in pr.checks}, {**flags, "unlisted": False})

    def test_required_query_rejects_malformed_partial_and_raced_responses(self):
        good = load("pr_required_checks.json")
        malformed = [None, [], {}, {"data": None}, {"data": {"node": None}},
                     {**good, "errors": [{"message": "lookup failed"}]}]
        for change in ("missing flag", "null flag", "string flag", "unknown type", "missing name",
                       "missing contexts", "null nodes", "extra page", "missing page info", "wrong head"):
            response = load("pr_required_checks.json")
            commit = response["data"]["node"]["commits"]["nodes"][0]["commit"]
            contexts = commit["statusCheckRollup"]["contexts"]
            node = contexts["nodes"][0]
            if change == "missing flag":
                del node["isRequired"]
            elif change == "null flag":
                node["isRequired"] = None
            elif change == "string flag":
                node["isRequired"] = "false"
            elif change == "unknown type":
                node["__typename"] = "Other"
            elif change == "missing name":
                del node["name"]
            elif change == "missing contexts":
                del commit["statusCheckRollup"]["contexts"]
            elif change == "null nodes":
                contexts["nodes"] = None
            elif change == "extra page":
                contexts["pageInfo"]["hasNextPage"] = True
            elif change == "missing page info":
                del contexts["pageInfo"]
            else:
                commit["oid"] = "another head"
            malformed.append(response)
        for response in malformed:
            with self.subTest(response=response), self.assertRaises(GhError):
                parse_required_checks(response, load("pr_view.json")["headRefOid"])

    def test_required_query_accepts_no_checks_and_combines_duplicate_names(self):
        response = load("pr_required_checks.json")
        commit = response["data"]["node"]["commits"]["nodes"][0]["commit"]
        contexts = commit["statusCheckRollup"]["contexts"]
        contexts["nodes"].append({"__typename": "CheckRun", "name": "pytest", "isRequired": False})
        self.assertTrue(parse_required_checks(response, commit["oid"])[0]["pytest"])
        contexts["nodes"] = []
        self.assertEqual(parse_required_checks(response, commit["oid"]), ({}, ["pytest", "ci/legacy"]))
        commit["statusCheckRollup"] = None
        self.assertEqual(parse_required_checks(response, commit["oid"]), ({}, ["pytest", "ci/legacy"]))

    def test_required_declarations_accept_no_rule(self):
        for base in ({"refUpdateRule": None}, {}, None):
            response = load("pr_required_checks.json")
            response["data"]["node"]["baseRef"] = base
            with self.subTest(base=base):
                flags, declared = parse_required_checks(response, load("pr_view.json")["headRefOid"])
                self.assertEqual(declared, [])
                self.assertTrue(flags["pytest"])

    def test_required_declarations_reject_malformed_lists(self):
        for declared in (None, "pytest", [None], [""], [1]):
            response = load("pr_required_checks.json")
            response["data"]["node"]["baseRef"]["refUpdateRule"]["requiredStatusCheckContexts"] = declared
            with self.subTest(declared=declared), self.assertRaises(GhError):
                parse_required_checks(response, load("pr_view.json")["headRefOid"])

    def test_declared_context_overrides_optional_flag_and_missing_context_is_pending(self):
        from lightkeeper.models import Check
        view = load("pr_view.json")
        response = load("pr_required_checks.json")
        response["data"]["node"]["baseRef"]["refUpdateRule"]["requiredStatusCheckContexts"] = ["lint", "external", "external"]
        flags, declared = parse_required_checks(response, view["headRefOid"])
        pr = parse_pull_request("org/repo", view, [], [], flags, declared)
        self.assertEqual([c.name for c in pr.checks], ["pytest", "lint", "ci/legacy", "external"])
        self.assertTrue(all(c.required for c in pr.checks))
        self.assertTrue(all(c.reported for c in pr.checks[:-1]))
        self.assertEqual(pr.checks[-1], Check("external", "pending", "", "", required=True, reported=False))

    def test_declared_contexts_wait_with_null_rollup(self):
        view = load("pr_view.json")
        view["statusCheckRollup"] = None
        response = load("pr_required_checks.json")
        response["data"]["node"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
        flags, declared = parse_required_checks(response, view["headRefOid"])
        pr = parse_pull_request("org/repo", view, [], [], flags, declared)
        self.assertEqual([c.name for c in pr.checks], declared)
        self.assertTrue(all(c.required and not c.reported and c.status == "pending" for c in pr.checks))

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
                                   (view, load("pr_required_checks.json"), [load("pr_files.json")], [others[:100], others[100:] + [owner]])])
        pr = Gh(runner=runner).pull_request("org/repo", 42)
        self.assertEqual(runner.call_count, 4)
        self.assertIn("id", PR_VIEW_FIELDS.split(","))
        call = runner.call_args_list[1]
        self.assertEqual(call.args[0], ["gh", "api", "graphql", "--input", "-"])
        payload = json.loads(call.kwargs["input"])
        self.assertEqual(payload["variables"], {"id": "PR_42"})
        self.assertIn("node(id:$id)", payload["query"])
        self.assertIn("baseRef { refUpdateRule { requiredStatusCheckContexts } }", payload["query"])
        self.assertEqual(payload["query"].count("isRequired(pullRequestId:$id)"), 2)
        self.assertEqual([c.required for c in pr.checks], [True, False, True])
        self.assertEqual(len(pr.reviews), 150)
        self.assertEqual(pr.reviews[149].author, "me")
        self.assertEqual(evaluate_pr(pr, make_policy()).outcome, "satisfied")
        self.assertNotIn("reviews", PR_VIEW_FIELDS.split(","))
        self.assertEqual(runner.call_args_list[-1].args[0],
                         ["gh", "api", "repos/org/repo/pulls/42/reviews?per_page=100", "--paginate", "--slurp"])

    def test_required_lookup_failures_fall_back_to_all_required(self):
        extra_page = load("pr_required_checks.json")
        extra_page["data"]["node"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["pageInfo"]["hasNextPage"] = True
        raced = load("pr_required_checks.json")
        raced["data"]["node"]["commits"]["nodes"][0]["commit"]["oid"] = "new head"
        errors = {"errors": [{"message": "lookup failed"}]}
        for result in (subprocess.CompletedProcess([], 1, "", "HTTP 502"),
                       subprocess.TimeoutExpired(["gh", "api", "graphql"], 60),
                       subprocess.CompletedProcess([], 0, "not json", ""),
                       *(subprocess.CompletedProcess([], 0, json.dumps(data), "")
                         for data in ({}, errors, extra_page, raced))):
            runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, json.dumps(load("pr_view.json")), ""),
                                       result, subprocess.CompletedProcess([], 0, "[[]]", ""),
                                       subprocess.CompletedProcess([], 0, "[[]]", "")])
            with self.subTest(result=result), self.assertLogs("lightkeeper", "WARNING") as logs:
                pr = Gh(runner=runner).pull_request("org/repo", 42)
            self.assertTrue(all(c.required for c in pr.checks))
            self.assertIsNone(pr.declared_required_checks)
            self.assertEqual(len(logs.output), 1)
            self.assertIn("org/repo#42", logs.output[0])
            self.assertIn(pr.head_sha, logs.output[0])
            self.assertIn("waiting for every check", logs.output[0])
            self.assertEqual(runner.call_count, 4)

    def test_rate_limited_lookup_raises_so_the_tick_backs_off(self):
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, json.dumps(load("pr_view.json")), ""),
                                   subprocess.CompletedProcess([], 1, "", "GraphQL: API rate limit exceeded")])
        with self.assertRaises(GhError) as caught:
            Gh(runner=runner).pull_request("org/repo", 42)
        self.assertTrue(caught.exception.rate_limited)
        self.assertEqual(runner.call_count, 2)

    def test_missing_pr_view_id_falls_back_without_graphql(self):
        for value in (None, "", " "):
            view = load("pr_view.json")
            if value is None:
                del view["id"]
            else:
                view["id"] = value
            gh = Gh(runner=Mock(side_effect=AssertionError("no subprocesses")))
            gh.json = Mock(side_effect=[view, [[]], [[]]])
            with self.subTest(value=value), self.assertLogs("lightkeeper", "WARNING") as logs:
                pr = gh.pull_request("org/repo", 42)
            self.assertTrue(all(c.required for c in pr.checks))
            self.assertEqual(gh.json.call_count, 3)
            self.assertEqual(len(logs.output), 1)
            self.assertIn("pr view: missing pull request node ID", logs.output[0])
            self.assertNotIn("api graphql", logs.output[0])

    def test_required_lookup_warning_once_per_repo_number_and_head(self):
        gh = Gh(runner=Mock(side_effect=AssertionError("no subprocesses")))
        calls = [("org/repo", 42, "a"), ("org/repo", 42, "a"), ("org/repo", 42, "b"),
                 ("org/repo", 43, "a"), ("other/repo", 42, "a"), ("org/repo", 42, "a")]
        with self.assertLogs("lightkeeper", "WARNING") as logs:
            for repo, number, head in calls:
                view = {**load("pr_view.json"), "headRefOid": head}
                gh.json = Mock(side_effect=[view, GhError(["api", "graphql"], 1, "lookup\nfailed"), [[]], [[]]])
                gh.pull_request(repo, number)
        self.assertEqual(len(logs.output), 4)
        self.assertTrue(all("\n" not in line for line in logs.output))

    def test_throttling_classification(self):
        for message, limited, no_access in (
                ("HTTP 429", True, False),
                ("GraphQL: API rate limit exceeded", True, False),
                ("GraphQL: API RATE LIMIT exceeded", True, False),
                ("GraphQL: other error", False, False),
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
