"""Everything that talks to GitHub, through the gh CLI. The parse_* helpers are pure and fixture-tested."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass

from .models import CICounts, ChangedFile, Check, Comment, Job, PendingEnvironment, PullRequest, Review, WorkflowRun
from .policy import approve_floor

PR_VIEW_FIELDS = ("id,number,state,isDraft,headRefOid,baseRefName,author,title,body,reviewRequests,"
                  "statusCheckRollup,changedFiles,comments")
REVIEW_EVENTS = {"approve": "APPROVE", "request_changes": "REQUEST_CHANGES", "comment": "COMMENT"}
LOG_TAIL_LINES = 120
SEARCH_LIMIT = 100
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z (.*)$")
_STATUS_DONE = {"success", "failure", "error"}
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
STATUS_BATCH_SIZE = 50
_logger = logging.getLogger("lightkeeper")
_STATUS_FIELDS = """state headRefOid isDraft updatedAt author { login } title
  commits(last:1) { nodes { commit { statusCheckRollup { contexts(first:100) {
    pageInfo { hasNextPage }
    nodes {
      __typename
      ... on CheckRun { name status conclusion startedAt completedAt checkSuite { workflowRun { workflow { name } } } }
      ... on StatusContext { context state createdAt }
    }
  } } } } }"""

_REQUIRED_CHECKS_QUERY = """query($id:ID!) {
  node(id:$id) { ... on PullRequest {
    baseRef { refUpdateRule { requiredStatusCheckContexts } }
    commits(last:1) { nodes { commit { oid statusCheckRollup { contexts(first:100) {
      pageInfo { hasNextPage }
      nodes {
        __typename
        ... on CheckRun { name isRequired(pullRequestId:$id) }
        ... on StatusContext { context isRequired(pullRequestId:$id) }
      }
    } } } } }
  } }
}"""


@dataclass(frozen=True)
class PRStatus:
    repo: str
    number: int
    state: str
    head: str
    is_draft: bool
    author: str
    title: str
    counts: tuple[tuple[str, int], ...]
    ci: CICounts
    updated_at: str = ""
    failed_checks: tuple[str, ...] | None = None  # populated by the full fetch only
    approve_refusal: str = ""                   # full-fetch floor for the owner's confirmation

    base_ref: str = ""
    requested_reviewers: tuple[str, ...] | None = None
    head_pushed_at: str = ""  # Only an actual push timestamp; never PR updated_at.
    failed_check_links: tuple[tuple[str, str], ...] = ()
    optional_checks: tuple[str, ...] = ()

    @property
    def wake_key(self):
        """What is worth a full check now. Progress of running checks is shown but does not wake the loop:
        required checks gate review; the minute tick still runs. A full fetch costs ~4 calls per watched PR."""
        return self.state, self.head, self.is_draft, self.ci.running == 0, self.ci.failed

    @classmethod
    def from_pr(cls, pr: PullRequest):
        # Only checks GitHub reported count, so the light poll (which sees the rollup alone) agrees on wake_key.
        reported = [c for c in pr.checks if c.reported]
        counts = tuple(sorted(Counter(c.conclusion if c.status == "completed" else c.status
                                      for c in reported).items()))
        return cls(pr.repo, pr.number, pr.state, pr.head_sha, pr.is_draft, pr.author, pr.title,
                   counts, CICounts.from_states(counts, len(reported)),
                   failed_checks=tuple(c.name for c in pr.checks if c.failed),
                   approve_refusal=approve_floor(pr) or "", base_ref=pr.base_ref,
                   requested_reviewers=(*pr.requested_users, *pr.requested_teams),
                   failed_check_links=tuple((c.name, c.details_url) for c in pr.checks if c.failed),
                   optional_checks=tuple(c.name for c in pr.checks if not c.required))


def parse_pr_statuses(response: dict, prs: list[tuple[str, int]]) -> list[PRStatus]:
    """Aliases are positional; missing PRs and truncated contexts are left for the full tick."""
    data = response["data"]
    if not isinstance(data, dict):
        raise ValueError("missing GraphQL data")
    for error in response.get("errors") or []:
        path = error.get("path") or []
        alias = path[0] if path else ""
        node = data.get(alias)
        if (error.get("type") not in ("NOT_FOUND", "FORBIDDEN") or alias not in data
                or (node is not None and node.get("pullRequest") is not None)):
            raise ValueError("GraphQL status query failed")
    result = []
    for i, (repo, number) in enumerate(prs):
        pr = (data.get(f"pr{i}") or {}).get("pullRequest")
        if pr is None:
            continue
        nodes = pr["commits"]["nodes"]
        rollup = nodes[0]["commit"]["statusCheckRollup"] if nodes else None
        contexts = rollup["contexts"] if rollup else None
        if contexts and contexts["pageInfo"]["hasNextPage"] is not False:
            continue
        runs = latest_runs(contexts["nodes"] if contexts else [])
        states = Counter()
        for c in runs:
            if "state" in c and "status" not in c:
                state = (c.get("state") or "").lower()
                if state not in _STATUS_DONE:
                    state = "pending"  # same normalization as parse_pull_request
            else:
                state = (c.get("status") or "").lower()
                if state == "completed":
                    state = (c.get("conclusion") or "").lower()
            states[state] += 1
        counts = tuple(sorted(states.items()))
        ci = CICounts.from_states(counts, len(runs))
        result.append(PRStatus(repo, number, pr["state"], pr["headRefOid"], pr["isDraft"],
                               (pr["author"] or {}).get("login", ""), pr["title"], counts, ci, pr["updatedAt"]))
    return result


class GhError(RuntimeError):
    """gh exited non-zero. GitHub answers 404 for missing permissions as well as missing objects."""

    def __init__(self, args, returncode, stderr, stdout=""):
        self.command = ["gh", *args]
        self.returncode = returncode
        self.stderr = stderr or ""
        self.stdout = stdout
        super().__init__(f"{' '.join(self.command)} -> exit {returncode}: {self.stderr.strip()[:300]}")

    @property
    def status(self) -> int | None:
        m = re.search(r"HTTP (\d{3})", self.stderr)
        return int(m.group(1)) if m else None

    @property
    def rate_limited(self) -> bool:
        return self.status == 429 or (self.status == 403 and
                                     any(s in self.stderr.lower() for s in ("rate limit", "secondary"))) or (
                                         self.status is None and "rate limit" in self.stderr.lower())

    @property
    def no_access(self) -> bool:
        return self.status in (403, 404) and not self.rate_limited

    @property
    def definite(self) -> bool:
        """GitHub rejected the request (4xx). A 5xx may come from a gateway after GitHub accepted the write."""
        return self.status is not None and 400 <= self.status < 500


class GhTimeout(RuntimeError):
    def __init__(self, args, timeout_s):
        self.command = ["gh", *args]
        super().__init__(f"{' '.join(self.command)} timed out after {timeout_s}s")


def parse_required_checks(response: dict, head_sha: str) -> tuple[dict[str, bool], list[str]]:
    """Match names conservatively; incomplete or raced responses must never permit an early review."""
    try:
        if response.get("errors"):
            raise ValueError("GraphQL errors")
        pr = response["data"]["node"]
        base = pr["baseRef"]
        rule = base.get("refUpdateRule") if base is not None else None
        declared = rule["requiredStatusCheckContexts"] if rule is not None else []
        if not isinstance(declared, list) or any(not isinstance(name, str) or not name for name in declared):
            raise ValueError("invalid declared required-check contexts")
        commits = pr["commits"]["nodes"]
        if not isinstance(commits, list) or len(commits) != 1:
            raise ValueError("missing head commit")
        commit = commits[0]["commit"]
        if commit["oid"] != head_sha:
            raise ValueError("head changed during required-check lookup")
        rollup = commit["statusCheckRollup"]
        if rollup is None:
            return {}, declared
        contexts = rollup["contexts"]
        # Do not silently classify checks beyond the first page as optional.
        if contexts["pageInfo"]["hasNextPage"] is not False:
            raise ValueError("required-check contexts exceed one page or page info is malformed")
        nodes = contexts["nodes"]
        if not isinstance(nodes, list):
            raise ValueError("missing check contexts")
        required = {}
        for node in nodes:
            field = {"CheckRun": "name", "StatusContext": "context"}[node["__typename"]]
            name, flag = node[field], node["isRequired"]
            if not isinstance(name, str) or not name or not isinstance(flag, bool):
                raise ValueError("invalid required-check name or flag")
            required[name] = required.get(name, False) or flag
        return required, declared
    except (AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
        raise GhError(["api", "graphql"], 1, f"invalid required-check response: {exc}") from exc


def latest_runs(contexts: list[dict]) -> list[dict]:
    """Keep one run per workflow/check pair or status context."""
    latest = {}
    for c in contexts:
        name = c.get("name") or c.get("context") or ""
        if "state" in c and "status" not in c:
            key = name
            active = (c.get("state") or "").lower() not in _STATUS_DONE
        else:
            suite = c.get("checkSuite") or {}
            workflow = (suite.get("workflowRun") or {}).get("workflow") or {}
            key = (c.get("workflowName") or workflow.get("name") or "", name)
            active = (c.get("status") or "").lower() in {
                "queued", "in_progress", "pending", "waiting", "requested"}
        stamp = next((c[field] for field in ("startedAt", "completedAt", "createdAt")
                      if c.get(field) and c[field] != "0001-01-01T00:00:00Z"), "")
        # Active reruns beat completed runs in either order; within each group latest date wins,
        # with list order breaking ties or missing dates (including undated completed runs).
        previous = latest.get(key)
        if (previous is None or active > previous[0] or
                (active == previous[0] and (not stamp or not previous[1] or stamp >= previous[1]))):
            latest[key] = (active, stamp, c)
    return [c for _, _, c in latest.values()]


def parse_pull_request(repo: str, view: dict, files: list[dict], reviews: list[dict],
                       required_checks: dict[str, bool] | None = None,
                       declared_checks: list[str] | None = None) -> PullRequest:
    owner = repo.split("/")[0]
    users, teams = [], []
    for rr in view.get("reviewRequests") or []:
        if rr.get("slug"):
            slug = rr["slug"]
            teams.append(slug if "/" in slug else f"{owner}/{slug}")
        elif rr.get("login"):
            users.append(rr["login"])
    parsed_reviews = tuple(
        Review(author=(r.get("user") or {}).get("login", ""), state=r.get("state", ""),
               commit_sha=r.get("commit_id") or "", body=r.get("body") or "",
               review_id=r.get("id"), submitted_at=r.get("submitted_at") or "")
        for r in reviews
    )
    checks = []
    for c in latest_runs(view.get("statusCheckRollup") or []):
        name = c.get("name") or c.get("context") or ""
        url = c.get("detailsUrl") or c.get("targetUrl") or ""
        required = required_checks is None or required_checks.get(name, False) or name in (declared_checks or ())
        if "state" in c and "status" not in c:            # StatusContext (commit status)
            state = (c.get("state") or "").lower()
            done = state in _STATUS_DONE
            checks.append(Check(name, "completed" if done else "pending", state if done else "", url, required))
        else:                                              # CheckRun
            checks.append(Check(name, (c.get("status") or "").lower(), (c.get("conclusion") or "").lower(),
                                url, required))
    reported = {c.name for c in checks}
    for name in dict.fromkeys(declared_checks or ()):
        if name not in reported:
            checks.append(Check(name, "pending", "", "", required=True, reported=False))
    comments = tuple(Comment((c.get("author") or {}).get("login", ""), c.get("body") or "")
                     for c in view.get("comments") or [])
    changed = tuple(
        ChangedFile(path=f["filename"], previous_path=f.get("previous_filename"), status=f.get("status", ""),
                    additions=int(f.get("additions", 0)), deletions=int(f.get("deletions", 0)), patch=f.get("patch"))
        for f in files
    )
    return PullRequest(
        repo=repo, number=int(view["number"]), head_sha=view["headRefOid"], base_ref=view.get("baseRefName", ""),
        author=(view.get("author") or {}).get("login", ""), is_draft=bool(view.get("isDraft")),
        state=view.get("state", ""), title=view.get("title", ""), body=view.get("body") or "",
        requested_users=tuple(users), requested_teams=tuple(teams), reviews=parsed_reviews, checks=tuple(checks),
        files=changed, author_is_bot=bool((view.get("author") or {}).get("is_bot")),
        changed_files=view.get("changedFiles"), comments=comments,
        declared_required_checks=tuple(declared_checks) if declared_checks is not None else None,
    )


def parse_runs(repo: str, items: list[dict]) -> list[WorkflowRun]:
    return [
        WorkflowRun(repo=repo, run_id=int(i["id"]), attempt=int(i.get("run_attempt") or 1),
                    workflow_file=(i.get("path") or "").rsplit("/", 1)[-1], event=i.get("event", ""),
                    head_branch=i.get("head_branch", ""), head_sha=i.get("head_sha", ""), status=i.get("status", ""))
        for i in items
    ]


def parse_jobs(items: list[dict]) -> list[Job]:
    return [Job(int(j["id"]), j.get("name", ""), (j.get("status") or "").lower(), (j.get("conclusion") or "").lower())
            for j in items]


def parse_pending(items: list[dict]) -> list[PendingEnvironment]:
    return [PendingEnvironment(int(p["environment"]["id"]), p["environment"]["name"],
                               bool(p.get("current_user_can_approve"))) for p in items]


def parse_approval_requests(items: list[dict]) -> list[tuple[str, int, str]]:
    out = []
    for n in items:
        subject = n.get("subject") or {}
        if n.get("reason") != "approval_requested" or subject.get("type") != "WorkflowRun":
            continue
        url = subject.get("url") or ""
        if not url.rsplit("/", 1)[-1].isdigit():
            continue
        out.append(((n.get("repository") or {}).get("full_name", ""), int(url.rsplit("/", 1)[-1]), n.get("updated_at", "")))
    return out


def clean_job_log(text: str) -> str:
    """`gh run view --log` prefixes each line with job, step and an ISO timestamp; keep only the message."""
    out = []
    for line in _ANSI.sub("", text).splitlines():
        m = _TIMESTAMP.search(line)
        out.append(m.group(1) if m else line)
    return "\n".join(out)


class Gh:
    def __init__(self, runner=subprocess.run, timeout_s: int = 60):
        self._runner = runner
        self.timeout_s = timeout_s
        self._required_check_warnings: set[tuple[str, int, str]] = set()
        self._status_warnings: set[tuple[str, int, str]] = set()

    def run(self, args: list[str], *, timeout_s: int | None = None, errors: str = "strict", input: str | None = None) -> str:
        timeout = timeout_s or self.timeout_s
        # subprocess's encoding controls pipes; argv uses Python's filesystem codec.
        command = [os.fsdecode(arg.encode("utf-8")) for arg in ["gh", *args]]
        try:
            proc = self._runner(command, **({"stdin": subprocess.DEVNULL} if input is None else {"input": input}),
                                capture_output=True, text=True,
                                encoding="utf-8", errors=errors, timeout=timeout, check=False,
                                env={**os.environ, "GH_HOST": "github.com"})
        except subprocess.TimeoutExpired:
            raise GhTimeout(args, timeout)
        except OSError as exc:
            detail = ("gh CLI not found on PATH; install GitHub CLI and run gh auth login"
                      if isinstance(exc, FileNotFoundError) else f"gh CLI could not start: {exc}")
            raise GhError(args, 127, detail) from exc
        if proc.returncode != 0:
            raise GhError(args, proc.returncode, proc.stderr, proc.stdout)
        return proc.stdout

    def json(self, args: list[str], *, timeout_s: int | None = None, input: str | None = None):
        return json.loads(self.run(args, timeout_s=timeout_s, input=input))

    # --- reads
    def login(self, *, timeout_s: int | None = None) -> str:
        login = self.json(["api", "user"], timeout_s=timeout_s)["login"]
        if not isinstance(login, str) or not login.strip():
            raise ValueError("malformed GitHub login")
        return login

    def search_review_requests(self, login: str, repos: tuple[str, ...]) -> tuple[list[tuple[str, int]], bool]:
        """Open PRs in `repos` where `login` is directly requested as a reviewer right now (team requests are
        not included: `review-requested:` would match every team the owner belongs to)."""
        found: dict[tuple[str, int], None] = {}
        repo_flags = [flag for repo in repos for flag in ("--repo", repo)]
        items = self.json(["search", "prs", f"user-review-requested:{login}", *repo_flags, "--state", "open",
                           "--limit", str(SEARCH_LIMIT), "--json", "repository,number"])
        for item in items:
            found[(item["repository"]["nameWithOwner"], int(item["number"]))] = None
        return sorted(found), len(items) >= SEARCH_LIMIT

    def approval_requests(self, since_iso: str) -> list[tuple[str, int, str]]:
        """(repo, run_id, updated_at) for every notification asking for a deployment approval since `since_iso`.
        Notifications are only the wake-up signal; pending_deployments is the truth."""
        pages = self.json(["api", f"notifications?all=true&since={since_iso}&per_page=100", "--paginate", "--slurp"])
        return parse_approval_requests([n for page in pages for n in page])

    def pull_request(self, repo: str, number: int) -> PullRequest:
        view = self.json(["pr", "view", str(number), "-R", repo, "--json", PR_VIEW_FIELDS])
        required_checks, declared_checks = None, None
        try:
            pr_id = view.get("id")
            if not isinstance(pr_id, str) or not pr_id.strip():
                raise ValueError("pr view: missing pull request node ID")
            response = self.json(["api", "graphql", "--input", "-"],
                                 input=json.dumps({"query": _REQUIRED_CHECKS_QUERY, "variables": {"id": pr_id}}))
            required_checks, declared_checks = parse_required_checks(response, view["headRefOid"])
        except (GhError, GhTimeout, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, GhError) and exc.rate_limited:
                raise                                   # the tick backs off as for every other call
            key = repo, number, view["headRefOid"]
            if key not in self._required_check_warnings:
                self._required_check_warnings.add(key)
                _logger.warning("%s#%s head=%s: required-check lookup failed; waiting for every check: %s",
                                *key, " ".join(str(exc).split()))
        pages = self.json(["api", f"repos/{repo}/pulls/{number}/files?per_page=100", "--paginate", "--slurp"])
        files = [f for page in pages for f in page]
        pages = self.json(["api", f"repos/{repo}/pulls/{number}/reviews?per_page=100", "--paginate", "--slurp"])
        reviews = [r for page in pages for r in page]
        return parse_pull_request(repo, view, files, reviews, required_checks, declared_checks)

    def pr_statuses(self, prs: list[tuple[str, int]], *, timeout_s: float = 15) -> list[PRStatus]:
        result = []
        deadline = time.monotonic() + timeout_s
        for start in range(0, len(prs), STATUS_BATCH_SIZE):
            batch = prs[start:start + STATUS_BATCH_SIZE]
            declarations, fields, variables = [], [], []
            for i, (repo, number) in enumerate(batch):
                owner, name = repo.split("/", 1)
                declarations.extend((f"$owner{i}:String!", f"$name{i}:String!", f"$number{i}:Int!"))
                fields.append(f"pr{i}:repository(owner:$owner{i},name:$name{i}) {{ "
                              f"pullRequest(number:$number{i}) {{ {_STATUS_FIELDS} }} }}")
                variables.extend(("-f", f"owner{i}={owner}", "-f", f"name{i}={name}", "-F", f"number{i}={number}"))
            query = "query(" + ",".join(declarations) + "){" + " ".join(fields) + "}"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GhTimeout(["api", "graphql"], timeout_s)
            try:
                response = self.json(["api", "graphql", "-f", f"query={query}", *variables], timeout_s=remaining)
            except GhError as exc:
                # gh exits nonzero even for partial data with a missing/inaccessible PR.
                try:
                    response = json.loads(exc.stdout) if exc.stdout else None
                except ValueError:
                    raise exc
                if not response or not response.get("errors") or exc.rate_limited:
                    raise
            statuses = parse_pr_statuses(response, batch)
            known = {(status.repo, status.number) for status in statuses}
            for i, (repo, number) in enumerate(batch):
                pr = (response["data"].get(f"pr{i}") or {}).get("pullRequest")
                if pr is not None and (repo, number) not in known:
                    key = repo, number, pr["headRefOid"]
                    if key not in self._status_warnings:
                        self._status_warnings.add(key)
                        _logger.warning("%s#%s head=%s: light-poll check contexts incomplete; skipping", *key)
            result.extend(statuses)
        return result

    def run_by_id(self, repo: str, run_id: int) -> WorkflowRun:
        return parse_runs(repo, [self.json(["api", f"repos/{repo}/actions/runs/{run_id}"])])[0]

    def jobs(self, repo: str, run_id: int, attempt: int) -> list[Job]:
        pages = self.json(["api", f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
                           "--paginate", "--slurp"])
        return parse_jobs([j for page in pages for j in page.get("jobs") or []])

    def job_log(self, repo: str, job_id: int) -> str:
        # The REST endpoint serves a finished job's log while the run is still waiting on a gate;
        # `gh run view --log` refuses until the whole run has completed.
        return clean_job_log(self.run(["api", "--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job_id}/logs"],
                                      timeout_s=120, errors="replace"))

    def failed_check_logs(self, pr: PullRequest, limit: int = 5) -> list[tuple[Check, str]]:
        """The tail of the log of every failed check that is an Actions job (at most `limit`), for CI triage.
        A log that cannot be read is reported as such rather than dropped."""
        out = []
        for check in [c for c in pr.checks if c.failed and c.job_id][:limit]:
            try:
                text = self.job_log(pr.repo, check.job_id)
            except (GhError, GhTimeout) as exc:
                text = f"(log not available: {exc})"
            lines = text.splitlines()
            tail = lines[-LOG_TAIL_LINES:]
            prefix = f"(… {len(lines) - len(tail)} earlier lines omitted)\n" if len(lines) > len(tail) else ""
            out.append((check, prefix + "\n".join(tail)))
        return out

    def run_approvals(self, repo: str, run_id: int) -> list[tuple[str, str, tuple[int, ...]]]:
        """(login, state, environment ids) for every deployment review recorded on the run."""
        items = self.json(["api", f"repos/{repo}/actions/runs/{run_id}/approvals"])
        return [((a.get("user") or {}).get("login", ""), a.get("state", ""),
                 tuple(int(e.get("id", 0)) for e in a.get("environments") or [])) for a in items]

    def default_branch(self, repo: str) -> str:
        return self.json(["api", f"repos/{repo}"])["default_branch"]

    def file_at(self, repo: str, path: str, ref: str) -> str | None:
        """Text of a repository file at a commit, or None when it does not exist there."""
        try:
            return self.run(["api", f"repos/{repo}/contents/{path}?ref={ref}", "-H", "Accept: application/vnd.github.raw"])
        except GhError as exc:
            if "HTTP 404" in exc.stderr:
                return None
            raise

    def pending_deployments(self, repo: str, run_id: int) -> list[PendingEnvironment]:
        return parse_pending(self.json(["api", f"repos/{repo}/actions/runs/{run_id}/pending_deployments"]))

    # --- writes
    def submit_review(self, pr: PullRequest, action: str, body: str) -> int:
        """Post one review (approve / request_changes / comment) bound to the assessed head SHA."""
        data = self.json(["api", "-X", "POST", f"repos/{pr.repo}/pulls/{pr.number}/reviews",
                          "--input", "-"],
                         input=json.dumps({"event": REVIEW_EVENTS[action], "commit_id": pr.head_sha, "body": body}))
        return int(data["id"])

    def approve_deployment(self, repo: str, run_id: int, env_id: int, comment: str) -> str:
        return self.run(["api", "-X", "POST", f"repos/{repo}/actions/runs/{run_id}/pending_deployments",
                  "--input", "-"],
                        input=json.dumps({"environment_ids": [env_id], "state": "approved", "comment": comment}))
