"""Factories and fakes shared by the tests. Nothing here touches the network."""
from __future__ import annotations

from pathlib import Path

from lightkeeper.models import ChangedFile, Check, Job, PendingEnvironment, PullRequest, WorkflowRun
from lightkeeper.policy import parse_policy

HEAD = "1234567890abcdef1234567890abcdef12345678"
RULES = "# Rules\n\n- Approve docs-only changes by humans.\n- Never approve bots.\n"


def make_file(path="docs/readme.md", additions=1, deletions=1, status="modified", previous_path=None,
              patch="@@ -1 +1 @@\n-old\n+new"):
    return ChangedFile(path, previous_path, status, additions, deletions, patch)


def make_pr(**overrides) -> PullRequest:
    base = dict(
        repo="org/repo", number=7, head_sha=HEAD, base_ref="main", author="alice",
        is_draft=False, state="OPEN", title="Fix docs", body="Fixes a typo.",
        requested_users=("me",), requested_teams=(), reviews=(),
        checks=(Check("tests", "completed", "success"),),
        files=(make_file(),),
    )
    base.update(overrides)
    return PullRequest(**base)


def make_policy(rules=RULES, **overrides):
    raw = {"mode": "live", "login": "me", "repos": ["org/repo", "org/private"], "model": "opus"}
    raw.update(overrides)
    return parse_policy(raw, Path("/tmp"), rules)


def make_run(**overrides) -> WorkflowRun:
    base = dict(repo="org/repo", run_id=100, attempt=1, workflow_file="infra-apply.yml",
                event="workflow_dispatch", head_branch="main", head_sha=HEAD, status="waiting")
    base.update(overrides)
    return WorkflowRun(**base)


PLAN_JOB = Job(1, "plan", "completed", "success")
APPLY_JOB = Job(2, "apply", "waiting", "")
PENDING_PROD = PendingEnvironment(555, "production", True)
PLAN_LOG_OK = "Terraform will perform the following actions:\n  + resource\nPlan: 3 to add, 1 to change, 0 to destroy.\n"
PLAN_LOG_DESTROY = "Terraform will perform the following actions:\n  - resource\nPlan: 0 to add, 0 to change, 2 to destroy.\n"


class FakeGh:
    """In-memory stand-in for github.Gh. `pr_snapshots[(repo, n)]` is a list consumed one per fetch;
    the last snapshot is returned forever. `writes` records every mutating call."""

    def __init__(self):
        self.pr_snapshots: dict[tuple[str, int], list[PullRequest]] = {}
        self.runs: list[WorkflowRun] = []
        self.notifications: list[tuple[str, int, str]] = []
        self.pending: dict[tuple[str, int], list[PendingEnvironment]] = {}
        self.jobs_by_attempt: dict[tuple[str, int, int], list[Job]] = {}
        self.logs: dict[int, str] = {}
        self.approvals: dict[tuple[str, int], list] = {}
        self.writes: list[tuple] = []
        self.run_sequence: list[WorkflowRun] = []
        self.files_at: dict[tuple[str, str], str] = {}
        self.fail_write: Exception | None = None
        self.saturated = False
        self.search_results: list | None = None             # None = every snapshot in a listed repo

    def login(self):
        return "me"

    def search_review_requests(self, login, repos):
        if self.search_results is not None:
            return list(self.search_results), self.saturated
        return sorted(k for k in self.pr_snapshots if k[0] in repos), self.saturated

    def approval_requests(self, since_iso):
        return list(self.notifications)

    def pull_request(self, repo, number):
        seq = self.pr_snapshots[(repo, number)]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def submit_review(self, pr, action, body):
        if self.fail_write:
            raise self.fail_write
        self.writes.append(("pr", pr.repo, pr.number, pr.head_sha, action, body))
        return 4242

    def failed_check_logs(self, pr):
        return [(c, self.logs.get(c.job_id, "")) for c in pr.checks if c.failed and c.job_id]

    def run_by_id(self, repo, run_id):
        if self.run_sequence:                                    # scripted answers, one per call
            return self.run_sequence.pop(0)
        return next(r for r in self.runs if r.repo == repo and r.run_id == run_id)

    def pending_deployments(self, repo, run_id):
        return self.pending.get((repo, run_id), [])

    def jobs(self, repo, run_id, attempt):
        return self.jobs_by_attempt.get((repo, run_id, attempt), [])

    def job_log(self, repo, job_id):
        return self.logs.get(job_id, "")

    def run_approvals(self, repo, run_id):
        return self.approvals.get((repo, run_id), [])

    def default_branch(self, repo):
        return "main"

    def file_at(self, repo, path, ref):
        return self.files_at.get((repo, path))

    def approve_deployment(self, repo, run_id, env_id, comment):
        if self.fail_write:
            raise self.fail_write
        self.writes.append(("deployment", repo, run_id, env_id, comment))
        return "[]"


class FakeReviewer:
    def __init__(self, decisions):
        self.decisions = list(decisions)   # Decision objects or Exceptions, consumed in order
        self.calls = 0
        self.last_prs = []
        self.last_logs = None

    def review_pr(self, pr, failed_logs=()):
        self.last_prs.append(pr)
        self.last_failed_logs = failed_logs
        return self._next()

    def review_deployment(self, run, environment, jobs, logs, default_branch="?", workflow_text=None):
        self.last_logs, self.last_workflow = logs, workflow_text
        return self._next()

    def _next(self):
        self.calls += 1
        item = self.decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# Keep independent on-disk settings for the runner's mandatory pre-write reload.
_settings_dirs = []


def make_settings(rules=RULES, **overrides):
    import tempfile
    from lightkeeper.settings import Settings, AgentSettings, CommentSettings, RepoSettings
    from lightkeeper.migrate import settings_toml
    directory = tempfile.TemporaryDirectory()
    _settings_dirs.append(directory)
    root = Path(directory.name)
    settings = Settings(overrides.get("login", "me"),
                        AgentSettings(model=overrides.get("model", "opus"), effort=overrides.get("effort", "medium"),
                                      profile=overrides.get("claude_config_dir")), CommentSettings(),
                        {r: RepoSettings(r, post=overrides.get("mode", "live"), deployments=True)
                         for r in overrides.get("repos", ["org/repo", "org/private"])},
                        rules, "", {}, root)
    (root / "settings.toml").write_text(settings_toml(settings))
    (root / "rules.md").write_text(rules)
    return settings


def isolate_cli(test, root):
    """Give CLI tests a private cwd, home and lightkeeper environment."""
    import os
    from contextlib import chdir
    from unittest.mock import patch
    root = Path(root).resolve()
    test.enterContext(chdir(root))
    test.enterContext(patch.dict(os.environ))
    for name in list(os.environ):
        if name.startswith('LIGHTKEEPER_'):
            del os.environ[name]
    os.environ['HOME'] = str(root)
    # Child Python processes must import this checkout even from the isolated cwd.
    os.environ['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
