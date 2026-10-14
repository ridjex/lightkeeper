"""Append-only JSONL journal: the tool's memory and its human-readable ledger, plus the instance lock.

Events, per key (PR: repo, number, head sha; deployment: repo, run, attempt, environment):
  assessment / applied / would_<action> / nothing / ignore / defer / satisfied / unknown / unclear / error /
  closed / notify / withheld / reopen / owner_attempt / owner_error.
  `owner_attempt` is unknown until an outcome; `owner_error` clears it without settling the head.
  `owner_approve` and owner-origin `satisfied` also persist owner takeover across later PR heads.
  `reopen` clears contextual settlement and cached assessment.
  `withheld` records a proposed action and reason but stays deferred, not final.
  `owner_approve`, `applied` with action approve and `satisfied` are final forever; `nothing`, `ignore`,
  `would_*`, `unclear`, `error` and `applied` comment/request_changes are settled only for the same policy
  hash, mode and context fingerprint (a push, a check rerun, an edited description or edited rules re-open
  them). `closed` only stops watching the pull request."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from pathlib import Path

FOREVER_EVENTS = ("satisfied", "owner_approve")
SETTLED_EVENTS = ("nothing", "ignore", "unclear", "error", "would_approve", "would_request_changes", "would_comment")
NOTIFICATIONS_KEPT = 20
RECORD_VERSION = 2


class JournalError(RuntimeError):
    pass


def key_str(key) -> str:
    return json.dumps(list(key), separators=(",", ":"))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Journal:
    def __init__(self, path: str | Path, observer=None):
        self.path = Path(path)
        self.observer = observer
        self._observations: dict[tuple, tuple] = {}
        self._pending: set[tuple] = set()
        self._outcomes: dict[str, str] = {}
        self._forever: dict[str, str] = {}                 # key -> event
        self._settled: dict[str, tuple] = {}               # key -> (event, policy, mode, fingerprint)
        self._protected: set[str] = set()
        self._defer: dict[str, str] = {}
        self._defer_since: dict[str, str] = {}
        self._defer_kind: dict[str, str] = {}
        self._unknown: dict[str, dict] = {}
        self._owner_takeovers: set[tuple] = set()
        self._watched: dict[str, list] = {}                # "repo#n" -> [repo, number], open PRs ever seen
        self._notified: dict[str, set[str]] = {}
        self.notifications: list[dict] = []
        self._rehashes: dict[tuple[str, str], str] = {}     # (legacy hash, repo) -> judgement
        self._superseded: set[str] = set()                   # settled keys with a later defer/unknown/closed
        self._assessments: dict[str, dict] = {}
        self._retry_assessments: set[str] = set()
        self.components: dict[str, dict[str, str]] = {}
        self.rehashes: dict[str, dict] = {}
        self.records = 0
        self.last_at: str | None = None

    def replay(self, read_only: bool = False) -> int:
        """Rebuild state from disk. Read-only status never repairs an interrupted append."""
        if not self.path.exists():
            return 0
        raw = self.path.read_bytes()
        good_bytes = 0
        records = []
        lines = raw.split(b"\n")
        for i, line in enumerate(lines):
            tail = i == len(lines) - 1
            if tail and not line:
                break
            try:
                rec = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if tail:
                    break  # Repair only after checking every complete record's version.
                raise JournalError(f"{self.path}: corrupt record at line {i + 1}") from exc
            try:
                version = rec.get("v", 1)
                if version > RECORD_VERSION:
                    raise JournalError(f"journal written by a newer lightkeeper (record version {version}); upgrade")
            except (TypeError, AttributeError) as exc:
                raise JournalError(f"{self.path}: corrupt record at line {i + 1}") from exc
            records.append(rec)
            good_bytes += len(line) + (0 if tail else 1)
        for i, rec in enumerate(records):
            try:
                self._apply(rec)
            except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
                raise JournalError(f"{self.path}: corrupt record at line {i + 1}") from exc
        if not read_only and (good_bytes != len(raw) or (good_bytes and raw[good_bytes - 1:] != b"\n")):
            with open(self.path, "r+b") as fh:
                fh.truncate(good_bytes)
                if good_bytes and raw[good_bytes - 1:good_bytes] != b"\n":
                    fh.seek(good_bytes)
                    fh.write(b"\n")
                fh.flush()
                os.fsync(fh.fileno())
        return self.records

    def _apply(self, rec: dict) -> None:
        if rec["event"] == "rehash":
            self.rehashes[rec["from"]] = rec
            self._rehashes.update({(rec["from"], repo): judgement for repo, judgement in rec["to"].items()})
            return                                      # metadata never changes item state or counters
        k, ev = key_str(rec["key"]), rec["event"]
        if ev == "owner_approve" or (ev == "satisfied" and rec.get("origin") == "owner"):
            self._owner_takeovers.add(tuple(rec["key"][:2]))
        if ev == "reopen":
            if k in self._forever or k in self._unknown or (k in self._settled and k in self._protected):
                return  # protected outcomes survive even a stale/invalid reopen on replay
            self._settled.pop(k, None)
            self._superseded.add(k)
            self._assessments.pop(k, None)
            self._retry_assessments.discard(k)
        if rec.get("components"):
            self.components[rec["key"][0]] = rec["components"]
        if ev == "assessment":
            self._assessments[k] = rec
        elif ev in ("defer", "withheld"):
            self._retry_assessments.add(k)
        elif ev in SETTLED_EVENTS or ev in ("applied", "satisfied", "owner_approve", "unknown", "closed"):
            self._retry_assessments.discard(k)
        self.records += 1
        self.last_at = rec.get("at")
        kind, key = rec.get("kind"), tuple(rec["key"])
        identity = (kind, *key[:2], key[3] if kind == "deployment" else 0)
        if ev == "seen":
            self._observations[identity] = (key, rec.get("title", ""), rec.get("environment", ""), rec.get("author", ""))
        if kind == "deployment":
            if ev == "gate_gone":
                self._observations.pop(identity, None)
                self._pending.discard(key)
                self._defer.pop(k, None)
            elif ev not in ("closed",):
                self._pending.add(key)
        if ev == "closed":
            self._observations.pop(identity, None)
        if ev in ("defer", "unknown", "closed", "withheld"):
            self._superseded.add(k)                     # a later event: the old settlement cannot be aliased by a rehash
        if ev in SETTLED_EVENTS or ev in ("applied", "satisfied", "owner_approve", "withheld"):
            self._outcomes[k] = rec.get("action", "approve") if ev in ("applied", "owner_approve") else ev
        if rec.get("kind") == "pr":
            pr_key = f"{rec['key'][0]}#{rec['key'][1]}"
            if ev == "closed":
                self._watched.pop(pr_key, None)
            else:
                self._watched[pr_key] = list(rec["key"][:2])
        if ev in FOREVER_EVENTS or (ev == "applied" and rec.get("action", "approve") == "approve"):
            self._forever[k] = ev
            self._unknown.pop(k, None)
            self._defer.pop(k, None)
        elif ev in SETTLED_EVENTS or ev == "applied":
            self._settled[k] = (ev, rec.get("policy", ""), rec.get("mode", ""), rec.get("fingerprint"))
            if rec.get("protected"):
                self._protected.add(k)
            else:
                self._protected.discard(k)
            self._superseded.discard(k)
            self._defer.pop(k, None)
            if ev == "applied":
                self._unknown.pop(k, None)
        elif ev in ("defer", "withheld"):
            if ev == "withheld":
                self._settled.pop(k, None)              # a previous contextual outcome cannot make it final
            reason = rec.get("reason", "")
            if k not in self._defer or self._defer[k].split(":")[0] != reason.split(":")[0]:
                self._defer_since[k] = rec.get("at", "")        # a new kind of wait (draft -> checks) restarts the clock
            self._defer[k] = reason
            self._defer_kind[k] = rec.get("kind", "")
        elif ev in ("unknown", "owner_attempt"):
            self._unknown[k] = rec
        elif ev == "owner_error":
            self._unknown.pop(k, None)
        elif ev == "notify":
            self._notified.setdefault(k, set()).add(rec.get("tag", ""))
            self.notifications.append(rec)
            del self.notifications[:-NOTIFICATIONS_KEPT]
        if self.observer:
            try:
                self.observer(rec)
            except Exception:
                logging.getLogger("lightkeeper").exception("journal observer failed")

    def append(self, *, kind: str, key, event: str, mode: str, policy: str, **fields) -> dict:
        rec = {"v": RECORD_VERSION, "at": now_iso(), "mode": mode, "kind": kind, "key": list(key), "event": event,
               "policy": policy, **fields}
        return self._append_record(rec, durable=event in ("owner_attempt", "owner_approve", "owner_error")
                                   or fields.get("origin") == "owner")

    def append_rehash(self, legacy_hash: str, judgements: dict[str, str]) -> dict:
        return self._append_record({"v": RECORD_VERSION, "event": "rehash", "from": legacy_hash, "to": dict(judgements), "at": now_iso()},
                                   durable=True)

    def _append_record(self, rec: dict, *, durable: bool = False) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        new = not self.path.exists()
        content = json.dumps(rec, ensure_ascii=False) + "\n"
        started = False
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                if new:
                    os.chmod(self.path, 0o600)
                offset = os.fstat(fh.fileno()).st_size
                started = True
                fh.write(content)
                if durable:
                    fh.flush()
                    os.fsync(fh.fileno())
        except BaseException:
            if started:
                # The failed writer is closed before rollback, including any buffered bytes.
                with open(self.path, "r+b") as fh:
                    fh.truncate(offset)
                    fh.flush()
                    os.fsync(fh.fileno())
            raise
        self._apply(rec)
        return rec

    def assessment(self, key, judgement: str, fingerprint: str) -> dict | None:
        k = key_str(key)
        rec = self._assessments.get(k)
        if (k in self._retry_assessments and rec and rec.get("policy") == judgement
                and rec.get("fingerprint") == fingerprint):
            return rec
        return None

    def settled_policies(self, repo: str) -> set[str]:
        return {value[1] for key, value in self._settled.items() if json.loads(key)[0] == repo}

    def is_final(self, key, policy_hash: str, mode: str, fingerprint: str | None = None) -> bool:
        k = key_str(key)
        if k in self._forever:
            return True
        settled = self._settled.get(k)
        if settled is None or settled[2:] != (mode, fingerprint):
            return False
        if settled[1] == policy_hash:
            return True                                 # direct finality: unchanged semantics
        if k in self._superseded:
            return False
        # A legacy migration may lead through a later frame-only alias. Guard corrupt cycles.
        previous, visited = settled[1], set()
        while previous not in visited:
            visited.add(previous)
            previous = self._rehashes.get((previous, key[0]))
            if previous is None:
                return False
            if previous == policy_hash:
                return True
        return False

    def is_unknown(self, key) -> bool:
        return key_str(key) in self._unknown

    def owner_takeover(self, key) -> bool:
        return tuple(key[:2]) in self._owner_takeovers

    def is_protected(self, key) -> bool:
        k = key_str(key)
        return k in self._settled and k in self._protected

    def unknown_records(self) -> list[dict]:
        """Journal records of writes whose outcome is still unknown (for reconciliation)."""
        return [self._unknown[k] for k in sorted(self._unknown)]

    def defer_reason(self, key) -> str | None:
        return self._defer.get(key_str(key))

    def defer_since(self, key) -> str | None:
        """When the current run of deferrals for this key began (ISO), or None when not deferred."""
        k = key_str(key)
        return self._defer_since.get(k) if k in self._defer else None

    def deferred_keys(self, kind: str) -> list[list]:
        return [json.loads(k) for k, kd in self._defer_kind.items() if kd == kind and k in self._defer]

    def watched_prs(self) -> list[tuple[str, int]]:
        """Open pull requests the tool has ever touched; they stay watched until a `closed` event."""
        return sorted((repo, int(n)) for repo, n in self._watched.values())

    def was_notified(self, key, tag: str | None = None) -> bool:
        tags = self._notified.get(key_str(key), ())
        return bool(tags) if tag is None else tag in tags

    def settled_outcome(self, key) -> str | None:
        return self._outcomes.get(key_str(key))

    def observation_changed(self, kind, key, title="", environment="", author="") -> bool:
        identity = (kind, *key[:2], key[3] if kind == "deployment" else 0)
        return self._observations.get(identity) != (tuple(key), title, environment, author)

    def pending_gates(self) -> list[tuple]:
        return sorted(self._pending)

    def summary(self) -> dict:
        return {"records": self.records, "last_at": self.last_at, "final": len(self._forever),
                "settled": len(self._settled), "watching": [f"{r}#{n}" for r, n in self.watched_prs()],
                "deferred": dict(self._defer), "unknown": sorted(self._unknown),
                "notifications": [{"at": n.get("at"), "text": n.get("text"), "url": n.get("url")} for n in self.notifications]}


class Lock:
    """Exclusive flock so two foreground instances never act on the same requests."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        self._fh = os.fdopen(fd, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            raise JournalError(f"another instance holds {self.path}")
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()
