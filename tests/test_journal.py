import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from lightkeeper.journal import Journal, JournalError, Lock

KEY = ("org/repo", 7, "abc")


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "journal.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def test_failed_partial_append_rolls_back_before_later_append(self):
        journal = Journal(self.path)
        journal.append(kind='pr', key=KEY, event='nothing', mode='live', policy='p1')
        before = self.path.read_bytes()
        real_open = open

        def broken_open(path, mode, **kwargs):
            stream = real_open(path, mode, **kwargs)
            if mode != 'a':
                return stream
            wrapper = Mock(wraps=stream)
            wrapper.__enter__ = Mock(return_value=wrapper)
            wrapper.__exit__ = Mock(side_effect=lambda *args: stream.close())
            def partial_write(content):
                stream.write(content[:25])
                stream.flush()
                raise OSError('partial append')
            wrapper.write.side_effect = partial_write
            return wrapper

        with patch('lightkeeper.journal.open', side_effect=broken_open), self.assertRaisesRegex(OSError, 'partial append'):
            journal.append(kind='pr', key=KEY, event='defer', mode='live', policy='p1')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(journal.records, 1)
        journal.append(kind='pr', key=KEY, event='defer', mode='live', policy='p1')
        self.assertEqual(Journal(self.path).replay(), 2)

    def test_newer_record_stops_before_any_mutation_or_tail_repair(self):
        journal = Journal(self.path)
        first = journal.append(kind='pr', key=KEY, event='nothing', mode='live', policy='p1')
        for tail in (b'', b'\n', b'\n{"v":'):
            for read_only in (False, True):
                content = (json.dumps(first) + '\n' + json.dumps({**first, 'v': 3})).encode() + tail
                self.path.write_bytes(content)
                observer = Mock()
                replay = Journal(self.path, observer)
                with self.assertRaisesRegex(JournalError, r'journal written by a newer lightkeeper \(record version 3\); upgrade'):
                    replay.replay(read_only=read_only)
                self.assertEqual(replay.records, 0)
                self.assertEqual(replay.watched_prs(), [])
                observer.assert_not_called()
                self.assertEqual(self.path.read_bytes(), content)

    def test_durable_append_fsync_failure_rolls_back_and_syncs_rollback(self):
        journal = Journal(self.path)
        journal.append(kind='pr', key=KEY, event='nothing', mode='live', policy='p1')
        before = self.path.read_bytes()
        with patch('lightkeeper.journal.os.fsync', side_effect=[OSError('sync failed'), None]) as sync:
            with self.assertRaisesRegex(OSError, 'sync failed'):
                journal.append(kind='pr', key=KEY, event='owner_attempt', mode='live', policy='p1')
            self.assertEqual(sync.call_count, 2)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(journal.is_unknown(KEY))
        journal.append(kind='pr', key=KEY, event='owner_attempt', mode='live', policy='p1')
        self.assertEqual(Journal(self.path).replay(), 2)

    def test_append_then_replay(self):
        j = Journal(self.path)
        j.append(kind="pr", key=KEY, event="applied", mode="live", policy="p1", reason="ok", review_id=1)
        j2 = Journal(self.path)
        self.assertEqual(j2.replay(), 1)
        self.assertTrue(j2.is_final(KEY, "other", "dry-run"))                 # approve applied: final forever
        rec = json.loads(self.path.read_text().splitlines()[0])
        self.assertEqual((rec["kind"], rec["event"], rec["key"], rec["review_id"]), ("pr", "applied", list(KEY), 1))
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_every_byte_truncation_of_unicode_tail_then_append(self):
        journal = Journal(self.path)
        journal.append(kind="pr", key=KEY, event="nothing", mode="live", policy="p1", reason="first")
        prefix = self.path.read_bytes()
        journal.append(kind="pr", key=KEY, event="defer", mode="live", policy="p1", reason="café 日本 🔭")
        record = self.path.read_bytes()[len(prefix):]
        for cut in range(len(record) + 1):
            with self.subTest(cut=cut):
                torn = prefix + record[:cut]
                self.path.write_bytes(torn)
                expected = 2 if cut >= len(record) - 1 else 1
                self.assertEqual(Journal(self.path).replay(read_only=True), expected)
                self.assertEqual(self.path.read_bytes(), torn)
                recovered = Journal(self.path)
                self.assertEqual(recovered.replay(), expected)
                self.assertTrue(self.path.read_bytes().endswith(b"\n"))
                recovered.append(kind="pr", key=KEY, event="nothing", mode="live", policy="p1", reason="after recovery")
                self.assertEqual(Journal(self.path).replay(), expected + 1)

    def test_malformed_terminated_records_report_line_number(self):
        Journal(self.path).append(kind="pr", key=KEY, event="nothing", mode="live", policy="p1")
        prefix = self.path.read_bytes()
        for bad in (b'\xff', b'{"event":', b'{}', b'[]'):
            for read_only in (False, True):
                with self.subTest(bad=bad, read_only=read_only):
                    content = prefix + bad + b'\n'
                    self.path.write_bytes(content)
                    with self.assertRaisesRegex(JournalError, 'line 2'):
                        Journal(self.path).replay(read_only=read_only)
                    self.assertEqual(self.path.read_bytes(), content)

    def test_parseable_invalid_tail_is_not_discarded(self):
        for record in ({}, {'event': 'defer', 'kind': 'pr', 'key': []}):
            with self.subTest(record=record):
                content = json.dumps(record).encode('utf-8')
                self.path.write_bytes(content)
                with self.assertRaisesRegex(JournalError, 'line 1'):
                    Journal(self.path).replay()
                self.assertEqual(self.path.read_bytes(), content)

    def test_settled_outcomes_are_per_policy_mode_and_fingerprint(self):
        for event in ("nothing", "ignore", "unclear", "would_approve", "would_comment"):
            j = Journal(self.path)
            j.append(kind="pr", key=KEY, event=event, mode="live", policy="p1", reason="r", fingerprint="f1")
            self.assertTrue(j.is_final(KEY, "p1", "live", "f1"), event)
            self.assertFalse(j.is_final(KEY, "p2", "live", "f1"), event)      # rules edited
            self.assertFalse(j.is_final(KEY, "p1", "dry-run", "f1"), event)   # mode flipped
            self.assertFalse(j.is_final(KEY, "p1", "live", "f2"), event)      # checks rerun / description edited

    def test_comment_and_request_changes_are_settled_not_forever(self):
        j = Journal(self.path)
        j.append(kind="pr", key=KEY, event="applied", action="comment", mode="live", policy="p1", fingerprint="f1")
        self.assertTrue(j.is_final(KEY, "p1", "live", "f1"))
        self.assertFalse(j.is_final(KEY, "p1", "live", "f2"))

    def test_defer_unknown_and_watching(self):
        j = Journal(self.path)
        self.assertEqual(j.watched_prs(), [])
        j.append(kind="pr", key=KEY, event="defer", mode="live", policy="p1", reason="ci pending")
        self.assertEqual(j.defer_reason(KEY), "ci pending")
        self.assertEqual(j.defer_since(KEY), j.last_at)
        self.assertEqual(j.deferred_keys("pr"), [list(KEY)])
        self.assertEqual(j.watched_prs(), [("org/repo", 7)])
        self.assertFalse(j.is_final(KEY, "p1", "live"))
        j.append(kind="pr", key=KEY, event="unknown", mode="live", policy="p1", reason="timeout", action="comment")
        self.assertTrue(j.is_unknown(KEY))
        self.assertEqual(j.unknown_records()[0]["action"], "comment")
        j.append(kind="pr", key=KEY, event="applied", mode="live", policy="p1", action="comment", fingerprint="f")
        self.assertFalse(j.is_unknown(KEY))                                      # a reconciled comment clears unknown
        j.append(kind="pr", key=KEY, event="unknown", mode="live", policy="p1", reason="timeout", action="approve")
        j.append(kind="pr", key=KEY, event="satisfied", mode="live", policy="p1", reason="seen")
        self.assertFalse(j.is_unknown(KEY))
        self.assertIsNone(j.defer_since(KEY))
        self.assertTrue(j.is_final(KEY, "p1", "live"))
        self.assertEqual(j.watched_prs(), [("org/repo", 7)])                   # still open: a push re-evaluates
        j.append(kind="pr", key=KEY, event="closed", mode="live", policy="p1", reason="merged")
        self.assertEqual(Journal(self.path).replay(), 6)
        self.assertEqual(j.watched_prs(), [])
        self.assertTrue(j.is_final(KEY, "p1", "live"))                            # from the earlier satisfied, not from closed
        other = ("org/repo", 8, "def")
        j.append(kind="pr", key=other, event="defer", mode="live", policy="p1", reason="draft")
        j.append(kind="pr", key=other, event="closed", mode="live", policy="p1", reason="closed")
        self.assertFalse(j.is_final(other, "p1", "live"))                        # reopened at the same head: reviewed again

    def test_defer_clock_restarts_when_the_kind_of_wait_changes(self):
        j = Journal(self.path)
        j.append(kind="pr", key=KEY, event="defer", mode="live", policy="p1", reason="draft")
        j.defer_since(KEY)
        j._apply({"key": list(KEY), "event": "defer", "kind": "pr", "reason": "checks still running: a", "at": "2026-01-01T00:00:00Z"})
        self.assertEqual(j.defer_since(KEY), "2026-01-01T00:00:00Z")
        j._apply({"key": list(KEY), "event": "defer", "kind": "pr", "reason": "checks still running: b", "at": "2026-01-01T01:00:00Z"})
        self.assertEqual(j.defer_since(KEY), "2026-01-01T00:00:00Z")           # same kind of wait keeps the clock

    def test_notifications_are_kept_and_tagged(self):
        j = Journal(self.path)
        j.append(kind="pr", key=KEY, event="notify", mode="live", policy="p1", text="look", tag="ci-wait")
        self.assertTrue(j.was_notified(KEY, "ci-wait"))
        self.assertFalse(j.was_notified(KEY, "other"))
        self.assertEqual(j.summary()["notifications"], [{"at": j.last_at, "text": "look", "url": None}])

    def test_incomplete_last_line_is_dropped_but_corruption_raises(self):
        j = Journal(self.path)
        j.append(kind="pr", key=KEY, event="ignore", mode="live", policy="p1", reason="x")
        with open(self.path, "a") as fh:
            fh.write('{"v":1,"at":"2026-09-17T00:00:00Z","mode":"live","kind":"pr","key":["a",1,"b"],"ev')
        self.assertEqual(Journal(self.path).replay(), 1)
        self.assertTrue(self.path.read_text().endswith("}\n"))          # fragment truncated on disk
        j3 = Journal(self.path)
        j3.replay()
        j3.append(kind="pr", key=("a", 2, "c"), event="ignore", mode="live", policy="p1", reason="y")
        self.assertEqual(Journal(self.path).replay(), 2)
        self.path.write_text('garbage\n' + self.path.read_text())
        with self.assertRaises(JournalError):
            Journal(self.path).replay()

    def test_summary(self):
        j = Journal(self.path)
        j.append(kind="pr", key=KEY, event="defer", mode="live", policy="p1", reason="ci")
        s = j.summary()
        self.assertEqual(s["deferred"], {json.dumps(list(KEY), separators=(",", ":")): "ci"})
        self.assertEqual((s["records"], s["watching"]), (1, ["org/repo#7"]))


class LockTests(unittest.TestCase):
    def test_second_holder_fails(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state" / "lock"
            with Lock(path):
                self.assertEqual(oct(path.parent.stat().st_mode & 0o777), "0o700")
                with self.assertRaises(JournalError):
                    with Lock(path):
                        pass
            with Lock(path):
                pass


if __name__ == "__main__":
    unittest.main()
