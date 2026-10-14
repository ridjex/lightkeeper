import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from lightkeeper.__main__ import EVENT_WORDS
from lightkeeper.journal import Journal
from lightkeeper.ui.model import Runtime
from lightkeeper.ui.project import Projection

KEY = ("example/widget", 7, "abcdef0")
REASON = "no publishable rationale after the remarks filter"


class WithheldTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "journal.jsonl"
        self.projection = Projection()
        self.journal = Journal(self.path, observer=self.projection.feed)
        self.runtime = Runtime(repos=(KEY[0],))

    def append(self, event="withheld", **fields):
        return self.journal.append(kind="pr", key=KEY, event=event, mode="live", policy="legacy",
                                   **(dict(action="request_changes", reason=REASON, fingerprint="f") | fields))

    def assert_withheld(self, journal):
        for policy in ("legacy", "new"):
            for mode in ("live", "dry-run"):
                for fingerprint in ("f", "other", None):
                    self.assertFalse(journal.is_final(KEY, policy, mode, fingerprint))
        self.assertEqual(journal.settled_outcome(KEY), "withheld")
        self.assertEqual(journal.watched_prs(), [(KEY[0], KEY[1])])
        self.assertEqual(journal.deferred_keys("pr"), [list(KEY)])
        self.assertEqual(journal.defer_reason(KEY), REASON)
        self.assertIsNotNone(journal.defer_since(KEY))
        self.assertEqual(journal.summary()["settled"], 0)
        self.assertFalse(journal.is_unknown(KEY))

    def test_append_replay_outcome_and_not_final(self):
        rec = self.append()
        self.assertEqual(EVENT_WORDS["withheld"], "withheld")
        self.assertEqual((rec["action"], rec["reason"]), ("request_changes", REASON))
        self.assert_withheld(self.journal)
        projection = Projection()
        replay = Journal(self.path, observer=projection.feed)
        self.assertEqual(replay.replay(), 1)
        self.assert_withheld(replay)
        self.assertEqual(json.loads(self.path.read_text()), rec)
        self.assertEqual(projection.screen(self.runtime, 0), self.projection.screen(self.runtime, 0))

    def test_withheld_supersedes_contextual_settlement_and_rehash(self):
        self.append("would_comment")
        self.assertTrue(self.journal.is_final(KEY, "legacy", "live", "f"))
        self.journal.append_rehash("legacy", {KEY[0]: "new"})
        self.assertTrue(self.journal.is_final(KEY, "new", "live", "f"))
        self.append()
        self.assert_withheld(self.journal)
        replay = Journal(self.path)
        replay.replay()
        self.assert_withheld(replay)
        self.append("applied", action="comment")
        self.assertTrue(self.journal.is_final(KEY, "legacy", "live", "f"))
        self.assertEqual(self.journal.deferred_keys("pr"), [])
        self.assertEqual(self.journal.settled_outcome(KEY), "comment")

    def test_withheld_keeps_existing_defer_and_watch_and_does_not_undo_forever(self):
        self.append("defer", reason="checks pending")
        self.append()
        self.assert_withheld(self.journal)
        self.append("applied", action="approve")
        self.append()
        self.assertTrue(self.journal.is_final(KEY, "anything", "dry-run", "other"))

    def test_forever_final_outcomes_survive_withheld_and_replay(self):
        for event in ("applied", "satisfied"):
            with self.subTest(event=event):
                path = self.path.with_name(f"{event}.jsonl")
                journal = Journal(path)
                for current in (event, "withheld"):
                    journal.append(kind="pr", key=KEY, event=current, mode="live", policy="legacy",
                                   action="approve", reason=REASON, fingerprint="f")
                journal.append_rehash("legacy", {KEY[0]: "new"})
                replay = Journal(path)
                self.assertEqual(replay.replay(), 2)
                for state in (journal, replay):
                    for policy in ("legacy", "new", "other"):
                        for mode in ("live", "dry-run"):
                            for fingerprint in ("f", "other", None):
                                self.assertTrue(state.is_final(KEY, policy, mode, fingerprint))
                    self.assertEqual(state.summary()["final"], 1)
                    self.assertEqual(state.settled_outcome(KEY), "withheld")

    def test_deployment_withheld_then_gate_gone_through_replay(self):
        key = (KEY[0], 100, 1, 10)
        for event in ("seen", "withheld", "gate_gone"):
            self.journal.append(kind="deployment", key=key, event=event, mode="live", policy="legacy",
                                action="approve", reason="deployments disabled", environment="staging")
            replay_projection = Projection()
            replay = Journal(self.path, observer=replay_projection.feed)
            replay.replay()
            for journal, projection in ((self.journal, self.projection), (replay, replay_projection)):
                with self.subTest(event=event, replay=journal is replay):
                    screen = projection.screen(self.runtime, 0)
                    self.assertFalse(journal.is_final(key, "legacy", "live"))
                    self.assertEqual(journal.watched_prs(), [])
                    self.assertEqual(journal.pending_gates(), [] if event == "gate_gone" else [key])
                    self.assertEqual(len(screen.watching), 0 if event == "gate_gone" else 1)
                    self.assertEqual(journal.deferred_keys("deployment"),
                                     [list(key)] if event == "withheld" else [])
                    self.assertEqual((screen.stats.posted, screen.stats.dry_run), ((), ()))
                    if event == "withheld":
                        self.assertEqual(screen.watching[0].state, "withheld")
                        self.assertEqual(journal.defer_reason(key), "deployments disabled")
                    if event == "gate_gone":
                        self.assertIsNone(journal.defer_reason(key))
                        self.assertIsNone(journal.defer_since(key))
                        self.assertEqual(screen.history[0].why, "approve: deployments disabled")
            self.assertEqual(replay_projection.screen(self.runtime, 0), self.projection.screen(self.runtime, 0))

    def test_projection_row_history_and_counts(self):
        self.append("seen", title="An example change")
        self.append()
        screen = self.projection.screen(self.runtime, 0)
        row, history = screen.watching[0], screen.history[0]
        self.assertEqual((row.state, row.why, row.item.title, row.dry_run),
                         ("withheld", REASON, "An example change", False))
        self.assertEqual(f"{history.state} {history.why}", f"withheld request_changes: {REASON}")
        self.assertEqual((screen.stats.posted, screen.stats.dry_run), ((), ()))
        self.append()
        self.assertEqual(self.projection.screen(self.runtime, 0).history[0].repeats, 2)
        self.append(action="comment")
        history = self.projection.screen(self.runtime, 0).history
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].why, f"comment: {REASON}")

    @unittest.skipUnless(importlib.util.find_spec("rich"), "Rich is optional")
    def test_rich_glyph_word_and_history(self):
        from rich.console import Console
        from lightkeeper.ui.rich_view import glyph, render, word
        self.append(reason="action disabled")
        screen = self.projection.screen(self.runtime, 0)
        for ascii, expected in ((False, "–"), (True, "-")):
            with self.subTest(ascii=ascii):
                self.assertEqual(glyph("withheld", 0, ascii).plain, expected)
                self.assertEqual(word(screen.watching[0]), "withheld:")
                output = io.StringIO()
                console = Console(file=output, width=200, height=30, force_terminal=False)
                console.print(render(screen, 0, 200, 30, ascii))
                text = output.getvalue()
                self.assertIn("withheld: request_changes: action disabled", " ".join(text.split()))
                self.assertIn(expected, text)
