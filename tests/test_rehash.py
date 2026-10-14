import json
from pathlib import Path
import tempfile
import tomllib
import unittest

from lightkeeper.journal import Journal, SETTLED_EVENTS
from lightkeeper.migrate import legacy_policy_hash, rehash
from lightkeeper.settings import judgement
from lightkeeper.ui.model import Runtime
from lightkeeper.ui.project import Projection

KEY = ("example/widget", 7, "abc")


class RehashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "journal.jsonl"
        self.projection = Projection()
        self.journal = Journal(self.path, observer=self.projection.feed)

    def append(self, event, key=KEY, **fields):
        return self.journal.append(kind="pr", key=key, event=event, policy="old123", mode="live", fingerprint="fp", **fields)

    def test_frame_three_preserves_old_settlement_without_reasking(self):
        from dataclasses import replace
        from lightkeeper.__main__ import Runner
        from lightkeeper._frame_v2 import FRAME, SCHEMA
        from lightkeeper.reviewer import FRAME_VERSION
        from tests.helpers import FakeGh, FakeReviewer, HEAD, make_pr, make_settings
        settings = make_settings()
        previous = judgement(FRAME, settings.rules, "", "important", SCHEMA).hash
        self.assertEqual(previous, "12324e754f32")  # hash produced by the frame-2 release
        self.assertEqual(FRAME_VERSION, "3",
                         "Freeze the frame-3 text and schema before bumping FRAME_VERSION.")
        current = settings.judgement("org/repo").hash
        self.assertNotEqual(previous, current)
        pr = make_pr()
        key = (pr.repo, pr.number, HEAD)
        self.journal.append(kind="pr", key=key, event="nothing", policy=previous,
                            mode="live", fingerprint=pr.fingerprint)
        self.assertEqual(rehash(settings, self.journal), {"frame_2": {pr.repo: current}})
        self.assertEqual(self.journal.rehashes[previous]["to"][pr.repo], current)
        before = self.path.read_bytes()
        self.assertIsNone(rehash(settings, self.journal))
        self.assertEqual(self.path.read_bytes(), before)
        replayed = Journal(self.path)
        replayed.replay()
        for journal in (self.journal, replayed):
            self.assertTrue(journal.is_final(key, current, "live", pr.fingerprint))
            self.assertFalse(journal.is_final(key, current, "dry-run", pr.fingerprint))
            self.assertFalse(journal.is_final(key, current, "live", "changed"))
            changed = replace(settings, rules=settings.rules + "Require another test.")
            rehash(changed, journal)
            self.assertFalse(journal.is_final(key, changed.judgement(pr.repo).hash, "live", pr.fingerprint))
            changed = replace(settings, notes={pr.repo: "Require another test."})
            rehash(changed, journal)
            self.assertFalse(journal.is_final(key, changed.judgement(pr.repo).hash, "live", pr.fingerprint))
            gh, reviewer = FakeGh(), FakeReviewer([])
            gh.pr_snapshots[key[:2]] = [pr]
            runner = Runner(settings, gh, journal, reviewer, live_lock_held=True)
            runner.process_pr(*key[:2])
            self.assertEqual(reviewer.calls, 0)
            self.assertFalse(gh.writes)

    def test_legacy_alias_can_chain_through_frame_upgrade_but_not_cycles(self):
        self.append("nothing")
        self.journal.append_rehash("old123", {KEY[0]: "frame2"})
        self.journal.append_rehash("frame2", {KEY[0]: "frame3"})
        self.assertTrue(self.journal.is_final(KEY, "frame3", "live", "fp"))
        self.journal.append_rehash("frame3", {KEY[0]: "old123"})
        self.assertFalse(self.journal.is_final(KEY, "unrelated", "live", "fp"))
        self.append("defer")
        self.assertFalse(self.journal.is_final(KEY, "frame3", "live", "fp"))

    def test_every_settled_outcome_keeps_mode_fingerprint_and_repo_boundaries(self):
        cases = [(event, {}) for event in SETTLED_EVENTS]
        cases += [("applied", {"action": action}) for action in ("comment", "request_changes")]
        for n, (event, fields) in enumerate(cases):
            key = (KEY[0], n, "abc")
            self.append(event, key, **fields)
        other = ("example/gadget", 7, "abc")
        self.append("nothing", other)
        unmapped = ("example/absent", 7, "abc")
        self.append("nothing", unmapped)
        self.journal.append_rehash("old123", {KEY[0]: "newjudgement", other[0]: "otherjudgement"})
        for n, (event, _) in enumerate(cases):
            key = (KEY[0], n, "abc")
            with self.subTest(event=event, n=n):
                self.assertTrue(self.journal.is_final(key, "newjudgement", "live", "fp"))
                self.assertTrue(self.journal.is_final(key, "old123", "live", "fp"))
                self.assertFalse(self.journal.is_final(key, "newjudgement", "dry-run", "fp"))
                self.assertFalse(self.journal.is_final(key, "newjudgement", "live", "changed"))
                self.assertFalse(self.journal.is_final(key, "changedrules", "live", "fp"))
        self.assertTrue(self.journal.is_final(other, "otherjudgement", "live", "fp"))
        self.assertFalse(self.journal.is_final(other, "newjudgement", "live", "fp"))
        self.assertFalse(self.journal.is_final(unmapped, "newjudgement", "live", "fp"))
        self.journal.append_rehash("differentlegacy", {KEY[0]: "differentjudgement"})
        self.assertFalse(self.journal.is_final(KEY, "differentjudgement", "live", "fp"))

    def test_metadata_preserves_closed_unknown_deferred_forever_and_projection(self):
        events = [("closed", {}), ("unknown", {"action": "comment"}), ("defer", {"reason": "checks"}),
                  ("satisfied", {}), ("applied", {"action": "approve"}), ("would_comment", {}),
                  ("applied", {"action": "comment"})]
        for n, (event, fields) in enumerate(events):
            self.append(event, (KEY[0], n, "abc"), **fields)
        self.journal.append(kind="deployment", key=[KEY[0], 80, 1, 2], event="defer", mode="live",
                            policy="old123", reason="jobs running")
        summary = self.journal.summary()
        runtime = Runtime(repos=(KEY[0],))
        screen = self.projection.screen(runtime, 0)
        pending = self.journal.pending_gates()
        deferred = self.journal.deferred_keys("pr")
        unknown = self.journal.unknown_records()
        record = self.journal.append_rehash("old123", {KEY[0]: "newjudgement"})
        self.assertEqual(set(record), {"v", "event", "from", "to", "at"})
        self.assertEqual(record['v'], 2)
        self.assertEqual(record["event"], "rehash")
        self.assertEqual(json.loads(self.path.read_text().splitlines()[-1]), record)
        self.projection.feed(record)                       # also safe when fed directly during replay
        self.assertEqual(self.projection.screen(runtime, 0), screen)
        self.assertEqual(self.journal.summary(), summary)
        self.assertEqual(self.journal.pending_gates(), pending)
        self.assertEqual(self.journal.deferred_keys("pr"), deferred)
        self.assertEqual(self.journal.unknown_records(), unknown)
        self.assertNotIn((KEY[0], 0), self.journal.watched_prs())
        for n in (0, 1, 2):
            self.assertFalse(self.journal.is_final((KEY[0], n, "abc"), "newjudgement", "live", "fp"))
        for n in (3, 4):
            self.assertTrue(self.journal.is_final((KEY[0], n, "abc"), "anything", "dry-run", "changed"))
        self.assertEqual(self.journal.defer_reason((KEY[0], 2, "abc")), "checks")
        self.assertTrue(self.journal.is_unknown((KEY[0], 1, "abc")))
        # Repeated metadata is idempotent, including when replayed from disk twice on fresh projections.
        self.journal.append_rehash("old123", {KEY[0]: "newjudgement"})
        self.assertEqual(self.journal.summary(), summary)
        for _ in range(2):
            projection = Projection()
            replayed = Journal(self.path, observer=projection.feed)
            replayed.replay()
            self.assertEqual(replayed.summary(), summary)
            self.assertEqual(projection.screen(runtime, 0), screen)
            self.assertTrue(replayed.is_final((KEY[0], 6, "abc"), "newjudgement", "live", "fp"))

    def test_metadata_only_replay_twice_on_same_journal(self):
        self.journal.append_rehash("old123", {KEY[0]: "newjudgement"})
        before = self.journal.summary()
        self.assertEqual(self.journal.replay(), 0)
        self.assertEqual(self.journal.replay(), 0)
        self.assertEqual(self.journal.summary(), before)

    def test_superseded_settlements_stay_nonfinal_on_same_pr_and_deployment_key(self):
        for kind, key in (("pr", KEY), ("deployment", (KEY[0], 80, 1, 2))):
            for event in ("defer", "unknown", "closed"):
                with self.subTest(kind=kind, event=event):
                    path = self.path.with_name(f"{kind}-{event}.jsonl")
                    journal = Journal(path)
                    fields = dict(kind=kind, key=key, policy="old123", mode="live", fingerprint="fp")
                    journal.append(event="nothing", **fields)
                    self.assertTrue(journal.is_final(key, "old123", "live", "fp"))
                    journal.append(event=event, **fields)
                    journal.append_rehash("old123", {key[0]: "newjudgement"})
                    replayed = Journal(path)
                    replayed.replay()
                    for state in (journal, replayed):
                        # Direct finality keeps today's semantics; the alias must not revive a superseded settlement.
                        self.assertFalse(state.is_final(key, "newjudgement", "live", "fp"))
                        self.assertFalse(state.is_final(key, "newjudgement", "live", "fp"))

    def test_forever_outcomes_survive_later_events_and_rehash_on_same_key(self):
        for kind, key in (("pr", KEY), ("deployment", (KEY[0], 80, 1, 2))):
            for outcome in ("applied", "satisfied"):
                for later in (None, "defer", "unknown", "closed"):
                    with self.subTest(kind=kind, outcome=outcome, later=later):
                        path = self.path.with_name(f"{kind}-{outcome}-{later}.jsonl")
                        journal = Journal(path)
                        fields = dict(kind=kind, key=key, policy="old123", mode="live", fingerprint="fp")
                        journal.append(event=outcome, **fields)
                        if later:
                            journal.append(event=later, **fields)
                        journal.append_rehash("old123", {key[0]: "newjudgement"})
                        replayed = Journal(path)
                        replayed.replay()
                        for state in (journal, replayed):
                            self.assertTrue(state.is_final(key, "newjudgement", "live", "fp"))
                            self.assertTrue(state.is_final(key, "anything", "dry-run", "changed"))

    def test_changed_legacy_rules_do_not_match_the_migrated_alias(self):
        fixture = Path(__file__).parent / "fixtures/migration"
        raw = tomllib.loads((fixture / "policy.example.toml").read_text())
        rules = (fixture / "RULES.md").read_text()
        old_hash = legacy_policy_hash(raw, rules)
        changed_rules = rules + "\nRequire an explicit regression test.\n"
        changed_hash = legacy_policy_hash(raw, changed_rules)
        migrated = judgement("1", rules, "", "important", "schema").hash
        changed = judgement("1", changed_rules, "", "important", "schema").hash
        self.assertNotEqual(old_hash, changed_hash)
        self.journal.append(kind="pr", key=KEY, event="nothing", policy=old_hash, mode="live", fingerprint="fp")
        self.journal.append_rehash(old_hash, {KEY[0]: migrated})
        self.assertTrue(self.journal.is_final(KEY, migrated, "live", "fp"))
        self.assertFalse(self.journal.is_final(KEY, changed_hash, "live", "fp"))
        self.assertFalse(self.journal.is_final(KEY, changed, "live", "fp"))
        self.journal.append(kind="pr", key=KEY, event="nothing", policy=changed_hash, mode="live", fingerprint="fp")
        self.assertTrue(self.journal.is_final(KEY, changed_hash, "live", "fp"))
        self.assertFalse(self.journal.is_final(KEY, migrated, "live", "fp"))
        replayed = Journal(self.path)
        replayed.replay()
        self.assertFalse(replayed.is_final(KEY, migrated, "live", "fp"))
