import tempfile
import unittest
from pathlib import Path

from lightkeeper.models import MARKER, Check, Review, marker_line
from lightkeeper.policy import PolicyError, approve_floor, evaluate_pr, load_policy, parse_policy
from tests.helpers import HEAD, RULES, make_file, make_policy, make_pr

MIN = {"login": "me", "repos": ["org/repo"]}


class ParsePolicyTests(unittest.TestCase):
    def test_defaults(self):
        p = parse_policy(MIN, Path("/x"), RULES)
        self.assertEqual((p.mode, p.model, p.effort, p.repos, p.claude_config_dir),
                         ("dry-run", "opus", "medium", ("org/repo",), None))
        self.assertEqual(p.rules_path, Path("/x/RULES.md"))
        self.assertEqual(p.rules, RULES)
        self.assertEqual(len(p.policy_hash), 6)

    def test_validation(self):
        for raw in ({}, {"repos": ["org/repo"]},                               # login required
                    {"login": "me"}, {"login": "me", "repos": []},             # repos required, explicit
                    {"login": "me", "repos": ["org"]}, {"login": "me", "repos": "org/repo"},
                    {**MIN, "orgs": ["org"]},                                  # v2 field, unknown now
                    {**MIN, "mode": "yolo"}):
            with self.assertRaises(PolicyError, msg=raw):
                parse_policy(raw, Path("/x"), RULES)
        with self.assertRaises(PolicyError):
            parse_policy(MIN, Path("/x"), "   \n")                             # empty rules

    def test_hash_covers_rules_and_policy(self):
        a = parse_policy(MIN, Path("/x"), RULES).policy_hash
        self.assertNotEqual(a, parse_policy(MIN, Path("/x"), RULES + "- more\n").policy_hash)
        self.assertNotEqual(a, parse_policy({**MIN, "model": "sonnet"}, Path("/x"), RULES).policy_hash)
        self.assertEqual(a, parse_policy(MIN, Path("/x"), RULES).policy_hash)

    def test_load_reads_rules_next_to_policy(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "policy.toml").write_text('login = "me"\nrepos = ["org/repo"]\nrules = "my-rules.md"\n')
            with self.assertRaises(PolicyError):
                load_policy(Path(d, "policy.toml"))                          # rules file missing
            Path(d, "my-rules.md").write_text("# r\n- x\n")
            p = load_policy(Path(d, "policy.toml"))
            self.assertEqual(p.rules, "# r\n- x\n")


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.policy = make_policy()

    def outcome(self, pr, policy=None):
        return evaluate_pr(pr, policy or self.policy).outcome

    def test_review_is_the_normal_outcome(self):
        d = evaluate_pr(make_pr(), self.policy)
        self.assertEqual((d.outcome, d.source), ("review", "eligibility"))
        self.assertEqual(self.outcome(make_pr(author="dependabot[bot]")), "review")     # bots: the rules decide
        self.assertEqual(self.outcome(make_pr(checks=(Check("tests", "completed", "failure"),))), "review")
        self.assertEqual(self.outcome(make_pr(checks=())), "review")                   # no checks: the rules decide

    def test_repo_must_be_listed(self):
        self.assertEqual(self.outcome(make_pr(repo="other/repo")), "ignore")

    def test_closed_draft_own(self):
        self.assertEqual(self.outcome(make_pr(state="MERGED")), "ignore")
        self.assertEqual(self.outcome(make_pr(is_draft=True)), "defer")            # re-evaluated when ready
        self.assertEqual(self.outcome(make_pr(author="me")), "ignore")

    def test_owner_reviews(self):
        auto = "text\n\n" + marker_line("opus", "abcdef", "0" * 40)
        self.assertEqual(self.outcome(make_pr(reviews=(Review("me", "APPROVED", HEAD),), requested_users=())), "satisfied")
        self.assertEqual(self.outcome(make_pr(reviews=(Review("me", "APPROVED", "0" * 40, body=auto),))), "review")
        self.assertEqual(self.outcome(make_pr(reviews=(Review("me", "CHANGES_REQUESTED", "0" * 40, body=auto),))),
                         "review")                                                  # automated block: new head re-evaluated
        self.assertEqual(self.outcome(make_pr(reviews=(Review("me", "COMMENTED", "0" * 40, body=auto),))), "review")
        self.assertEqual(self.outcome(make_pr(reviews=(Review("me", "APPROVED", "0" * 40, body="LGTM"),))), "ignore")
        self.assertEqual(self.outcome(make_pr(reviews=(Review("me", "CHANGES_REQUESTED", HEAD, body="no"),))), "ignore")
        self.assertEqual(self.outcome(make_pr(reviews=(Review("me", "PENDING", HEAD),))), "ignore")
        self.assertEqual(self.outcome(make_pr(reviews=(Review("me", "DISMISSED", HEAD, body=auto),))), "ignore")
        self.assertEqual(self.outcome(make_pr(reviews=(Review("me", "DISMISSED", "0" * 40, body="was fine"),))), "ignore")
        # a human merely mentioning the tool is still a human review
        mention = f"{MARKER} missed the data loss; do not merge"
        self.assertEqual(self.outcome(make_pr(reviews=(Review("me", "CHANGES_REQUESTED", "0" * 40, body=mention),))), "ignore")

    def test_watched_pr_is_reviewed_after_the_tool_reviewed_but_not_when_removed_before(self):
        auto = "ok\n\n" + marker_line("opus", "abcdef", "0" * 40)
        self.assertEqual(self.outcome(make_pr(requested_users=("bob",), reviews=(Review("me", "COMMENTED", "0" * 40, body=auto),))),
                         "review")                                                   # GitHub dropped the request after our review
        d = evaluate_pr(make_pr(requested_users=("bob",)), self.policy)
        self.assertEqual((d.outcome, d.reason), ("defer", "no longer requested"))   # removed before any review; re-request resumes
        self.assertEqual(self.outcome(make_pr(reviews=(Review("me", "COMMENTED", "0" * 40, body="hm?"),))), "ignore")  # any manual review = takeover

    def test_approve_floor(self):
        self.assertIsNone(approve_floor(make_pr()))
        self.assertEqual(approve_floor(make_pr(author="renovate[bot]")), "bot author")
        self.assertEqual(approve_floor(make_pr(author_is_bot=True)), "bot author")
        self.assertEqual(approve_floor(make_pr(checks=(Check("sast", "completed", "failure"),))), "failed check: sast")
        self.assertEqual(approve_floor(make_pr(checks=(Check("lint", "completed", "skipped"),))), None)
        self.assertEqual(approve_floor(make_pr(checks=(Check("ci/legacy", "completed", "error"),))), "failed check: ci/legacy")
        self.assertEqual(approve_floor(make_pr(files=(make_file("logo.png", 0, 0, status="added", patch=None),))),
                         "patch not visible: logo.png")

    def test_checks_must_be_finished(self):
        d = evaluate_pr(make_pr(checks=(Check("tests", "in_progress", ""),)), self.policy)
        self.assertEqual((d.outcome, d.source, d.reason), ("defer", "readiness", "checks still running: tests"))

    def test_file_list_must_match_github_count(self):
        self.assertEqual(self.outcome(make_pr(changed_files=1)), "review")
        d = evaluate_pr(make_pr(changed_files=2), self.policy)
        self.assertEqual((d.outcome, d.reason), ("nothing", "file list incomplete: 1 of 2"))
        self.assertIn("open it and decide", d.notify)

    def test_incomplete_patch_goes_to_the_model_marked(self):
        self.assertEqual(self.outcome(make_pr(files=())), "ignore")
        binary = make_file("logo.png", 0, 0, status="added", patch=None)
        self.assertEqual(self.outcome(make_pr(files=(binary,))), "review")
        rename = make_file("b.md", 0, 0, status="renamed", previous_path="a.md", patch=None)
        self.assertEqual(self.outcome(make_pr(files=(rename,))), "review")


if __name__ == "__main__":
    unittest.main()
