"""policy.toml holds the few knobs that are not rules; RULES.md holds everything about what to approve.
The code-owned eligibility checks (design §4) live here as pure functions."""
from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .models import Decision, PullRequest, is_automated

SEMANTICS_VERSION = "5"     # part of policy_hash: bump when eligibility or prompt semantics change
POLL_INTERVAL_S = 60
MODES = ("dry-run", "live")
_TOP = {"mode", "login", "repos", "model", "effort", "claude_config_dir", "rules"}


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    mode: str
    login: str
    repos: tuple[str, ...]          # "owner/name", explicit; nothing outside this list is ever touched
    model: str
    effort: str
    claude_config_dir: str | None
    rules_path: Path
    rules: str                      # RULES.md text, verbatim
    policy_hash: str


def _str(raw: dict, key: str, default=None, required: bool = False) -> str | None:
    value = raw.get(key, default)
    if required and (not isinstance(value, str) or not value.strip()):
        raise PolicyError(f"{key}: required string")
    if value is not None and not isinstance(value, str):
        raise PolicyError(f"{key}: expected a string")
    return value


def parse_policy(raw: dict, base_dir: Path, rules_text: str) -> Policy:
    if not isinstance(raw, dict):
        raise PolicyError("policy: expected a table")
    unknown = sorted(set(raw) - _TOP)
    if unknown:
        raise PolicyError(f"policy: unknown field(s) {', '.join(unknown)}")
    mode = _str(raw, "mode", "dry-run")
    if mode not in MODES:
        raise PolicyError(f"mode: expected one of {MODES}")
    repos = raw.get("repos")
    if (not isinstance(repos, list) or not repos
            or not all(isinstance(r, str) and r.count("/") == 1 and all(r.split("/")) for r in repos)):
        raise PolicyError('repos: expected a non-empty list of "owner/name" strings')
    if not rules_text.strip():
        raise PolicyError("rules file is empty")
    rules_path = base_dir / _str(raw, "rules", "RULES.md")
    profile = _str(raw, "claude_config_dir")
    if profile:
        profile_path = Path(profile).expanduser()
        profile = str((base_dir / profile_path).resolve())
    digest = hashlib.sha256(json.dumps([SEMANTICS_VERSION, raw, rules_text], sort_keys=True).encode()).hexdigest()[:6]
    return Policy(mode=mode, login=_str(raw, "login", required=True), repos=tuple(repos),
                  model=_str(raw, "model", "opus"), effort=_str(raw, "effort", "medium"),
                  claude_config_dir=profile,
                  rules_path=rules_path, rules=rules_text, policy_hash=digest)


def load_policy(path: str | Path) -> Policy:
    path = Path(path)
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise PolicyError(f"policy file not found: {path}")
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"policy file is not valid TOML: {exc}")
    rules_name = raw.get("rules", "RULES.md") if isinstance(raw, dict) else "RULES.md"
    rules_path = path.parent / str(rules_name)
    try:
        rules_text = rules_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PolicyError(f"rules file not found: {rules_path}")
    return parse_policy(raw, path.parent, rules_text)


def evaluate_pr(pr: PullRequest, policy) -> Decision:
    """Code-owned eligibility (design §4). Returns `review` when the model should look at the pull request.
    A PR stays watched from the first review request until it is closed, so being removed from the
    requested reviewers (which GitHub does after every submitted review) does not end the evaluation."""
    login = policy.login
    if pr.repo not in policy.repos:
        return Decision("ignore", "repository not in repos", "eligibility")
    if pr.state != "OPEN":
        return Decision("ignore", f"pull request is {pr.state.lower()}", "eligibility")
    if pr.author == login:
        return Decision("ignore", "own pull request", "eligibility")
    mine = [r for r in pr.reviews if r.author == login]
    if any(r.state == "APPROVED" and r.commit_sha == pr.head_sha for r in mine):
        return Decision("satisfied", "already approved at head", "observed")
    if any(r.state != "PENDING" and not is_automated(r.body) for r in mine):
        return Decision("ignore", "the owner reviewed this pull request themselves", "eligibility")
    if any(r.state == "PENDING" for r in mine):
        return Decision("ignore", "the owner has an unsubmitted review open", "eligibility")
    if any(r.state == "DISMISSED" and r.commit_sha == pr.head_sha and is_automated(r.body) for r in mine):
        return Decision("ignore", "automated review was dismissed on this head", "eligibility")
    if not mine and login not in pr.requested_users:
        return Decision("defer", "no longer requested", "readiness")     # removed before any review; a re-request resumes
    if pr.is_draft:
        return Decision("defer", "draft", "readiness")          # becomes eligible when marked ready
    pending = [c.name for c in pr.checks if c.status != "completed" and c.required]
    if pending:
        return Decision("defer", f"checks still running: {', '.join(pending[:3])}", "readiness")
    if not pr.files:
        return Decision("ignore", "no changed files", "eligibility")
    if pr.changed_files is not None and pr.changed_files != len(pr.files):
        reason = f"file list incomplete: {len(pr.files)} of {pr.changed_files}"
        return Decision("nothing", reason, "eligibility", notify=f"{pr.repo}#{pr.number}: {reason}; open it and decide")
    return Decision("review", "eligible; the model decides", "eligibility")


def approve_floor(pr: PullRequest) -> str | None:
    """The three objective rules that an `approve` must clear whatever the model was talked into. Returns the
    reason that blocks it, or None. The same rules are written in RULES.md for the model; this is the floor."""
    if pr.author_is_bot or pr.author.endswith("[bot]"):
        return "bot author"
    failed = [c.name for c in pr.checks if c.failed]
    if failed:
        return f"failed check: {failed[0]}"
    missing = [f.path for f in pr.files if not f.is_complete]
    if missing:
        return f"patch not visible: {missing[0]}"
    return None
