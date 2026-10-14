"""Release checks use the existing gh client and never affect review outcomes."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
from unittest import TestCase
from unittest.mock import Mock, patch

from lightkeeper import __main__ as cli
from lightkeeper.commands import Command
from lightkeeper.github import GhError, GhTimeout
from lightkeeper.journal import Journal
from lightkeeper.updates import CHECK_INTERVAL, UPDATE_REPO, UpdateChecker, newer
from tests.helpers import make_settings


class UpdateTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = 100000
        self.gh = Mock()
        self.gh.json.return_value = {'tag_name': 'v0.2.0'}
        self.checker = UpdateChecker(self.root, self.gh, lambda: self.now)

    def test_numeric_tags_and_zero_padding(self):
        for tag, current, expected in [('v0.2.0', '0.1.0', True), ('1.10', 'v1.9', True),
                                       ('1.2', '1.2.0', False), ('1.2.0.1', '1.2', True),
                                       ('0.1.0', '0.1.0', False), ('0.0.9', '0.1.0', False)]:
            self.assertEqual(newer(tag, current), expected)
        for tag in ('v1.beta', '1.2-rc1', '', 'vv1.2', '1..2', 12, None):
            with self.assertLogs('lightkeeper', level='DEBUG'):
                self.assertIsNone(newer(tag))
        with self.assertLogs('lightkeeper', level='DEBUG'):
            self.assertIsNone(newer('v1.2', 'dev-build'))

    def test_non_numeric_installed_version_skips_cached_and_fresh_comparisons(self):
        for force in (False, True):
            with patch('lightkeeper.updates.__version__', '0.1.dev1'), self.assertLogs('lightkeeper', level='DEBUG'):
                self.assertEqual(self.checker.check(force=force), 'no release information available')
            self.assertEqual(self.checker.available, '')

    def test_checks_require_repositories_and_successful_identity(self):
        runner = cli.Runner(make_settings(), self.gh, Journal(self.root / 'journal.jsonl'))
        runner.check_updates(force=True)
        self.gh.json.assert_not_called()
        for login in ('someone-else', OSError('offline')):
            self.gh.login.side_effect = login if isinstance(login, Exception) else None
            self.gh.login.return_value = login
            with patch.object(runner, '_probe_engine'), patch.object(cli, 'log'):
                runner.tick()
                runner.check_updates(force=True)
            self.gh.json.assert_not_called()
        runner.settings = replace(runner.settings, repos={})
        runner._identity_checked = True
        runner.check_updates(force=True)
        self.gh.json.assert_not_called()

    def test_unexpected_update_failure_uses_tick_error_boundary(self):
        runner = cli.Runner(make_settings(), self.gh, Journal(self.root / 'journal.jsonl'))
        self.gh.login.return_value = runner.settings.login
        with patch.object(runner, '_probe_engine'), patch.object(runner.updates, 'check', side_effect=RuntimeError('broken')):
            with self.assertRaisesRegex(RuntimeError, 'broken'):
                runner.tick()
        self.assertEqual(runner._tick_failures, 1)
        self.assertFalse(runner._in_tick)

    def test_throttle_survives_restart_and_expires_at_24_hours(self):
        self.assertIn('update 0.2.0 available', self.checker.check())
        self.gh.json.assert_called_once_with(['api', f'repos/{UPDATE_REPO}/releases/latest'], timeout_s=10)
        record = json.loads((self.root / 'updates.json').read_text())
        self.assertEqual(record, {'checked_at': self.now, 'latest_tag': 'v0.2.0'})
        self.now += CHECK_INTERVAL - 1
        restarted = UpdateChecker(self.root, self.gh, lambda: self.now)
        self.assertIn('update 0.2.0 available', restarted.check())
        self.assertEqual(self.gh.json.call_count, 1)
        self.now += 1
        restarted.check()
        self.assertEqual(self.gh.json.call_count, 2)
        restarted.check(force=True)
        self.assertEqual(self.gh.json.call_count, 3)

    def test_disabled_including_manual_check_touches_neither_disk_nor_github(self):
        for force in (False, True):
            self.assertEqual(self.checker.check(enabled=False, force=force), 'update checks disabled')
        self.gh.json.assert_not_called()
        self.assertFalse((self.root / 'updates.json').exists())

    def test_failures_are_debug_only_and_throttled(self):
        for error in (GhError(['api'], 1, 'HTTP 404'), GhTimeout(['api'], 20),
                      OSError('offline'), ValueError('bad JSON')):
            with self.subTest(error=error):
                self.checker = UpdateChecker(self.root, self.gh, lambda: self.now)
                self.gh.json.side_effect = error
                with self.assertLogs('lightkeeper', level='DEBUG') as logs:
                    self.assertIn('unavailable', self.checker.check(force=True))
                    self.checker.check()
                self.assertEqual(len(logs.records), 1)
                self.assertEqual(logs.records[0].levelname, 'DEBUG')
        self.gh.json.side_effect = None
        self.gh.json.return_value = {'tag_name': 'release-candidate'}
        with self.assertLogs('lightkeeper', level='DEBUG'):
            self.assertIn('unavailable', self.checker.check(force=True))

    def test_corrupt_cache_and_unwritable_cache_do_not_escape(self):
        (self.root / 'updates.json').write_text('not json')
        with self.assertLogs('lightkeeper', level='DEBUG'):
            self.assertIn('update 0.2.0 available', self.checker.check())
        self.assertEqual(self.checker.available, '0.2.0')
        with patch('lightkeeper.updates.os.replace', side_effect=OSError('read only')), \
             self.assertLogs('lightkeeper', level='DEBUG'):
            self.assertIn('update 0.2.0 available', self.checker.check(force=True))
        self.assertEqual(list(self.root.glob('.updates-*')), [])

    def test_runner_tick_and_command_check_off_ui_thread_without_journal_or_notice(self):
        settings = make_settings()
        observer, notifier = Mock(), Mock()
        runner = cli.Runner(settings, self.gh, Journal(self.root / 'journal.jsonl'),
                            observer=observer, notifier=notifier)
        self.gh.login.return_value = settings.login
        thread_names = []
        self.gh.json.side_effect = lambda args, **kwargs: thread_names.append(threading.current_thread().name) or {'tag_name': 'v0.2.0'}
        with patch.object(runner, '_tick', return_value={}), patch.object(runner, '_probe_engine'), \
             patch.object(cli, 'log') as log:
            worker = threading.Thread(target=runner.tick, name='runner')
            worker.start()
            worker.join()
            runner.commands.put_nowait(Command('update', 'check_updates'))
            worker = threading.Thread(target=runner.drain_commands, name='runner')
            worker.start()
            worker.join()
            reminders = [call for call in log.call_args_list if str(call.args[0]).startswith('update ')]
            self.assertEqual(len(reminders), 1)
        self.assertEqual(thread_names, ['runner', 'runner'])
        observer.assert_any_call('policy', update_version='0.2.0')
        self.assertFalse(runner.journal.path.exists())
        notifier.assert_not_called()
        from lightkeeper.settings import save_settings, settings_digest
        save_settings(settings.config_dir, {'screen': {'updates': False}}, settings_digest(settings.config_dir))
        self.assertEqual(runner.check_updates(force=True), 'update checks disabled')
        observer.assert_any_call('policy', update_version='')

    def test_once_loop_checks_and_failure_does_not_skip_tick(self):
        from types import SimpleNamespace
        settings = make_settings()
        runner = cli.Runner(settings, self.gh, Journal(self.root / 'journal.jsonl'))
        self.gh.login.return_value = settings.login
        self.gh.json.side_effect = GhError(['api'], 1, 'HTTP 404')
        with patch.object(runner, '_tick', return_value={}) as tick, \
             patch.object(runner, '_probe_engine'), \
             patch.object(cli, 'log'), self.assertLogs('lightkeeper', level='DEBUG'):
            self.assertEqual(cli._loop(SimpleNamespace(once=True), settings, runner, runner.stop), 0)
        self.gh.json.assert_called_once()
        tick.assert_called_once()

    def test_manual_checks_coalesce_and_current_version_has_no_reminder(self):
        from lightkeeper.commands import CommandInbox
        inbox = CommandInbox(threading.Event())
        self.assertTrue(inbox.put(Command('one', 'check_updates')))
        self.assertFalse(inbox.put(Command('two', 'check_updates')))
        command = inbox.get_nowait()
        inbox.complete(command)
        self.assertTrue(inbox.put(Command('three', 'check_updates')))
        self.gh.json.return_value = {'tag_name': 'v0.1.0'}
        self.assertEqual(self.checker.check(), 'lightkeeper is up to date')
        self.assertEqual(self.checker.available, '')
