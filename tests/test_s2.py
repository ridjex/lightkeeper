"""Snapshot, Runner and Textual contracts; all data and services are synthetic."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import tempfile
import threading
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch
import importlib.util
import unittest

HAS_UI = all(importlib.util.find_spec(name) is not None for name in ("rich", "textual"))

if HAS_UI:
    from rich.text import Text
    from textual.widgets import DataTable, Input
    from lightkeeper.ui.detail import ConfirmReopen, PRDetail, session_path
    from lightkeeper.ui.textual_app import TextualApp
    from tests.test_textual import contents

from lightkeeper import __main__ as cli
from lightkeeper.commands import Command, CommandInbox
from lightkeeper.engines.base import Identity
from lightkeeper.github import GhError, PRStatus
from lightkeeper.journal import Journal, key_str
from lightkeeper.models import Check, Decision, Review, marker_line
from lightkeeper.ui.model import Alert, Runtime
from lightkeeper.ui.project import Observer, Projection
from tests.helpers import FakeGh, FakeReviewer, HEAD, make_pr, make_settings
from tests.test_ui import NOW, fixture_screen


class EventTests(TestCase):
    def test_records_are_ordered_bounded_immutable_and_private(self):
        p = Projection()
        base = dict(kind='pr', key=['org/repo', 7, HEAD], policy='policy', mode='live')
        for n in range(25):
            p.feed(dict(base, at=f'2026-10-10T09:00:{n:02}Z', event='applied', action='comment',
                        reason='private detail', summary=f'public {n}', published_findings=2,
                        body='public body', engine='fake/test', session='/state/sessions/test', url='https://example.test'))
        s = p.screen(Runtime(repos=('org/repo',)), NOW)
        key = tuple(base['key'])
        events = s.detail(key)
        self.assertEqual(len(events), 20)
        self.assertEqual([e.summary for e in events], [f'public {n}' for n in range(5, 25)])
        self.assertEqual(sorted(e.at for e in events), [e.at for e in events])
        self.assertEqual(events[-1].private_reason, 'private detail')
        self.assertEqual((events[-1].published_findings, events[-1].engine), (2, 'fake/test'))
        self.assertNotIn('private detail', s.watching[0].why)
        with self.assertRaises(FrozenInstanceError):
            events[0].summary = 'changed'
        with self.assertRaises(TypeError):
            s.events[key] = ()
        p.feed(dict(base, at='2026-10-10T10:00:00Z', event='reopen'))
        self.assertEqual(s.detail(key), events)
        self.assertEqual(p.screen(s.runtime, NOW).watching[0].why, 're-review queued')
        self.assertEqual(s.detail(('org/repo', 8, HEAD)), ())

    def test_withheld_private_reason_and_unknown_projection(self):
        p = Projection()
        rec = dict(kind='pr', key=['org/repo', 7, HEAD], at='2026-10-10T10:00:00Z')
        p.feed(dict(rec, event='withheld', action='comment', reason='comment disabled',
                    private_reason='model private', summary='public', body='proposal'))
        p.feed(dict(rec, event='unknown', body='attempt'))
        s = p.screen(Runtime(repos=('org/repo',)), NOW)
        self.assertEqual(s.detail(tuple(rec['key']))[0].private_reason, 'model private')
        self.assertEqual([i.number for i in s.unknown_items], [7])
        p.feed(dict(rec, event='applied', action='comment', source='observed'))
        self.assertFalse(p.screen(s.runtime, NOW).unknown_items)
        self.assertTrue(s.unknown_items)  # previous snapshot never mutates

    def test_sticky_detail_survives_timeline_eviction_and_keys_are_pruned(self):
        p = Projection(history_limit=2)
        rec = dict(kind='pr', key=['org/repo', 7, HEAD], at='2026-10-10T10:00:00Z')
        p.feed(dict(rec, event='unknown', body='attempted body', session='/state/session',
                    private_reason='private detail', summary='public summary'))
        p.feed(dict(rec, event='applied', action='comment', source='observed'))
        before = p.screen(Runtime(repos=('org/repo',)), NOW)
        for _ in range(25):
            p.feed(dict(rec, event='notify', text='notice'))
        snapshot = p.screen(before.runtime, NOW)
        detail = snapshot.details[tuple(rec['key'])]
        self.assertEqual(len(snapshot.detail(tuple(rec['key']))), 20)
        self.assertEqual((detail.latest_body, detail.latest_session, detail.latest_private_reason,
                          detail.latest_outcome, detail.latest_summary),
                         ('attempted body', '/state/session', 'private detail', 'applied', 'public summary'))
        with self.assertRaises(TypeError):
            snapshot.details[tuple(rec['key'])] = detail
        p.feed(dict(rec, event='closed'))
        for n in range(8, 18):
            p.feed(dict(rec, key=['org/repo', n, HEAD], event='closed'))
        self.assertEqual(len(p._events), 2)
        self.assertEqual(set(p._events), set(p._details))
        self.assertNotIn(tuple(rec['key']), p._events)
        self.assertEqual(before.details[tuple(rec['key'])].latest_body, 'attempted body')

    def test_full_status_names_survive_unchanged_light_poll_only(self):
        p = Projection()
        pr = make_pr(is_draft=True, checks=(Check('unit', 'completed', 'failure'),))
        p.feed(dict(kind='pr', key=[pr.repo, pr.number, pr.head_sha], event='seen', at='2026-10-10T10:00:00Z'))
        full = PRStatus.from_pr(pr)
        p.pr_status(full)
        p.pr_status(replace(full, failed_checks=None))
        row = p.screen(Runtime(repos=(pr.repo,)), NOW).watching[0]
        self.assertTrue(row.is_draft)
        self.assertEqual(row.failed_checks, ('unit',))
        p.pr_status(replace(full, failed_checks=None, counts=(('success', 1),)))
        self.assertEqual(p.screen(Runtime(repos=(pr.repo,)), NOW).watching[0].failed_checks, ())


class CommandTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = make_settings()
        self.observer = Observer(Projection(), Runtime(repos=('org/repo',)))
        self.journal = Journal(self.root / 'journal.jsonl', observer=self.observer.feed)
        self.gh = FakeGh()
        self.pr = make_pr()
        self.key = (self.pr.repo, self.pr.number, HEAD)
        self.gh.pr_snapshots[self.key[:2]] = [self.pr]
        self.reviewer = FakeReviewer([Decision('comment', 'private', 'ai', summary='public', why='Not approving yet: CI is pending.')])
        self.runner = cli.Runner(self.settings, self.gh, self.journal, self.reviewer,
                                 observer=self.observer, notifier=Mock(), live_lock_held=True)
        self.addCleanup(setattr, cli, '_plain', True)
        cli.configure_logging(self.root, plain=False)

    def record(self, event, key=None, **fields):
        self.journal.append(kind='pr', key=key or self.key, event=event, mode='live',
                            policy=self.settings.judgement('org/repo').hash, fingerprint=self.pr.fingerprint, **fields)

    def command(self, kind, key=None, head=''):
        self.runner.commands.put(Command('test', kind, key, head))
        self.runner.drain_commands()
        return self.observer.runtime.last_command

    def test_reopen_restart_only_target_no_cached_reuse_model_once(self):
        self.record('assessment', action='comment', reason='cached', source='ai')
        self.record('withheld', action='comment', reason='disabled')
        self.record('nothing')
        other = ('org/repo', 8, HEAD)
        self.record('nothing', other)
        self.assertTrue(self.journal.is_final(self.key, self.settings.judgement('org/repo').hash, 'live', self.pr.fingerprint))
        result = self.command('reopen', self.key, HEAD)
        self.assertEqual(result.text, 're-review queued')
        self.assertFalse(self.journal.is_final(self.key, self.settings.judgement('org/repo').hash, 'live', self.pr.fingerprint))
        restarted = Journal(self.journal.path)
        restarted.replay()
        self.runner.journal = restarted
        self.assertIsNone(self.runner._stored_decision(self.key, self.pr.fingerprint))
        self.assertIn(key_str(self.key), restarted._superseded)
        self.assertTrue(restarted.is_final(other, self.settings.judgement('org/repo').hash, 'live', self.pr.fingerprint))
        self.assertEqual(self.runner.process_pr(*self.key[:2]), 'applied')
        self.assertEqual(self.runner.process_pr(*self.key[:2]), 'final')
        self.assertEqual(self.reviewer.calls, 1)

    def test_duplicate_reopen_during_model_call_preserves_settlement(self):
        self.record('nothing')
        self.assertEqual(self.command('reopen', self.key, HEAD).outcome, 'done')
        self.assertEqual(self.runner.commands.unfinished_tasks, 0)
        self.assertTrue(self.runner.commands.targets(self.key))
        def review(*args):
            self.assertFalse(self.runner.commands.put(Command('duplicate', 'reopen', self.key, HEAD)))
            return Decision('comment', 'private', 'ai', summary='public', why='Not approving yet: CI is pending.')
        self.reviewer.review_pr = Mock(side_effect=review)
        self.assertEqual(self.runner.process_pr(*self.key[:2]), 'applied')
        self.runner.drain_commands()
        self.assertEqual(self.runner.process_pr(*self.key[:2]), 'final')
        self.reviewer.review_pr.assert_called_once()
        self.assertTrue(self.journal.is_final(self.key, self.settings.judgement('org/repo').hash,
                                              'live', self.pr.fingerprint))
        self.assertFalse(self.runner.commands.targets(self.key))
        self.assertTrue(self.runner.commands.put(Command('later', 'reopen', self.key, HEAD)))

    def test_reopen_during_normal_model_call_is_already_queued(self):
        def review(*args):
            self.assertFalse(self.runner.commands.put(Command('duplicate', 'reopen', self.key, HEAD)))
            return Decision('nothing', 'private', 'ai')
        self.reviewer.review_pr = Mock(side_effect=review)
        self.assertEqual(self.runner.process_pr(*self.key[:2]), 'nothing')
        self.runner.drain_commands()
        self.assertEqual(self.runner.process_pr(*self.key[:2]), 'final')
        self.reviewer.review_pr.assert_called_once()

    def test_reopen_preserves_ci_wait_clock_through_replay(self):
        self.journal.append(kind='pr', key=self.key, event='defer', policy='policy', mode='live',
                            reason='checks still running: unit', at='2026-10-10T10:00:00Z')
        self.assertEqual(self.command('reopen', self.key, HEAD).outcome, 'done')
        self.record('defer', reason='checks still running: unit')
        replay = Journal(self.journal.path)
        replay.replay()
        self.assertEqual(replay.defer_reason(self.key), 'checks still running: unit')
        self.assertEqual(replay.defer_since(self.key), '2026-10-10T10:00:00Z')
        self.assertFalse(self.runner.commands.put(Command('duplicate', 'reopen', self.key, HEAD)))

    def test_runner_takeover_and_dismissal_ignore_protected_through_replay(self):
        for state, body in [('COMMENTED', 'manual'), ('DISMISSED', marker_line('fake', 'abcdef', HEAD))]:
            with self.subTest(state=state):
                self.journal = Journal(self.root / f'{state}.jsonl')
                self.runner.journal = self.journal
                pr = replace(self.pr, reviews=(Review('me', state, HEAD, body),))
                self.gh.pr_snapshots[self.key[:2]] = [pr]
                self.assertEqual(self.runner.process_pr(*self.key[:2]), 'ignore')
                import json
                records = [json.loads(line) for line in self.journal.path.read_text().splitlines()]
                self.assertTrue(next(r for r in records if r['event'] == 'ignore')['protected'])
                self.record('reopen')
                replay = Journal(self.journal.path)
                replay.replay()
                self.assertTrue(replay.is_final(self.key, self.settings.judgement('org/repo').hash,
                                               'live', pr.fingerprint))

    def test_refused_forever_unknown_closed_reviewing_and_head_move(self):
        cases = [('satisfied', None, 'forever-final'), ('unknown', None, 'unknown write'),
                 (None, replace(self.pr, state='CLOSED'), 'closed'),
                 (None, replace(self.pr, head_sha='f' * 40), 'head changed'),
                 (None, None, 'already queued')]
        for i, (event, pr, reason) in enumerate(cases):
            with self.subTest(reason=reason):
                self.journal = Journal(self.root / f'case{i}.jsonl')
                self.runner.journal = self.journal
                if event:
                    self.record(event)
                self.gh.pr_snapshots[self.key[:2]] = [pr or self.pr]
                self.runner._reviewing_key = self.key if reason == 'already queued' else None
                before = self.journal.records
                result = self.command('reopen', self.key, HEAD)
                self.assertEqual((result.outcome, result.text), ('refused', f'refused: {reason}'))
                self.assertEqual(self.journal.records, before)
        self.assertEqual(self.reviewer.calls, 0)
        self.assertFalse(self.gh.writes)

    def test_manual_takeover_dismissal_and_approval_are_refused(self):
        for state, body, expected in [('COMMENTED', 'manual', 'owner reviewed'),
                                      ('DISMISSED', marker_line('fake', 'abcdef', HEAD), 'dismissed'),
                                      ('APPROVED', 'manual', 'already approved')]:
            with self.subTest(state=state):
                self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, reviews=(Review('me', state, HEAD, body),))]
                result = self.command('reopen', self.key, HEAD)
                self.assertEqual(result.outcome, 'refused')
                self.assertIn(expected, result.text)
        self.assertEqual(self.journal.records, 0)

    def test_replay_reopen_never_changes_forever_or_unknown(self):
        for index, event in enumerate(('satisfied', 'unknown')):
            key = ('org/repo', index, HEAD)
            self.record(event, key)
            self.record('reopen', key)
        replay = Journal(self.journal.path)
        replay.replay()
        self.assertTrue(replay.is_final(('org/repo', 0, HEAD), '', ''))
        self.assertTrue(replay.is_unknown(('org/repo', 1, HEAD)))

    def test_repeated_action_is_already_and_row_says_so(self):
        body = marker_line('fake', 'abcdef', HEAD)
        self.gh.pr_snapshots[self.key[:2]] = [replace(self.pr, reviews=(Review('me', 'COMMENTED', HEAD, body),))]
        self.record('applied', action='comment')
        self.assertEqual(self.command('reopen', self.key, HEAD).outcome, 'done')
        self.assertEqual(self.runner.process_pr(*self.key[:2]), 'already')
        self.assertFalse(self.gh.writes)
        self.assertEqual(self.reviewer.calls, 1)
        self.assertIn('already exists', self.observer.snapshot.watching[0].why)
        self.assertEqual(self.runner.process_pr(*self.key[:2]), 'final')
        self.assertEqual(self.reviewer.calls, 1)

    def test_probe_results_and_quota_pause_preserved(self):
        engine = Mock(label='fake/test', probe=Mock(return_value=Identity(True)))
        self.reviewer.engine = engine
        self.runner.reviewer_paused_until = 123456
        self.assertEqual(self.command('probe_engine').text, 'engine probe ok')
        self.assertEqual(self.runner.reviewer_paused_until, 123456)
        engine.probe.return_value = Identity(False, detail='expired', fix_command='fake login')
        result = self.command('probe_engine')
        self.assertEqual((result.outcome, result.text), ('failed', 'engine probe failed: expired'))
        self.assertEqual(self.observer.runtime.alerts[0].fix_command, 'fake login')
        self.assertEqual(self.runner.commands.unfinished_tasks, 0)

    def test_check_repo_cache_and_quarantine_recovery(self):
        self.gh.json = Mock(return_value={'archived': False, 'default_branch': 'trunk'})
        self.runner.quarantined = {('pr', 'org/repo', 7), ('pr', 'org/private', 8)}
        self.runner._quarantine_conditions()
        result = self.command('check_repo', ('org/repo',))
        self.assertEqual(result.text, 'access ok · default branch trunk')
        self.assertEqual(dict(self.observer.runtime.repo_checks)['org/repo'], result.text)
        self.assertEqual(self.runner.quarantined, {('pr', 'org/private', 8)})
        self.assertFalse(any(a.repo == 'org/repo' for a in self.observer.runtime.alerts))
        self.gh.json.return_value = {'archived': True, 'default_branch': 'trunk'}
        self.assertEqual(self.command('check_repo', ('org/repo',)).text, 'archived')
        self.gh.json.side_effect = GhError(['api'], 1, 'HTTP 404')
        self.assertEqual(self.command('check_repo', ('org/repo',)).text, 'no access')
        self.assertEqual(dict(self.observer.runtime.repo_checks)['org/repo'], 'no access')
        self.gh.json.side_effect = GhError(['api'], 1, 'HTTP 429')
        self.assertIn('check_repo failed:', self.command('check_repo', ('org/repo',)).text)

    def test_coalesces_queued_and_running_commands_and_keeps_results(self):
        inbox = self.runner.commands
        for kind, key in [('probe_engine', None), ('reopen', self.key)]:
            first = Command('a', kind, key, HEAD)
            self.assertTrue(inbox.put(first))
            self.assertFalse(inbox.put(Command('b', kind, key, HEAD)))
            self.assertEqual(inbox.get_nowait(), first)
            self.assertFalse(inbox.put(Command('c', kind, key, HEAD)))
            inbox.complete(first)
        self.gh.json = Mock(return_value={'archived': True})
        inbox.put(Command('one', 'check_repo', ('org/repo',)))
        inbox.put(Command('two', 'wake'))
        self.runner.drain_commands()
        for _ in range(100):
            self.observer('phase', phase='checking')
        self.assertEqual([r.id for r in self.observer.snapshot.runtime.command_results], ['one', 'two'])


@unittest.skipUnless(HAS_UI, "optional UI dependencies are not installed")
class DetailPilots(IsolatedAsyncioTestCase):
    def app(self, snapshot=None, **kwargs):
        snapshot = snapshot or fixture_screen()
        observer = Observer(Projection(), snapshot.runtime)
        observer.snapshot = snapshot
        worker = Mock(runner=Mock(cancelled=False, commands=CommandInbox(threading.Event())),
                      poll_join=Mock(return_value=False))
        return TextualApp(observer, worker, opener=Mock(), folder_opener=Mock(), clipboard=Mock(), **kwargs)

    async def test_fixture_detail_title_state_body_timeline_and_back(self):
        app = self.app()
        async with app.run_test(size=(110, 32)) as pilot:
            app.cursor = next(i for i, (section, row) in enumerate(app.rows)
                              if section == 'watching' and row.item.number == 7)
            await pilot.press('enter')
            self.assertIsInstance(app.screen, PRDetail)
            output = contents(app)
            for expected in ('example/widget#7', 'approved', 'Posted', 'Please update the description.',
                             'Timeline', 'no assessment on this head', 'next step final'):
                self.assertIn(expected, output)
            await pilot.press('A')
            self.assertIn('refused: dry-run mode', contents(app))
            await pilot.press('escape', 'n', 'enter')
            self.assertIn('Retry transient failures', contents(app))
            self.assertIn('Timeline', contents(app))

    async def test_session_worker_reads_missing_files_and_confines_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / 'sessions' / 'test'
            session.mkdir(parents=True)
            (session / 'prompt.md').write_text('[@click=app.quit] prompt\x1b[2J')
            (session / 'stdout.json').write_text('{"answer": "synthetic"}')
            p = Projection()
            p.feed(dict(kind='pr', key=['org/repo', 7, HEAD], event='nothing', reason='private',
                        title='Synthetic title', session=str(session), at='2026-10-10T10:00:00Z'))
            snapshot = p.screen(Runtime(repos=('org/repo',), state_dir=str(root)), NOW)
            app = self.app(snapshot)
            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.press('enter', 'p')
                await app.workers.wait_for_complete()
                output = contents(app)
                self.assertIn('[@click=app.quit] prompt', output)
                self.assertNotIn('\x1b', output)
                self.assertIn('"synthetic"', output)
                await pilot.press('s')
                await app.workers.wait_for_complete()
                app.folder_opener.assert_called_once_with(session.resolve())
                (session / 'stdout.json').unlink()
                await pilot.press('p', 'p')
                await app.workers.wait_for_complete()
                self.assertIn('stdout.json (read-only)\nunavailable', contents(app))
                outside = replace(snapshot.detail(('org/repo', 7, HEAD))[0], session=str(root.parent))
                detail = replace(snapshot.details[('org/repo', 7, HEAD)], latest_session=str(root.parent))
                app.observer.snapshot = replace(snapshot, events={('org/repo', 7, HEAD): (outside,)},
                                                details={('org/repo', 7, HEAD): detail})
                await pilot.press('s')
                await app.workers.wait_for_complete()
                self.assertIn('outside the state directory', contents(app))
                self.assertEqual(app.folder_opener.call_count, 1)
            link = root / 'escape'
            link.symlink_to(root.parent, target_is_directory=True)
            self.assertIsNone(session_path(str(root), str(link)))

    async def test_confirmation_cancels_or_captures_original_head(self):
        app = self.app()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press('n', 'enter', 'R')
            self.assertIsInstance(app.screen, ConfirmReopen)
            await pilot.press('escape')
            self.assertTrue(app.worker.runner.commands.empty())
            await pilot.press('R')
            self.assertIsInstance(app.screen, ConfirmReopen)
            snapshot = app.observer.snapshot
            app.observer.snapshot = replace(snapshot, watching=tuple(
                replace(row, item=replace(row.item, head='b' * 40)) if row.item.number == 1 else row
                for row in snapshot.watching))
            app.refresh_snapshot()  # the Runner can publish while the modal remains open
            await pilot.press('enter')
            command = app.worker.runner.commands.get_nowait()
            self.assertEqual((command.kind, command.key, command.head),
                             ('reopen', ('example/widget', 1, 'aaaaaaa'), 'aaaaaaa'))
            self.assertIsInstance(app.screen, PRDetail)

    async def test_input_focus_disables_detail_and_global_actions(self):
        app = self.app()
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.press('n', 'enter')
            detail = app.screen
            field = Input()
            await detail.mount(field)
            field.focus()
            await pilot.press('o', 's', 'p', 'R', 'A', 'q', 'r', 't', 'c', 'escape')
            self.assertIs(app.screen, detail)
            self.assertIs(app.focused, field)
            self.assertIn('ospRAqrtc', field.value)
            self.assertTrue(app.worker.runner.commands.empty())
            app.worker.request_stop.assert_not_called()
            app.opener.assert_not_called()
            app.folder_opener.assert_not_called()
            app.copy_command.assert_not_called()

    async def test_alert_expansion_dispatch_and_copy_only_with_command(self):
        alerts = (Alert('engine_identity', 'error', 'expired', 90, fix_command='fake login'),
                  Alert('reviewer_paused', 'warning', 'quota', 90),
                  Alert('gh_identity', 'error', 'wrong account', 90),
                  Alert('quarantined:org/repo', 'warning', 'no access', 90, repo='org/repo', scope='org/repo'),
                  Alert('unknown_write', 'error', 'write uncertain', 90))
        snapshot = fixture_screen()
        affected = snapshot.watching[0].item
        snapshot = replace(snapshot, alerts=alerts, unknown_items=(affected,))
        app = self.app(snapshot)
        async with app.run_test(size=(110, 40)) as pilot:
            for index, expected in enumerate(('probe_engine', 'probe_engine', 'wake', 'check_repo', None)):
                app.cursor = index
                await pilot.press('enter')
                output = contents(app)
                for label in ('cause:', 'scope:', 'since', 'last attempt and result:', 'action:'):
                    self.assertIn(label, output)
                self.assertGreater(app.query_one('#alerts', DataTable).ordered_rows[index].height, 1)
                await pilot.press('t', 'c')
                await app.workers.wait_for_complete()
                if expected:
                    command = app.worker.runner.commands.get_nowait()
                    self.assertEqual(command.kind, expected)
                    if expected == 'check_repo':
                        self.assertEqual(command.key, ('org/repo',))
                    app.worker.runner.commands.complete(command)
                else:
                    self.assertTrue(app.worker.runner.commands.empty())
                    self.assertIn('reconciled automatically when GitHub confirms; otherwise left to you', contents(app))
                    self.assertIn(f'affected: {affected.repo}#{affected.number}', contents(app))
            self.assertEqual([c.args[0] for c in app.copy_command.call_args_list], ['fake login'])

    async def test_all_body_labels_and_latest_private_reason(self):
        for event, expected in [('applied', 'Posted'), ('would_comment', 'Dry-run'),
                                ('withheld', 'Withheld'), ('unknown', 'Outcome unknown'),
                                ('assessment', 'nothing posted')]:
            with self.subTest(event=event):
                p = Projection()
                record = dict(kind='pr', key=['org/repo', 7, HEAD], action='comment',
                              title='Synthetic', at='2026-10-10T10:00:00Z')
                if event != 'assessment':
                    p.feed(dict(record, event='assessment', reason='model reason'))
                p.feed(dict(record, event=event, summary='public', body='body text', reason='model reason'))
                p.feed(dict(record, event='notify', text='notice'))
                app = self.app(p.screen(Runtime(repos=('org/repo',)), NOW))
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.press('enter')
                    output = contents(app)
                    self.assertIn(expected, output)
                    if event in ('applied', 'would_comment', 'unknown'):
                        self.assertIn('body text', output)
                    else:
                        self.assertNotIn('body text', output)
                    self.assertIn('model reason', output)
                    self.assertIn('private · journal only', output)
                    self.assertIn('Timeline', contents(app))

    async def test_resolved_outcomes_and_sticky_body_after_eviction(self):
        for event, label in [('applied', 'Posted'), ('satisfied', 'Posted')]:
            p = Projection()
            rec = dict(kind='pr', key=['org/repo', 7, HEAD], at='2026-10-10T10:00:00Z')
            p.feed(dict(rec, event='unknown', body='attempted body', reason='private detail', summary='public summary'))
            p.feed(dict(rec, event=event, action='comment', source='observed'))
            for _ in range(25):
                p.feed(dict(rec, event='notify', text='notice'))
            app = self.app(p.screen(Runtime(repos=('org/repo',)), NOW))
            async with app.run_test() as pilot:
                await pilot.press('enter')
                self.assertIn(label, contents(app))
                self.assertIn('attempted body', contents(app))
                self.assertIn('no assessment on this head', contents(app))
                self.assertNotIn('Outcome unknown', contents(app))

    async def test_next_queued_only_for_this_pr_and_paused_only_for_reviewer(self):
        p = Projection()
        rec = dict(kind='pr', key=['org/repo', 7, HEAD], at='2026-10-10T10:00:00Z')
        p.feed(dict(rec, event='withheld', action='comment', reason='private', summary='public'))
        app = self.app(p.screen(Runtime(repos=('org/repo',)), NOW))
        async with app.run_test() as pilot:
            await pilot.press('enter')
            inbox = app.worker.runner.commands
            inbox.put(Command('other', 'reopen', ('org/repo', 8, HEAD), HEAD))
            app.refresh_snapshot()
            self.assertIn('next step needs you', contents(app))
            self.assertNotIn('next step queued', contents(app))
            self.assertNotIn('next step paused', contents(app))
            inbox.put(Command('this', 'reopen', tuple(rec['key']), HEAD))
            app.refresh_snapshot()
            self.assertIn('next step queued', contents(app))
            snapshot = app.observer.snapshot
            app.observer.snapshot = replace(snapshot, runtime=replace(snapshot.runtime,
                                                                       reviewer_paused_until=app.clock() + 60))
            app.refresh_snapshot()
            self.assertIn('next step paused', contents(app))

    async def test_session_bounded_reads_render_and_resolution_run_in_worker(self):
        from lightkeeper.ui import detail as module
        import io
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / 'session'
            session.mkdir()
            p = Projection()
            p.feed(dict(kind='pr', key=['org/repo', 7, HEAD], event='nothing', session=str(session),
                        at='2026-10-10T10:00:00Z'))
            app = self.app(p.screen(Runtime(repos=('org/repo',), state_dir=str(root)), NOW))
            app_thread = threading.get_ident()
            streams, threads, delivered = [], [], []
            def resolve(*args):
                threads.append(threading.get_ident())
                return session_path(*args)
            def open_file(*args, **kwargs):
                threads.append(threading.get_ident())
                stream = io.BytesIO(b'x' * (64 * 1024) + b'UNREAD TAIL')
                stream.read = Mock(wraps=stream.read)
                streams.append(stream)
                return stream
            async with app.run_test() as pilot:
                await pilot.press('enter')
                detail = app.screen
                show = detail.show_session
                def show_session(generation, value):
                    delivered.append(value)
                    show(generation, value)
                with patch.object(module, 'session_path', side_effect=resolve), \
                     patch.object(Path, 'open', side_effect=open_file), \
                     patch.object(detail, 'show_session', side_effect=show_session):
                    await pilot.press('p', 's')
                    await app.workers.wait_for_complete()
                self.assertEqual(len(streams), 2)
                for stream in streams:
                    stream.read.assert_called_once_with(64 * 1024)
                self.assertTrue(all(thread != app_thread for thread in threads))
                self.assertIsInstance(delivered[0], Text)
                self.assertEqual(delivered[0].plain.count('… truncated, open with s'), 2)
                self.assertNotIn('UNREAD TAIL', delivered[0].plain)
                app.folder_opener.assert_called_once_with(session.resolve())

    async def test_refresh_safe_before_mount_after_unmount_and_missing_widgets(self):
        app = self.app()
        app.refresh_snapshot()
        async with app.run_test():
            timer = app._refresh_timer
            with patch.object(timer, 'stop', wraps=timer.stop) as stop:
                app.on_unmount()
                stop.assert_called_once()
            await app.query_one('#footer').remove()
            with patch.object(app, 'rebuild_rows', side_effect=RuntimeError('synthetic render failure')):
                app._last_snapshot = None
                app.refresh_snapshot()  # Missing error footer must not raise.
            await app.query_one('#header').remove()
            app.refresh_snapshot()
        app.refresh_snapshot()

    async def test_detail_ci_and_refusal_while_reviewing(self):
        pr = make_pr(is_draft=True, checks=(Check('unit', 'completed', 'failure'),))
        p = Projection()
        p.feed(dict(kind='pr', key=['org/repo', 7, HEAD], event='seen', at='2026-10-10T10:00:00Z'))
        p.pr_status(PRStatus.from_pr(pr))
        snapshot = p.screen(Runtime(repos=('org/repo',)), NOW)
        app = self.app(snapshot)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press('enter')
            for expected in ('author alice', 'base main · draft', '1 failed', '1 failed: unit'):
                self.assertIn(expected, contents(app))
            row = replace(snapshot.watching[0], state='reviewing')
            app.observer.snapshot = replace(snapshot, watching=(row,))
            app.refresh_snapshot()
            await pilot.press('R')
            self.assertIsInstance(app.screen, PRDetail)
            self.assertTrue(app.worker.runner.commands.empty())
            self.assertIn('refused: already queued', contents(app))

    async def test_narrow_detail_and_untrusted_body_stay_literal(self):
        p = Projection()
        p.feed(dict(kind='pr', key=['org/repo', 7, HEAD], event='would_comment',
                    summary='public', body='[@click=app.quit] body\x1b[2J', title='[bold]literal[/]',
                    at='2026-10-10T10:00:00Z'))
        app = self.app(p.screen(Runtime(repos=('org/repo',)), NOW))
        async with app.run_test(size=(45, 20)) as pilot:
            await pilot.press('enter')
            self.assertIn('Dry-run', contents(app))
            self.assertIn('[@click=app.quit] body', contents(app))
            self.assertNotIn('\x1b', contents(app))
            app.worker.request_stop.assert_not_called()
