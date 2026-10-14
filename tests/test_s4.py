"""Owner approvals: synthetic services only, including the Textual confirmation."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch
import importlib.util
import unittest

HAS_UI = all(importlib.util.find_spec(name) is not None for name in ("rich", "textual"))

if HAS_UI:
    from textual.widgets import Button, Input, Static
    from lightkeeper.ui.detail import ConfirmOwnerApprove, PRDetail
    from lightkeeper.ui.textual_app import TextualApp
    from tests.test_textual import contents

from lightkeeper import __main__ as cli
from lightkeeper.commands import Command
from lightkeeper.github import GhError, GhTimeout, PRStatus, parse_pull_request
from lightkeeper.journal import Journal, key_str
from lightkeeper.migrate import settings_toml
from lightkeeper.models import Check, Decision, Review, is_automated, marker_line
from lightkeeper.posting import owner_approval_body
from lightkeeper.ui.model import Runtime
from lightkeeper.ui.project import Observer, Projection
from lightkeeper.worker import RunnerThread
from tests.helpers import FakeGh, FakeReviewer, HEAD, make_file, make_pr, make_settings


class OwnerFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = make_settings()
        self.observer = Observer(Projection(), Runtime())
        self.journal = Journal(self.root / 'journal.jsonl', observer=self.observer.feed)
        self.gh = FakeGh()
        self.pr = make_pr()
        self.key = (self.pr.repo, self.pr.number, HEAD)
        self.gh.pr_snapshots[self.key[:2]] = [self.pr]
        self.runner = cli.Runner(self.settings, self.gh, self.journal, observer=self.observer, live_lock_held=True)
        self.runner.refresh_settings(self.settings)
        self.observer('gh_identity', login='me')

    def disk_settings(self, **repo_fields):
        settings = replace(self.settings, repos={self.pr.repo: replace(self.settings.repo(self.pr.repo), **repo_fields)})
        (settings.config_dir / 'settings.toml').write_text(settings_toml(settings))

    def command(self, note='', key=None, head=HEAD):
        command = Command(str(self.journal.records), 'owner_approve', key or self.key, head, note)
        self.assertTrue(self.runner.commands.put(command))
        self.runner.drain_commands()
        return self.observer.snapshot.runtime.last_command

    def records(self):
        return [json.loads(line) for line in self.journal.path.read_text().splitlines()] if self.journal.path.exists() else []

    def record(self, event, **fields):
        self.runner._journal('pr', self.key, event, **fields)


class OwnerRunnerTests(OwnerFixture, TestCase):
    def test_forged_authority_refusals_reload_disk_before_post(self):
        cases = ('invalid', 'missing', 'login changed', 'disabled', 'removed', 'disallowed', 'global dry',
                 'repo dry', 'no lock', 'identity mismatch', 'identity failure', 'unknown')
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                expected = case
                if case == 'invalid':
                    (self.settings.config_dir / 'settings.toml').write_text('broken = [')
                    expected = 'settings invalid'
                elif case == 'missing':
                    (self.settings.config_dir / 'settings.toml').unlink()
                    expected = 'settings invalid'
                elif case == 'login changed':
                    settings = replace(self.settings, login='different')
                    (settings.config_dir / 'settings.toml').write_text(settings_toml(settings))
                    expected = 'settings invalid'
                elif case == 'disabled':
                    self.disk_settings(enabled=False)
                    expected = 'repository disabled'
                elif case == 'removed':
                    (self.settings.config_dir / 'settings.toml').write_text(settings_toml(replace(self.settings, repos={})))
                    expected = 'repository disabled'
                elif case == 'disallowed':
                    self.disk_settings(allowed=())
                    expected = 'approve disabled'
                elif case == 'global dry':
                    self.runner.global_mode = 'dry-run'
                    expected = 'dry-run'
                elif case == 'repo dry':
                    self.disk_settings(post='dry-run')
                    expected = 'dry-run'
                elif case == 'no lock':
                    self.runner.live_lock_held = False
                    expected = 'live lock not held'
                elif case == 'identity mismatch':
                    self.gh.login = Mock(return_value='different')
                    expected = 'gh login is different'
                elif case == 'identity failure':
                    self.gh.login = Mock(side_effect=GhTimeout(['api'], 1))
                    expected = 'gh login lookup failed'
                elif case == 'unknown':
                    self.record('unknown', action='approve', origin='owner')
                    expected = 'unknown write'
                result = self.command()
                self.assertEqual(result.outcome, 'refused')
                self.assertIn(expected, result.text)
                self.assertFalse(self.gh.writes)

    def test_checks_run_in_contract_order(self):
        self.disk_settings(enabled=False, allowed=(), post='dry-run')
        self.gh.login = Mock(side_effect=AssertionError('must not look up identity'))
        self.assertIn('repository disabled', self.command().text)
        self.disk_settings(allowed=(), post='dry-run')
        self.assertIn('approve disabled', self.command().text)
        self.disk_settings(post='dry-run')
        self.assertIn('dry-run', self.command().text)
        self.disk_settings()
        self.runner.live_lock_held = False
        self.assertIn('live lock not held', self.command().text)
        self.gh.login.assert_not_called()
        self.assertFalse(self.gh.writes)

    def test_fresh_pr_refusals_and_malformed_command(self):
        for changes, expected in [({'head_sha': 'f' * 40}, 'head changed'), ({'state': 'CLOSED'}, 'closed'),
                                  ({'author': 'me'}, 'own pull request'),
                                  ({'reviews': (Review('me', 'PENDING', HEAD), Review('me', 'APPROVED', HEAD))}, 'unsubmitted')]:
            with self.subTest(expected=expected):
                self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, **changes)]
                self.assertIn(expected, self.command().text)
        self.assertIn('invalid PR key', self.command(head='f' * 40).text)
        self.gh.pull_request = Mock(side_effect=GhTimeout(['api'], 1))
        self.assertIn('could not fetch PR', self.command().text)
        self.runner.stop.set()
        self.gh.pull_request = Mock(return_value=self.pr)
        self.assertIn('stopping', self.command().text)
        self.assertFalse(self.gh.writes)

    def test_existing_approval_satisfied_and_duplicate_command_one_post(self):
        first = Command('first', 'owner_approve', self.key, HEAD)
        self.assertTrue(self.runner.commands.put(first))
        self.assertFalse(self.runner.commands.put(replace(first, id='duplicate')))
        self.runner.drain_commands()
        self.assertEqual(self.command().text, 'already approved by you')  # journal protects a stale read
        self.assertEqual(len(self.gh.writes), 1)
        self.assertEqual(self.records()[-1]['event'], 'owner_approve')
        self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, reviews=(Review('me', 'APPROVED', HEAD),))]
        self.assertEqual(self.command().text, 'already approved by you')
        self.assertEqual(len(self.gh.writes), 1)

    def test_existing_github_approval_without_local_record(self):
        self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, reviews=(Review('me', 'APPROVED', HEAD),))]
        self.assertEqual(self.command().text, 'already approved by you')
        self.assertEqual(self.records()[-1]['event'], 'satisfied')
        self.assertFalse(self.gh.writes)

    def test_floor_requires_note_and_other_eligibility_does_not_apply(self):
        cases = [dict(author_is_bot=True), dict(checks=(Check('unit', 'completed', 'failure'),)),
                 dict(files=(make_file(patch=None),))]
        for changes in cases:
            with self.subTest(changes=changes):
                self.setUp()
                self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, **changes)]
                self.assertIn('note required', self.command('   ').text)
                self.assertFalse(self.gh.writes)
                self.assertEqual(self.command('Reviewed the exception.').text, 'approved by you')
        self.setUp()
        self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, is_draft=True, requested_users=(),
                                                    checks=(Check('unit', 'in_progress', ''),))]
        self.assertEqual(self.command().text, 'approved by you')

    def test_body_markers_private_reason_replay_history_count_and_takeover(self):
        self.record('assessment', action='request_changes', reason='PRIVATE REASON', summary='Public summary')
        note = 'Reviewed.\n```\n<!-- comment -->\n' + marker_line('test', 'abcdef', HEAD)
        expected = owner_approval_body(note, 'me')
        self.assertEqual(self.command(note).text, 'approved by you')
        record = self.records()[-1]
        self.assertEqual((record['event'], record['body'], record['review_id']), ('owner_approve', expected, 4242))
        self.assertEqual(self.gh.writes[0][-1], expected)
        self.assertNotIn('PRIVATE REASON', expected)
        self.assertNotIn('Public summary', expected)
        self.assertFalse(is_automated(expected))
        self.assertNotIn('<!--', expected)
        self.assertNotIn('\nlightkeeper (', expected)
        self.assertNotIn('\n```', expected)
        for note in (marker_line('test', 'abcdef', HEAD),
                     '<!-- lightkeeper v2 head=1234567 judgement=abcdefabcdef engine=fake/test -->'):
            self.assertFalse(is_automated(owner_approval_body(note, 'me')))
        replay_observer = Observer(Projection(), self.observer.runtime)
        replay = Journal(self.journal.path, observer=replay_observer.feed)
        replay.replay()
        self.assertTrue(replay.is_final(self.key, 'changed', 'dry-run', 'changed'))
        snapshot = replay_observer.snapshot
        self.assertEqual(snapshot.history[0].why, 'approved by you')
        self.assertEqual(dict(snapshot.stats.posted), {'approve': 1})
        self.runner.journal = replay
        approval = Review('me', 'APPROVED', HEAD, expected)
        self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, head_sha='f' * 40, reviews=(approval,))]
        self.assertEqual(self.runner.process_pr(*self.key[:2]), 'ignore')
        self.assertEqual(len(self.gh.writes), 1)

    def test_uncertain_write_replay_and_manual_only_reconciliation(self):
        for failure in (GhTimeout(['api'], 1), GhError(['api'], 1, 'HTTP 502'), RuntimeError('transport failed'), None):
            with self.subTest(failure=failure):
                self.setUp()
                self.gh.submit_review = Mock(side_effect=failure, return_value=None)
                self.assertTrue(self.command().text.startswith('unknown:'))
                record = self.records()[-1]
                self.assertEqual((record['event'], record['action'], record['origin']), ('unknown', 'approve', 'owner'))
                replay = Journal(self.journal.path, observer=self.observer.feed)
                replay.replay()
                self.runner.journal = replay
                self.assertTrue(replay.is_unknown(self.key))
                self.assertIn('unknown write', self.command().text)
                automated = Review('me', 'APPROVED', HEAD, marker_line('test', 'abcdef', HEAD))
                self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, reviews=(automated,))]
                self.assertEqual(self.runner.reconcile_unknown(), 0)
                manual = replace(automated, body=record['body'])
                self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, reviews=(manual,))]
                self.assertEqual(self.runner.reconcile_unknown(), 1)
                self.assertEqual(self.records()[-1]['event'], 'owner_approve')
                self.assertFalse(replay.is_unknown(self.key))
                self.assertTrue(replay.is_final(self.key, '', '', None))
                self.assertEqual(self.observer.snapshot.history[0].why, 'approved by you')

    def test_definite_error_is_journaled_with_message(self):
        self.gh.fail_write = GhError(['api'], 1, 'HTTP 422: cannot approve')
        self.assertIn('error:', self.command().text)
        record = self.records()[-1]
        self.assertEqual(record['event'], 'owner_error')
        self.assertIn('cannot approve', record['reason'])
        self.assertFalse(self.journal.is_unknown(self.key))

    def test_authority_changes_inside_fetch_prevent_post(self):
        for change in ('disabled', 'approve', 'repo mode', 'removed', 'invalid', 'login', 'identity', 'global mode', 'lock'):
            with self.subTest(change=change):
                self.setUp()
                def fetch(*args):
                    if change == 'disabled':
                        self.disk_settings(enabled=False)
                    elif change == 'approve':
                        self.disk_settings(allowed=())
                    elif change == 'repo mode':
                        self.disk_settings(post='dry-run')
                    elif change == 'removed':
                        (self.settings.config_dir / 'settings.toml').write_text(settings_toml(replace(self.settings, repos={})))
                    elif change == 'invalid':
                        (self.settings.config_dir / 'settings.toml').write_text('broken = [')
                    elif change == 'login':
                        (self.settings.config_dir / 'settings.toml').write_text(settings_toml(replace(self.settings, login='other')))
                    elif change == 'identity':
                        self.gh.login = Mock(return_value='other')
                    elif change == 'global mode':
                        self.runner.global_mode = 'dry-run'
                    else:
                        self.runner.live_lock_held = False
                    return self.pr
                self.gh.pull_request = fetch
                self.assertEqual(self.command().outcome, 'refused')
                self.assertFalse(self.gh.writes)
                self.assertFalse(self.journal.is_unknown(self.key))

    def test_durable_attempt_precedes_final_authority_and_post(self):
        order = []
        original_load = cli.load_settings
        def load(*args):
            order.append('settings')
            return original_load(*args)
        self.gh.pull_request = lambda *args: (order.append('fetch') or self.pr)
        self.gh.login = lambda: (order.append('identity') or 'me')
        def submit(pr, action, body):
            order.append('post')
            replay = Journal(self.journal.path)
            replay.replay()
            self.assertTrue(replay.is_unknown(self.key))
            record = replay.unknown_records()[0]
            self.assertEqual(record['head'], HEAD)
            self.assertEqual(record['body_digest'], hashlib.sha256(body.encode()).hexdigest())
            return 4242
        self.gh.submit_review = submit
        with patch.object(cli, 'load_settings', side_effect=load), patch('lightkeeper.journal.os.fsync') as fsync:
            self.assertEqual(self.command().text, 'approved by you')
        self.assertEqual(order, ['settings', 'identity', 'fetch', 'settings', 'identity', 'post'])
        self.assertGreaterEqual(fsync.call_count, 2)

    def test_outcome_io_failure_blocks_all_writes_and_replay_reconciles(self):
        append = self.journal.append
        def fail_outcome(**fields):
            if fields['event'] == 'owner_approve':
                raise OSError('synthetic disk failure')
            return append(**fields)
        with patch.object(self.journal, 'append', side_effect=fail_outcome):
            self.assertIn('writes blocked until restart', self.command().text)
        self.assertIn('writes blocked until restart', self.command().text)
        other = replace(self.pr, number=8)
        self.gh.pr_snapshots[(other.repo, other.number)] = [other]
        self.assertIn('writes blocked until restart', self.command(key=(other.repo, other.number, HEAD)).text)
        self.assertTrue(any(a.key == 'journal_error' and a.level == 'error' for a in self.observer.snapshot.alerts))
        self.runner.reviewer = FakeReviewer([Decision('approve', 'checked', 'ai', summary='Checked')])
        self.assertEqual(self.runner.process_pr(other.repo, other.number), 'defer')
        self.assertIn('defer', self.runner._boundary(other.repo, 'approve', deployment=True, writing=True))
        self.assertEqual(len(self.gh.writes), 1)
        replay = Journal(self.journal.path, observer=self.observer.feed)
        replay.replay()
        self.assertTrue(replay.is_unknown(self.key))
        self.runner = cli.Runner(self.settings, self.gh, replay, observer=self.observer, live_lock_held=True)
        self.assertIn('unknown write', self.command().text)
        self.assertEqual(self.runner.process_pr(*self.key[:2]), 'unknown')
        body = self.gh.writes[0][-1]
        review = Review('me', 'APPROVED', HEAD, body, 4242, '2026-10-13T10:00:00Z')
        self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, reviews=(review,))]
        self.assertEqual(self.runner.reconcile_unknown(), 1)
        self.assertFalse(replay.is_unknown(self.key))
        self.assertTrue(replay.owner_takeover(self.key))

    def test_takeover_survives_lagging_reviews_and_restart_on_later_head(self):
        self.assertEqual(self.command().text, 'approved by you')
        for restart, head in ((False, 'a' * 40), (True, 'b' * 40)):
            with self.subTest(restart=restart):
                if restart:
                    replay = Journal(self.journal.path)
                    replay.replay()
                    self.runner.journal = replay
                self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, head_sha=head, reviews=())]
                self.runner.reviewer = Mock()
                self.assertEqual(self.runner.process_pr(*self.key[:2]), 'ignore')
                self.runner.reviewer.review_pr.assert_not_called()
                self.assertEqual(self.records()[-1]['reason'], 'taken over by you')

    def test_observed_owner_approval_takeover_survives_restart(self):
        for reconcile in (False, True):
            with self.subTest(reconcile=reconcile):
                self.setUp()
                if reconcile:
                    self.record('owner_attempt', action='approve', origin='owner', body='Attempted note')
                observed = Review('me', 'APPROVED', HEAD, 'Independent approval', 9001,
                                  '2026-10-13T10:00:00Z')
                self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, reviews=(observed,))]
                if reconcile:
                    self.assertEqual(self.runner.reconcile_unknown(), 1)
                else:
                    self.assertEqual(self.command().text, 'already approved by you')
                record = self.records()[-1]
                self.assertEqual((record['event'], record['origin'], record['body'], record['review_id']),
                                 ('satisfied', 'owner', observed.body, observed.review_id))
                self.assertEqual(self.observer.snapshot.history[0].why, 'approved by you on GitHub')
                replay = Journal(self.journal.path)
                replay.replay()
                self.runner = cli.Runner(self.settings, self.gh, replay, observer=self.observer,
                                         live_lock_held=True)
                self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, head_sha='f' * 40, reviews=())]
                self.runner.reviewer = Mock()
                self.assertEqual(self.runner.process_pr(*self.key[:2]), 'ignore')
                self.assertEqual(self.records()[-1]['reason'], 'taken over by you')
                self.runner.reviewer.review_pr.assert_not_called()
                self.assertFalse(self.gh.writes)

    def test_reopen_refuses_takeover_or_protected_settlement_and_releases_target(self):
        for protection in ('takeover', 'settlement'):
            with self.subTest(protection=protection):
                self.setUp()
                if protection == 'takeover':
                    self.assertEqual(self.command().text, 'approved by you')
                else:
                    self.record('ignore', reason='taken over by you', protected=True,
                                fingerprint=self.pr.fingerprint)
                head = 'f' * 40 if protection == 'takeover' else HEAD
                key = (*self.key[:2], head)
                self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, head_sha=head, reviews=())]
                before = self.records()
                self.assertTrue(self.runner.commands.put(Command('reopen', 'reopen', key, head)))
                self.runner.drain_commands()
                result = self.observer.snapshot.runtime.last_command
                self.assertEqual((result.outcome, result.text), ('refused', 'refused: taken over by you'))
                self.assertFalse(self.runner.commands.targets(key))
                self.assertEqual(self.records(), before)
                self.runner.reviewer = Mock()
                self.assertEqual(self.runner.process_pr(*self.key[:2]),
                                 'ignore' if protection == 'takeover' else 'final')
                self.runner.reviewer.review_pr.assert_not_called()
                self.assertEqual(len(self.gh.writes), int(protection == 'takeover'))

    def test_owner_error_does_not_settle_or_erase_protection(self):
        self.gh.fail_write = GhError(['api'], 1, 'HTTP 422: cannot approve')
        self.command()
        self.assertFalse(self.journal.is_final(self.key, self.settings.judgement(self.pr.repo).hash, 'live', self.pr.fingerprint))
        self.runner.reviewer = FakeReviewer([Decision('nothing', 'checked', 'ai')])
        self.assertEqual(self.runner.process_pr(*self.key[:2]), 'nothing')
        self.assertEqual(self.runner.reviewer.calls, 1)
        self.record('ignore', reason='protected settlement', protected=True, fingerprint=self.pr.fingerprint)
        before = self.journal._settled[key_str(self.key)]
        self.command()
        replay = Journal(self.journal.path)
        replay.replay()
        self.assertEqual(replay._settled[key_str(self.key)], before)
        self.assertIn(key_str(self.key), replay._protected)
        self.assertEqual(self.observer.snapshot.history[0].state, 'error')
        self.assertIn('owner write failed', (self.root / 'run.log').read_text())

    def test_already_final_does_not_append_or_misattribute_automated_approval(self):
        self.command()
        before = self.records()
        self.assertEqual(self.command().text, 'already approved by you')
        self.assertEqual(self.records(), before)
        self.setUp()
        automated = Review('me', 'APPROVED', HEAD, marker_line('test', 'abcdef', HEAD))
        self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, reviews=(automated,))]
        self.assertEqual(self.command().text, 'already approved by you')
        self.assertFalse(self.records())
        self.assertFalse(self.gh.writes)

    def test_canonical_note_controls_whitespace_and_mandatory_note(self):
        empty = owner_approval_body('', 'me')
        self.assertEqual(owner_approval_body(' \t\x00\x1b[31m\u200b\u200d\ufeff\ufe0f\u034f\n ', 'me'), empty)
        self.assertEqual(owner_approval_body('A\tB  \nC\u200b  ', 'me'), 'A B\nC\n\n' + empty)
        self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, author_is_bot=True)]
        self.assertIn('note required', self.command('\u200b\t\x00').text)
        self.assertFalse(self.gh.writes)

    def test_blank_looking_characters_are_whitespace_and_cannot_override_floor(self):
        empty = owner_approval_body('', 'me')
        blanks = ('\u2800', '\x00', '\u200b', '\ud800', '\u00a0', '\u2028', '\u2029',
                  '\u3164', '\u115f', '\u1160')
        self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, author_is_bot=True)]
        for blank in blanks:
            with self.subTest(blank=ascii(blank)):
                self.assertEqual(owner_approval_body(blank * 3, 'me'), empty)
                self.assertEqual(owner_approval_body(blank + 'A' + blank * 3 + 'B' + blank, 'me'),
                                 'A B\n\n' + empty)
                self.assertIn('note required', self.command(blank * 3).text)
        self.assertFalse(self.gh.writes)

    def test_reconcile_attributes_observed_body_id_and_day_once(self):
        for matches, days in ((True, 0), (False, 0), (True, 2), (False, 2)):
            with self.subTest(matches=matches, days=days):
                self.setUp()
                self.gh.submit_review = Mock(side_effect=GhTimeout(['api'], 1))
                self.command('Attempted note')
                attempted = self.records()[-1]['body']
                stamp = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
                observed = Review('me', 'APPROVED', HEAD, attempted if matches else 'Independent GitHub approval', 9001, stamp)
                self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, reviews=(observed,))]
                self.assertEqual(self.runner.reconcile_unknown(), 1)
                self.assertEqual(self.runner.reconcile_unknown(), 0)
                record = self.records()[-1]
                self.assertEqual(record['event'], 'owner_approve' if matches else 'satisfied')
                self.assertEqual((record['body'], record['review_id'], record['reviewed_at']), (observed.body, 9001, stamp))
                self.assertEqual(self.observer.snapshot.history[0].why, 'approved by you' if matches else 'approved by you on GitHub')
                self.assertEqual(dict(self.observer.snapshot.stats.posted), {} if days else {'approve': 1})
                projection = Projection()
                replay = Journal(self.journal.path, observer=projection.feed)
                replay.replay()
                projection.feed(record)  # duplicate observations count once
                screen = projection.screen(self.observer.runtime, datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp())
                self.assertEqual(dict(screen.stats.posted), {'approve': 1})

    def test_review_parser_preserves_observed_id_and_time(self):
        review = {'user': {'login': 'me'}, 'state': 'APPROVED', 'commit_id': HEAD,
                  'body': 'Observed approval', 'id': 9001, 'submitted_at': '2026-10-13T10:00:00Z'}
        pr = parse_pull_request('org/repo', {'number': 7, 'headRefOid': HEAD}, [], [review])
        self.assertEqual((pr.reviews[0].review_id, pr.reviews[0].submitted_at),
                         (9001, '2026-10-13T10:00:00Z'))

    def test_unrelated_empty_manual_body_replaces_attempt_in_detail(self):
        self.gh.submit_review = Mock(side_effect=GhTimeout(['api'], 1))
        self.command('Attempted note')
        observed = Review('me', 'APPROVED', HEAD, '', 9001, '2026-10-13T10:00:00Z')
        self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, reviews=(observed,))]
        self.assertEqual(self.runner.reconcile_unknown(), 1)
        self.assertEqual(self.records()[-1]['body'], '')
        self.assertEqual(self.observer.snapshot.details[self.key].latest_body, '')

    def test_owner_target_is_independent_of_retained_reopen(self):
        reopen = Command('reopen', 'reopen', self.key, HEAD)
        self.assertTrue(self.runner.commands.put(reopen))
        self.runner.drain_commands()
        self.assertTrue(self.runner.commands.targets(self.key))
        self.assertEqual(self.command().text, 'approved by you')
        self.assertFalse(self.runner.commands.targets(self.key))
        self.assertEqual(len(self.gh.writes), 1)

    def test_quit_during_post_finishes_and_journals(self):
        entered, release = threading.Event(), threading.Event()
        def submit(*args):
            entered.set()
            if not release.wait(4):
                raise RuntimeError('test timed out')
            return 4242
        self.gh.submit_review = submit
        self.runner.commands.put(Command('write', 'owner_approve', self.key, HEAD))
        worker = RunnerThread(self.runner, self.observer, self.runner.drain_commands, join_timeout=0)
        worker.thread.start()
        try:
            self.assertTrue(entered.wait(2))
            worker.request_stop()
            self.assertFalse(worker.poll_join())
            self.assertEqual(self.observer.snapshot.runtime.phase, 'writing')
        finally:
            release.set()
            worker.join()
        self.assertEqual(worker.exit_code, 0)
        self.assertEqual(self.records()[-1]['event'], 'owner_approve')
        self.assertEqual(self.observer.snapshot.runtime.last_command.text, 'approved by you')


@unittest.skipUnless(HAS_UI, "optional UI dependencies are not installed")
class OwnerModalTests(OwnerFixture, IsolatedAsyncioTestCase):
    def app(self, floor=False):
        self.record('seen', title='Synthetic PR', author='contributor')
        self.record('assessment', action='request_changes', reason='PRIVATE REASON', summary='Public summary')
        if floor:
            self.pr = replace(self.pr, checks=(Check('unit', 'completed', 'failure'),))
            self.gh.pr_snapshots[self.key[:2]] = [self.pr]
        self.runner._publish_pr_status(PRStatus.from_pr(self.pr))
        worker = Mock(runner=self.runner, poll_join=Mock(return_value=False))
        return TextualApp(self.observer, worker, opener=Mock(), clipboard=Mock())

    async def test_unresolved_owner_attempt_detail_shows_unknown_and_reconciliation(self):
        app = self.app()
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press('enter')
            self.assertIsInstance(app.screen, PRDetail)
            self.record('owner_attempt', action='approve', origin='owner', body='Attempted approval')
            self.assertTrue(self.journal.is_unknown(self.key))
            app.refresh_snapshot()
            self.assertIn('Outcome unknown', contents(app))
            self.assertIn('Attempted approval', contents(app))
            self.assertIn('next step awaiting reconciliation', contents(app))

    async def test_braille_blank_cannot_confirm_floor_override(self):
        app = self.app(floor=True)
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press('enter', 'A')
            modal = app.screen
            modal.query_one(Input).value = '\u2800'
            await pilot.pause()
            self.assertEqual(modal.query_one('#owner-preview', Static).render().plain,
                             owner_approval_body('', 'me'))
            self.assertTrue(modal.query_one('#confirm', Button).disabled)
            await pilot.click('#confirm')
            self.assertIs(app.screen, modal)
            self.assertTrue(self.runner.commands.empty())
            self.assertFalse(self.gh.writes)

    async def test_re_review_later_head_refuses_owner_takeover(self):
        self.assertEqual(self.command().text, 'approved by you')
        self.pr = replace(self.pr, head_sha='f' * 40, reviews=())
        self.key = (self.pr.repo, self.pr.number, self.pr.head_sha)
        self.gh.pr_snapshots[self.key[:2]] = [self.pr]
        app = self.app()
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press('enter', 'R')
            await pilot.click('#confirm')
            self.runner.drain_commands()
            app.refresh_snapshot()
            self.assertIn('refused: taken over by you', contents(app))
            self.assertFalse(self.runner.commands.targets(self.key))
            self.runner.reviewer = Mock()
            self.assertEqual(self.runner.process_pr(*self.key[:2]), 'ignore')
            self.runner.reviewer.review_pr.assert_not_called()
            self.assertEqual(len(self.gh.writes), 1)

    async def test_cancel_and_input_focus_guard_no_command(self):
        app = self.app()
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.press('A')
            self.assertIs(app.screen, app.overview)
            await pilot.press('enter', 'A')
            modal = app.screen
            self.assertIsInstance(modal, ConfirmOwnerApprove)
            field = modal.query_one(Input)
            field.focus()
            await pilot.press('q', '1', '2', '3', 'A', 'R', 'r', 'enter')
            self.assertIs(app.screen, modal)
            self.assertTrue(modal.check_action('cancel', ()))
            self.assertTrue(self.runner.commands.empty())
            await pilot.press('escape')
            self.assertIsInstance(app.screen, PRDetail)
            self.assertTrue(self.runner.commands.empty())

    async def test_floor_preview_matches_post_captures_head_and_reports_result(self):
        app = self.app(floor=True)
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press('enter', 'A')
            modal = app.screen
            output = contents(app)
            for expected in ('org/repo#7', HEAD[:7], 'request_changes', 'Public summary', 'failed check: unit',
                             'Effective mode: live', 'gh me', 'expected @me'):
                self.assertIn(expected, output)
            self.assertNotIn('PRIVATE REASON', output)
            self.assertTrue(modal.query_one('#confirm', Button).disabled)
            note = 'Reviewed <!-- lightkeeper v2 head=1234567 judgement=abcdefabcdef engine=fake/test -->'
            modal.query_one(Input).value = note
            await pilot.pause()
            expected = owner_approval_body(note, 'me')
            self.assertEqual(modal.query_one('#owner-preview', Static).render().plain, expected)
            self.assertFalse(modal.query_one('#confirm', Button).disabled)
            detail = app.screen_stack[-2]
            detail.row = replace(detail.row, item=replace(detail.row.item, head='f' * 40))
            self.observer('phase', phase='reviewing', kind='pr', key=('org/repo', 8, HEAD))
            await pilot.click('#confirm')
            self.assertEqual(app.screen.pending_approval, self.runner.commands.queue[0].id)
            self.assertEqual(self.runner.commands.queue[0].head, HEAD)
            self.assertIn('queued, after repo#8', contents(app))
            self.runner.drain_commands()
            app.refresh_snapshot()
            self.assertEqual(self.gh.writes[0][-1], expected)
            self.assertIn('approved by you', contents(app))

    async def test_confirmation_uses_latest_full_floor_and_rejects_new_light_ci(self):
        app = self.app()
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press('enter')
            failed = replace(self.pr, checks=(Check('unit', 'completed', 'failure'),))
            self.runner._publish_pr_status(PRStatus.from_pr(failed))
            await pilot.press('A')
            modal = app.screen
            self.assertIn('failed check: unit', contents(app))
            self.assertTrue(modal.query_one('#confirm', Button).disabled)
            modal.query_one(Input).value = '\u200b\t\x00'
            await pilot.pause()
            self.assertTrue(modal.query_one('#confirm', Button).disabled)
            note = 'Checked\tlocally\u200b  '
            modal.query_one(Input).value = note
            await pilot.pause()
            self.assertEqual(modal.query_one('#owner-preview', Static).render().plain, owner_approval_body(note, 'me'))
            self.assertFalse(modal.query_one('#confirm', Button).disabled)
            running = PRStatus.from_pr(replace(self.pr, checks=(Check('unit', 'in_progress', ''),)))
            self.runner._publish_pr_status(replace(running, failed_checks=None, approve_refusal=''))
            app.refresh_snapshot()
            self.assertIn('CI changed since; reopen to confirm', contents(app))
            self.assertTrue(modal.query_one('#confirm', Button).disabled)
            await pilot.click('#confirm')
            self.assertTrue(self.runner.commands.empty())
            # A newer full fetch restores freshness only for a reopened confirmation.
            self.runner._publish_pr_status(PRStatus.from_pr(self.pr))
            app.refresh_snapshot()
            self.assertTrue(modal.query_one('#confirm', Button).disabled)
            await pilot.press('escape', 'A')
            self.assertIsNot(app.screen, modal)
            self.assertFalse(app.screen.query_one('#confirm', Button).disabled)

    async def test_double_approve_sends_once_and_keeps_first_command(self):
        app = self.app()
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.press('enter', 'A')
            modal = app.screen
            detail = app.screen_stack[-2]
            button = modal.query_one('#confirm', Button)
            modal.on_button_pressed(Button.Pressed(button))
            first = detail.pending_approval
            modal.on_button_pressed(Button.Pressed(button))
            await pilot.pause()
            await pilot.press('A')
            self.assertIs(app.screen, detail)
            self.assertEqual(detail.pending_approval, first)
            self.assertEqual(self.runner.commands.qsize(), 1)
            self.assertEqual(self.runner.commands.queue[0].id, first)
            self.runner.drain_commands()
            app.refresh_snapshot()
            self.assertEqual(len(self.gh.writes), 1)
            self.assertIn('approved by you', contents(app))

    async def test_ui_disables_dry_disabled_disallowed_unknown(self):
        app = self.app()
        runtime = self.observer.runtime
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.press('enter')
            for changes, expected in [({'mode': 'dry-run'}, 'dry-run'), ({'dry_repos': ('org/repo',)}, 'dry-run'),
                                      ({'live_lock_held': False}, 'dry-run'), ({'repos': ()}, 'repository disabled'),
                                      ({'approve_repos': ()}, 'approve disabled')]:
                self.observer.snapshot = replace(self.observer.snapshot, runtime=replace(runtime, **changes))
                await pilot.press('A')
                self.assertIsInstance(app.screen, PRDetail)
                self.assertIn(expected, contents(app))
            self.observer.snapshot = replace(self.observer.snapshot, runtime=runtime,
                                            unknown_items=(app.screen.row.item,))
            await pilot.press('A')
            self.assertIsInstance(app.screen, PRDetail)
            self.assertIn('unknown write', contents(app))
            self.assertTrue(self.runner.commands.empty())
