"""Settings saves, editor isolation and Textual forms with synthetic services."""
from contextlib import nullcontext
from dataclasses import replace
import hashlib
import fcntl
import os
import queue
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
    from textual.app import SuspendNotSupported
    from textual.worker import WorkerCancelled
    from textual.widgets import Button, DataTable, Input, Select
    from lightkeeper.ui.agent import AgentPanel
    from lightkeeper.ui.repos import Confirm, Repositories, RepoForm, read_settings, SettingsCache
    from lightkeeper.ui.textual_app import TextualApp
    from tests.test_textual import contents

from lightkeeper.commands import Command, CommandInbox
from lightkeeper.engines.base import Identity
from lightkeeper.settings import (SettingsError, load_settings, save_settings, settings_digest,
                                         validate_settings_file, validate_template)
from lightkeeper.ui.editing import edit_file, nonempty_utf8
from lightkeeper.ui.project import Observer, Projection
from tests.test_ui import fixture_screen

SETTINGS = '[identity]\nlogin = "reviewer"\n[agent]\nprofile = "profiles/test"\n[repos."example/widget"]\npost = "live"\n'


class SaveTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.file = self.root / 'settings.toml'
        self.file.write_text(SETTINGS)
        (self.root / "rules.md").write_text("Review carefully.", encoding="utf-8")
        self.digest = settings_digest(self.root)

    def test_happy_path_mode_profile_digest_and_round_trip(self):
        result = save_settings(self.root, {'repos': {'example/widget': {'remarks': 'all'}}}, self.digest)
        self.assertEqual(result.settings, load_settings(self.root))
        self.assertEqual(result.settings.agent.profile, 'profiles/test')
        self.assertEqual(result.settings.resolved_profile, str(self.root / 'profiles/test'))
        self.assertEqual(result.digest, hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.file.read_text().startswith('# written by lightkeeper; comments are not kept\n'))
        self.assertEqual(result.settings.repo('example/widget').remarks, 'all')
        self.assertEqual(set(self.root.iterdir()), {self.file, self.root / ".write-lock", self.root / "rules.md"})
        self.assertEqual((self.root / ".write-lock").stat().st_mode & 0o777, 0o600)

    def test_add_remove_agent_comment_and_exactly_one_patch(self):
        for change in ({'repos': {'example/new': {}}}, {'repos': {'example/widget': None}},
                       {'agent': {'effort': 'high'}}, {'comment': {'disclose': False}}):
            save_settings(self.root, change, settings_digest(self.root))
        settings = load_settings(self.root)
        self.assertEqual(list(settings.repos), ['example/new'])
        self.assertEqual(settings.repo('example/new').post, 'dry-run')
        self.assertEqual(settings.agent.effort, 'high')
        self.assertFalse(settings.comment.disclose)
        for bad in ({}, {'agent': {'effort': 'high', 'model': 'test'}}, {'identity': {'login': 'other'}},
                    {'repos': {'example/a': {}, 'example/b': {}}}, {'agent': {}, 'comment': {}}):
            with self.subTest(bad=bad), self.assertRaises(SettingsError):
                save_settings(self.root, bad, settings_digest(self.root))

    def test_conflict_refuses_stale_and_concurrent_validation(self):
        self.file.write_text(SETTINGS + '# changed\n')
        before = self.file.read_bytes()
        with self.assertRaises(SettingsError) as caught:
            save_settings(self.root, {'agent': {'model': 'test'}}, self.digest)
        self.assertEqual((caught.exception.file, caught.exception.key), (Path('settings.toml'), 'conflict'))
        self.assertEqual(self.file.read_bytes(), before)
        original_validate = validate_settings_file
        def concurrent(candidate, root):
            result = original_validate(candidate, root)
            self.file.write_text(SETTINGS + '# editor saved\n')
            return result
        with patch('lightkeeper.settings.validate_settings_file', concurrent), self.assertRaisesRegex(SettingsError, 'conflict'):
            save_settings(self.root, {'agent': {'model': 'test'}}, settings_digest(self.root))
        self.assertTrue(self.file.read_text().endswith('# editor saved\n'))

    def test_fresh_empty_login_invalid_candidates_preserve_bytes(self):
        self.file.unlink()
        with self.assertRaises(SettingsError):
            save_settings(self.root, {'agent': {'model': 'test'}}, settings_digest(self.root))
        self.assertFalse(self.file.exists())
        self.file.write_text('[identity]\nlogin = ""')
        before = self.file.read_bytes()
        with self.assertRaises(SettingsError):
            save_settings(self.root, {'agent': {'model': 'test'}}, settings_digest(self.root))
        self.assertEqual(self.file.read_bytes(), before)
        self.file.write_text(SETTINGS)
        for bad in ({'repos': {'example/widget': {'post': 'wrong'}}}, {'agent': {'timeout_s': -1}},
                    {'repos': {'../outside': {}}}, {'repos': {'example/widget': {'name': 'other/repo'}}}):
            with self.subTest(bad=bad), self.assertRaises(SettingsError):
                save_settings(self.root, bad, self.digest)
            self.assertEqual(self.file.read_text(), SETTINGS)

    def test_template_placeholder_caught(self):
        (self.root / 'template.md').write_text('{private_reason}')
        with self.assertRaisesRegex(SettingsError, 'unknown placeholder'):
            save_settings(self.root, {'agent': {'model': 'test'}}, self.digest)
        self.assertEqual(self.file.read_text(), SETTINGS)

    @unittest.skipUnless(HAS_UI, "Textual settings cache")
    def test_read_is_side_effect_free_and_cache_skips_text_and_unchanged_settings(self):
        rules = self.root / 'rules.md'
        rules.write_text('rules')
        rules.chmod(0o640)
        self.root.chmod(0o750)
        self.file.chmod(0o640)
        read_settings(self.root)
        self.assertEqual(rules.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o750)
        cache = SettingsCache()
        with patch('lightkeeper.ui.repos.load_settings', wraps=load_settings) as load:
            for _ in range(4):
                cache.read(self.root)
            self.assertEqual(load.call_count, 1)
            load.assert_called_with(self.root, include_text=False)
            self.file.write_text(SETTINGS + '# changed')
            cache.read(self.root)
            self.assertEqual(load.call_count, 2)
            stamp = self.file.stat()
            self.file.write_text(SETTINGS + '# altered')
            os.utime(self.file, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            cache.read(self.root)
            self.assertEqual(load.call_count, 3)
            os.utime(self.file, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000))
            cache.read(self.root)
            self.assertEqual(load.call_count, 4)
        self.assertEqual(cache.value[0].notes, {})

    def test_settings_symlink_is_refused(self):
        target = self.root / 'target.toml'
        self.file.rename(target)
        self.file.symlink_to(target)
        with self.assertRaisesRegex(SettingsError, 'symlinked'):
            save_settings(self.root, {'agent': {'effort': 'high'}}, self.digest)
        self.assertTrue(self.file.is_symlink())
        self.assertEqual(target.read_text(), SETTINGS)

    def test_two_app_writers_share_final_comparison_lock(self):
        # Both candidates validate from the same original; only one may replace it.
        for editor_writer in (False, True):
            with self.subTest(editor_writer=editor_writer):
                self.file.write_text(SETTINGS)
                barrier = threading.Barrier(2)
                errors = []
                successes = []
                def validate(path, root):
                    result = validate_settings_file(path, root)
                    barrier.wait(timeout=3)
                    return result
                def save():
                    try:
                        save_settings(self.root, {'agent': {'effort': 'high'}}, self.digest)
                        successes.append('form')
                    except SettingsError as exc:
                        errors.append(str(exc))
                def other():
                    if editor_writer:
                        app = Mock(suspend=Mock(side_effect=nullcontext),
                                   call_from_thread=Mock(side_effect=lambda fn: fn()))
                        error = edit_file(app, self.file, lambda path: validate(path, self.root))
                        (errors if error else successes).append(error or 'editor')
                    else:
                        save()
                def editor(args):
                    Path(args[-1]).write_text(SETTINGS + '# edited')
                    return 0
                real_replace = os.replace
                def locked_replace(source, destination):
                    with (self.root / '.write-lock').open('rb') as lock:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    real_replace(source, destination)
                with patch('lightkeeper.settings.validate_settings_file', validate), \
                     patch('lightkeeper.settings.os.replace', locked_replace), \
                     patch('lightkeeper.ui.editing.subprocess.call', editor):
                    threads = [threading.Thread(target=save), threading.Thread(target=other)]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(5)
                        self.assertFalse(thread.is_alive())
                self.assertEqual(len(successes), 1)
                self.assertEqual(len(errors), 1)
                self.assertIn('changed on disk', errors[0])



class EditorTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.live = self.root / 'rules.md'
        self.live.write_text('original')
        self.live.chmod(0o640)
        self.editor = self.root / 'editor'
        self.app = Mock(suspend=Mock(side_effect=nullcontext))

    def run_editor(self, content, code=0, validate=nonempty_utf8):
        # The script asserts that the live file still contains the original during editing.
        self.editor.write_text('#!/bin/sh\n[ "$(cat "$LIVE_TEST_FILE")" = original ] || exit 99\n'
                               '[ "$1" != "$LIVE_TEST_FILE" ] || exit 98\n'
                               f"printf '%s' '{content}' > \"$1\"\nexit {code}\n")
        self.editor.chmod(0o700)
        with patch.dict(os.environ, {'VISUAL': str(self.editor), 'EDITOR': '/does/not/exist',
                                     'LIVE_TEST_FILE': str(self.live)}):
            return edit_file(self.app, self.live, validate)

    def test_valid_replaces_enforces_mode_and_visual_precedence(self):
        self.assertIsNone(self.run_editor('edited'))
        self.assertEqual(self.live.read_text(), 'edited')
        self.assertEqual(self.live.stat().st_mode & 0o777, 0o600)
        self.app.suspend.assert_called_once()
        self.assertEqual(set(self.root.iterdir()), {self.live, self.editor, self.root / ".write-lock"})

    def test_missing_editor_reports_without_keeping_a_copy(self):
        with patch.dict(os.environ, {'VISUAL': '/does/not/exist', 'EDITOR': ''}):
            error = edit_file(self.app, self.live, nonempty_utf8)
        self.assertIn('editor unavailable', error)
        self.assertNotIn('copy kept', error)
        self.assertEqual(self.live.read_text(), 'original')
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith('.rules')], [])

    def test_invalid_or_nonzero_keeps_original(self):
        for content, code in (('', 0), ('edited', 1)):
            with self.subTest(code=code):
                self.assertTrue(self.run_editor(content, code))
                self.assertEqual(self.live.read_text(), 'original')

    def test_settings_and_template_validation_and_editor_conflict(self):
        self.assertTrue(self.run_editor('invalid toml', validate=lambda p: validate_settings_file(p, self.root)))
        self.assertEqual(self.live.read_text(), 'original')
        self.assertTrue(self.run_editor('{unknown}', validate=lambda p: validate_template(p.read_text())))
        def concurrent(path):
            self.live.write_text('external change')
        self.assertIn('changed on disk', self.run_editor('edited', validate=concurrent))
        self.assertEqual(self.live.read_text(), 'external change')

    def test_missing_file_editor_uses_empty_copy_and_utf8_validation(self):
        self.live.unlink()
        def editor(args):
            self.assertFalse(self.live.exists())
            self.assertEqual(Path(args[-1]).read_bytes(), b'')
            Path(args[-1]).write_text('notes')
            return 0
        with patch('lightkeeper.ui.editing.subprocess.call', editor):
            self.assertIsNone(edit_file(self.app, self.live, nonempty_utf8))
        self.assertEqual(self.live.stat().st_mode & 0o777, 0o600)
        self.live.write_bytes(b'\xff')
        with self.assertRaises(UnicodeError):
            nonempty_utf8(self.live)

    def test_editor_arguments_and_vi_fallback(self):
        with patch.dict(os.environ, {}, clear=True), patch('lightkeeper.ui.editing.subprocess.call', return_value=0) as call:
            self.assertIsNone(edit_file(self.app, self.live, nonempty_utf8))
            self.assertEqual(call.call_args.args[0][0], 'vi')
            os.environ['EDITOR'] = 'test-editor --wait'
            self.assertIsNone(edit_file(self.app, self.live, nonempty_utf8))
            self.assertEqual(call.call_args.args[0][:2], ['test-editor', '--wait'])

    def test_runner_thread_can_progress_while_editor_is_suspended(self):
        entered, progressed = threading.Event(), threading.Event()
        def runner():
            if entered.wait(2):
                progressed.set()
        thread = threading.Thread(target=runner)
        thread.start()
        def editor(args):
            entered.set()
            self.assertTrue(progressed.wait(2))
            Path(args[-1]).write_text('updated')
            self.assertEqual(self.live.read_text(), 'original')
            return 0
        try:
            with patch('lightkeeper.ui.editing.subprocess.call', editor):
                self.assertIsNone(edit_file(self.app, self.live, nonempty_utf8))
        finally:
            entered.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_worker_editor_launch_hops_to_main_thread(self):
        calls = queue.Queue()
        completed = threading.Event()
        outcome = []
        main = threading.get_ident()

        def hop(callback):
            self.assertNotEqual(threading.get_ident(), main)
            calls.put(callback)
            if not completed.wait(3):
                raise RuntimeError('main-thread callback timed out')
            return 0

        def editor(args):
            self.assertEqual(threading.get_ident(), main)
            Path(args[-1]).write_text('updated', encoding='utf-8')
            return 0

        self.app.call_from_thread.side_effect = hop
        self.app.suspend.side_effect = lambda: (
            self.assertEqual(threading.get_ident(), main) or nullcontext())
        with patch('lightkeeper.ui.editing.subprocess.call', editor):
            worker = threading.Thread(target=lambda: outcome.append(edit_file(self.app, self.live, nonempty_utf8)))
            worker.start()
            try:
                calls.get(timeout=3)()
            finally:
                completed.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, [None])
        self.app.call_from_thread.assert_called_once()
        self.assertEqual(self.live.read_text(encoding='utf-8'), 'updated')

    def test_explicit_config_named_repos_uses_same_lock_as_form_saves(self):
        config = self.root / 'repos'
        config.mkdir()
        file = config / 'settings.toml'
        file.write_text(SETTINGS)
        (file.parent / "rules.md").write_text("Review carefully.", encoding="utf-8")
        save_settings(config, {'agent': {'effort': 'high'}}, settings_digest(config))
        def editor(args):
            candidate = Path(args[-1])
            candidate.write_text(candidate.read_text() + '# edited')
            return 0
        with patch('lightkeeper.ui.editing.subprocess.call', editor):
            self.assertIsNone(edit_file(self.app, file, lambda path: validate_settings_file(path, config),
                                        config_dir=config))
        self.assertTrue((config / '.write-lock').exists())
        self.assertFalse((self.root / '.write-lock').exists())
        self.assertTrue(file.read_text().endswith('# edited'))

    def test_suspend_and_arbitrary_validator_errors_retain_private_copy(self):
        self.app.suspend.side_effect = RuntimeError('no terminal')
        error = self.run_editor('edited')
        self.assertIn('editor unavailable: no terminal', error)
        self.assertIn('edited copy kept at', error)
        self.assertEqual(self.live.read_text(), 'original')
        self.app.suspend.side_effect = nullcontext
        def validator(path):
            path.chmod(0o644)
            raise LookupError('validator failed')
        error = self.run_editor('edited', validate=validator)
        self.assertIn('editor unavailable: validator failed', error)
        copy = Path(error.split('edited copy kept at ', 1)[1])
        self.assertEqual(copy.read_text(), 'edited')
        self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.live.read_text(), 'original')

    def test_symlinked_notes_refused_before_editor_and_after_validation(self):
        notes = self.root / 'repos'
        notes.mkdir()
        link = notes / 'example__widget.md'
        link.symlink_to(self.live)
        with patch('lightkeeper.ui.editing.subprocess.call') as editor:
            error = edit_file(self.app, link, nonempty_utf8)
            self.assertIn('symlinked', error)
            editor.assert_not_called()
        link.unlink()
        link.write_text('original')
        def validator(path):
            link.unlink()
            link.symlink_to(self.live)
        with patch('lightkeeper.ui.editing.subprocess.call', return_value=0):
            error = edit_file(self.app, link, validator)
        self.assertIn('symlinked', error)
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.live.read_text(), 'original')



@unittest.skipUnless(HAS_UI, "optional UI dependencies are not installed")
class ScreenTests(IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / 'settings.toml').write_text(SETTINGS)
        (self.root / 'rules.md').write_text('Review carefully.', encoding='utf-8')
        screen = fixture_screen()
        runtime = replace(screen.runtime, config_dir=str(self.root), state_dir=str(self.root / 'state'),
                          live_lock_held=False, dry_repos=('example/widget',),
                          engine_identity=Identity(False, 'test-account', 'needs login', 'claude auth login'),
                          repo_checks=(('example/widget', 'access ok · default branch main'),), gh_login='reviewer')
        self.observer = Observer(Projection(), runtime)
        self.observer.snapshot = replace(screen, runtime=runtime)
        self.runner = Mock(cancelled=False, commands=CommandInbox(threading.Event()))
        worker = Mock(runner=self.runner, poll_join=Mock(return_value=False))
        self.app = TextualApp(self.observer, worker, clipboard=Mock(), folder_opener=Mock())

    def commands(self):
        return list(self.runner.commands.queue)

    async def open_repos(self, pilot):
        await pilot.press('2')
        await pilot.pause()
        self.assertIsInstance(self.app.screen, Repositories)
        return self.app.screen

    async def test_table_space_effective_modes_literal_cells_and_navigation(self):
        async with self.app.run_test(size=(130, 40)) as pilot:
            screen = await self.open_repos(pilot)
            table = screen.query_one(DataTable)
            self.assertEqual(table.row_count, 1)
            cells = table.get_row('example/widget')
            self.assertTrue(all(isinstance(c, Text) for c in cells))
            self.assertEqual(cells[2].plain, 'dry-run')
            self.assertIn('access ok', cells[-1].plain)
            await pilot.press('space')
            self.assertFalse(load_settings(self.root).repo('example/widget').enabled)
            self.assertEqual([c.kind for c in self.commands()], ['wake'])
            await pilot.press('3')
            self.assertIsInstance(self.app.screen, AgentPanel)
            await pilot.press('1')
            self.assertIs(self.app.screen, self.app.overview)

    async def test_form_single_field_save_and_remarks_feedback(self):
        async with self.app.run_test(size=(130, 45)) as pilot:
            screen = await self.open_repos(pilot)
            await pilot.press('enter')
            form = self.app.screen
            self.assertIsInstance(form, RepoForm)
            form.query_one('#remarks', Select).value = 'all'
            await pilot.pause()
            with patch('lightkeeper.ui.repos.save_settings', wraps=save_settings) as save:
                await pilot.click('#save')
                try:
                    await self.app.workers.wait_for_complete()  # The save runs on the worker queue.
                except WorkerCancelled:  # A superseded access check; the latest one still completes.
                    pass
                await pilot.pause()
            self.assertEqual(save.call_args.args[1], {'repos': {'example/widget': {'remarks': 'all'}}})
            self.assertIs(self.app.screen, screen)
            self.assertIn('re-evaluates', contents(self.app))
            self.assertIn('saved; the Runner re-checks before each write', contents(self.app))

    async def test_live_switch_confirmation_and_restart(self):
        save_settings(self.root, {'repos': {'example/widget': {'post': 'dry-run'}}}, settings_digest(self.root))
        async with self.app.run_test(size=(130, 45)) as pilot:
            await self.open_repos(pilot)
            await pilot.press('enter')
            form = self.app.screen
            form.query_one('#post', Select).value = 'live'
            await pilot.pause()
            self.assertIsInstance(self.app.screen, Confirm)
            self.assertIn('restart required for live', contents(self.app))
            await pilot.click('#cancel')
            await pilot.pause()
            self.assertEqual(form.query_one('#post', Select).value, 'dry-run')
            form.query_one('#post', Select).value = 'live'
            await pilot.pause()
            await pilot.click('#confirm')
            await pilot.pause()
            self.assertIn('restart required for live', contents(self.app))
            await pilot.click('#save')
            await pilot.pause()
            self.assertEqual(load_settings(self.root).repo('example/widget').post, 'live')
            self.assertIn('restart required for live', contents(self.app))

    async def test_new_repo_waits_for_matching_result_refuses_no_access_and_archived(self):
        async with self.app.run_test(size=(130, 45)) as pilot:
            await self.open_repos(pilot)
            await pilot.press('a')
            form = self.app.screen
            form.query_one('#repo-name', Input).value = 'example/new'
            for outcome, result in [('failed', 'no access'), ('failed', 'archived'), ('done', 'access ok · default branch main')]:
                await pilot.click('#save')
                await pilot.pause()
                self.assertIn('checking access', contents(self.app))
                self.assertNotIn('example/new', load_settings(self.root).repos)
                command = self.commands()[-1]
                self.assertEqual((command.kind, command.key), ('check_repo', ('example/new',)))
                self.observer('command_done', id='unrelated', outcome='done', text='access ok')
                form.refresh_panel()
                self.assertIsNotNone(form.pending)
                self.runner.commands.get_nowait()
                self.runner.commands.complete(command)
                self.observer('command_done', id=command.id, outcome=outcome, text=result)
                form.refresh_panel()
                try:
                    await self.app.workers.wait_for_complete()  # The save runs on the worker queue.
                except WorkerCancelled:  # A superseded access check; the latest one still completes.
                    pass
                await pilot.pause()
                if outcome == 'failed':
                    self.assertIn(result, contents(self.app))
                    self.assertNotIn('example/new', load_settings(self.root).repos)
            self.assertEqual(load_settings(self.root).repo('example/new').post, 'dry-run')
            self.assertEqual(self.commands()[-1].kind, 'wake')

    async def test_remove_confirmation_preserves_projection(self):
        async with self.app.run_test(size=(130, 45)) as pilot:
            await self.open_repos(pilot)
            before = self.observer.snapshot
            await pilot.press('enter')
            await pilot.click('#remove')
            await pilot.pause()
            self.assertIn('Journal history is kept', contents(self.app))
            await pilot.click('#confirm')
            await self.app.workers.wait_for_complete()  # The removal runs on the worker queue.
            await pilot.pause()
            self.assertFalse(load_settings(self.root).repos)
            self.assertIs(self.observer.snapshot, before)

    async def test_stale_form_refuses_and_input_guards(self):
        async with self.app.run_test(size=(130, 45)) as pilot:
            await self.open_repos(pilot)
            await pilot.press('a')
            form = self.app.screen
            field = form.query_one(Input)
            field.focus()
            await pilot.press('q', '1', '2', '3', 'A', 'R', 'C', 't', 'n')
            self.assertIs(self.app.screen, form)
            self.assertTrue(field.value)
            self.assertEqual(self.commands(), [])
            self.assertFalse(form.check_action('toggle', ('approve',)))
            await pilot.click('#cancel')
            await pilot.press('enter')
            file = self.root / 'settings.toml'
            file.write_text(SETTINGS + '# external\n')
            self.app.screen.query_one('#remarks', Select).value = 'all'
            await pilot.click('#save')
            await pilot.pause()
            self.assertIn('settings changed on disk; reopen the form', contents(self.app))
            self.assertTrue(file.read_text().endswith('# external\n'))

    async def test_agent_identities_probe_copy_edit_folder_and_guard(self):
        async with self.app.run_test(size=(130, 45)) as pilot:
            await pilot.press('3')
            await pilot.pause()
            screen = self.app.screen
            output = contents(self.app)
            for value in ('Engine:', 'Model:', 'Effort:', 'Profile: test', 'test-account', 'claude auth login',
                          'gh: reviewer', 'expected:', 'Config dir:', 'State dir:', 'Notifications:', 'Uptime:'):
                self.assertIn(value, output)
            await pilot.press('t', 'c', 'L')
            await pilot.pause()
            self.assertEqual(self.commands()[0].kind, 'probe_engine')
            self.app.copy_command.assert_called_once_with('claude auth login')
            self.app.folder_opener.assert_called_once_with(str(self.root / 'state'))
            from lightkeeper.ui.editor import Editor
            for key, file in [('e', 'settings.toml'), ('r', 'rules.md'), ('T', 'template.md')]:
                await pilot.press(key)
                await pilot.pause()
                self.assertIsInstance(self.app.screen, Editor)
                self.assertEqual(self.app.screen.path, self.root / file)
                await pilot.press('escape')
            field = Input()
            await screen.mount(field)
            field.focus()
            await pilot.press('t', 'e', 'r', 'm', 'q', '2')
            self.assertIs(self.app.screen, screen)
            self.assertFalse(screen.check_action('probe', ()))

    async def test_notes_creates_empty_template_and_feedback(self):
        async with self.app.run_test(size=(130, 45)) as pilot:
            await self.open_repos(pilot)
            from textual.widgets import TextArea
            from lightkeeper.ui.editor import Editor
            await pilot.press('n')
            await pilot.pause()
            editor = self.app.screen
            self.assertIsInstance(editor, Editor)
            self.assertEqual(editor.path, self.root / 'repos/example__widget.md')
            self.assertIs(editor.validate, nonempty_utf8)
            self.assertEqual(editor.query_one(TextArea).text, '')
            await pilot.press('n', 'o', 't', 'e', 's', 'ctrl+s')
            await pilot.pause()
            self.assertEqual(editor.path.read_text(encoding='utf-8'), 'notes')
            self.assertEqual(editor.path.stat().st_mode & 0o777, 0o600)
            self.assertIn('re-evaluates', contents(self.app))
            self.assertEqual(self.commands()[-1].kind, 'wake')

    async def test_narrow_ascii_no_color_unchecked_and_input_guard(self):
        save_settings(self.root, {'repos': {'example/widget': {'allowed': ['approve']}}}, settings_digest(self.root))
        rt = replace(self.observer.snapshot.runtime, repo_checks=(), live_lock_held=True, mode='live', dry_repos=())
        self.observer.snapshot = replace(self.observer.snapshot, runtime=rt)
        with patch.dict(os.environ, {'NO_COLOR': '1', 'LIGHTKEEPER_ASCII': '1'}):
            self.app.ascii = True
            async with self.app.run_test(size=(60, 18)) as pilot:
                screen = await self.open_repos(pilot)
                cells = screen.query_one(DataTable).get_row('example/widget')
                self.assertEqual(cells[2].plain, 'live')
                self.assertEqual(cells[-1].plain, 'unchecked')
                self.assertTrue(any(span.style == 'dim' for span in cells[4].spans))
                field = Input()
                await screen.mount(field)
                field.focus()
                await pilot.press('q', '3', 'a', 'n', 'space')
                self.assertIs(self.app.screen, screen)
                self.assertFalse(screen.check_action('toggle', ()))
                self.assertEqual(self.commands(), [])
                await field.remove()
                screen.query_one(DataTable).focus()
                await pilot.press('enter')
                await pilot.pause()
                form = self.app.screen
                self.assertIsInstance(form, RepoForm)
                # Snapshot polling under a modal must not reset the form or its digest.
                digest = form.digest
                for _ in range(30):
                    self.observer('command_done', id='other', outcome='done', text='checked')
                self.app.refresh_snapshot()
                self.assertEqual(form.digest, digest)
                self.assertFalse(self.app._render_failed)
                form.query_one('#cancel', Button).scroll_visible(animate=False)
                await pilot.pause()
                await pilot.click('#cancel')
                await pilot.press('3')
                await pilot.pause()
                self.assertIn('Engine:', contents(self.app))
                self.assertFalse(self.app._render_failed)

    async def test_poll_during_screen_mount_is_safe(self):
        async with self.app.run_test(size=(100, 30)) as pilot:
            for number in (2, 3, 1):
                self.app.action_screen(number)
                self.app.refresh_snapshot()
                self.assertFalse(self.app._render_failed)
                await pilot.pause()

    async def test_cancel_new_check_and_remove_never_save(self):
        async with self.app.run_test(size=(130, 45)) as pilot:
            screen = await self.open_repos(pilot)
            await pilot.press('enter')
            await pilot.click('#remove')
            await pilot.pause()
            await pilot.click('#cancel')
            await pilot.pause()
            self.assertIn('example/widget', load_settings(self.root).repos)
            await pilot.click('#cancel')
            await pilot.press('a')
            form = self.app.screen
            form.query_one(Input).value = 'example/new'
            await pilot.click('#save')
            command = self.commands()[-1]
            await pilot.click('#cancel')
            self.observer('command_done', id=command.id, outcome='done', text='access ok')
            self.app.refresh_snapshot()
            self.assertNotIn('example/new', load_settings(self.root).repos)
            self.assertIs(self.app.screen, screen)

    async def test_disabled_and_new_dry_repo_stay_dry_with_live_lock(self):
        save_settings(self.root, {'repos': {'example/widget': {'enabled': False}}}, settings_digest(self.root))
        save_settings(self.root, {'repos': {'example/new': {}}}, settings_digest(self.root))
        self.observer.snapshot = replace(self.observer.snapshot, runtime=replace(
            self.observer.snapshot.runtime, mode='live', live_lock_held=True, dry_repos=()))
        async with self.app.run_test(size=(130, 45)) as pilot:
            screen = await self.open_repos(pilot)
            table = screen.query_one(DataTable)
            self.assertEqual(table.get_row('example/widget')[2].plain, 'dry-run')
            self.assertEqual(table.get_row('example/new')[2].plain, 'dry-run')

    async def test_unchanged_form_does_not_write_or_wake(self):
        async with self.app.run_test(size=(130, 45)) as pilot:
            screen = await self.open_repos(pilot)
            before = (self.root / 'settings.toml').stat().st_mtime_ns
            await pilot.press('enter')
            with patch('lightkeeper.ui.repos.save_settings') as save:
                await pilot.click('#save')
                await pilot.pause()
                save.assert_not_called()
            self.assertIs(self.app.screen, screen)
            self.assertEqual((self.root / 'settings.toml').stat().st_mtime_ns, before)
            self.assertEqual(self.commands(), [])

    async def test_existing_check_is_awaited_and_missing_result_times_out(self):
        command = Command('existing', 'check_repo', ('example/new',))
        self.runner.commands.put(command)
        # A check remains shareable after the Runner has dequeued it.
        self.runner.commands.get_nowait()
        async with self.app.run_test(size=(130, 45)) as pilot:
            await self.open_repos(pilot)
            await pilot.press('a')
            form = self.app.screen
            form.query_one(Input).value = 'example/new'
            await pilot.click('#save')
            self.assertEqual(form.pending, 'existing')
            self.assertIn('checking access', contents(self.app))
            form.pending_since = self.app.clock() - 31
            form.refresh_panel()
            self.assertIn('check result lost; retry', contents(self.app))
            self.assertFalse(form.query_one('#save', Button).disabled)
            form.save()
            self.assertEqual(form.pending, 'existing')
            self.runner.commands.complete(command)
            self.observer('command_done', id='existing', outcome='done', text='access ok')
            form.refresh_panel()
            await pilot.pause()
            self.assertIn('example/new', load_settings(self.root).repos)

    async def test_panels_clear_load_error_after_success_and_cache_polls(self):
        async with self.app.run_test(size=(130, 45)) as pilot:
            for key in ('2', '3'):
                app_thread = threading.get_ident()
                def worker_load(*args, **kwargs):
                    self.assertNotEqual(threading.get_ident(), app_thread)
                    return load_settings(*args, **kwargs)
                with patch('lightkeeper.ui.repos.load_settings', side_effect=worker_load) as load:
                    await pilot.press(key)
                    await pilot.pause()
                    screen = self.app.screen
                    for _ in range(3):
                        screen.refresh_panel()
                        await pilot.pause()
                    self.assertEqual(load.call_count, 1)
                    file = self.root / 'settings.toml'
                    file.write_text('broken = [')
                    screen.refresh_panel()
                    await pilot.pause()
                    self.assertIn('toml:', contents(self.app))
                    file.write_text(SETTINGS)
                    screen.refresh_panel()
                    await pilot.pause()
                    self.assertNotIn('toml:', contents(self.app))

    async def test_disk_worker_keeps_app_responsive_and_serializes_jobs(self):
        entered, release = threading.Event(), threading.Event()
        app_thread = threading.get_ident()
        fsync = os.fsync
        def slow_fsync(fd):
            self.assertNotEqual(threading.get_ident(), app_thread)
            entered.set()
            if not release.wait(4):
                raise RuntimeError('test worker timed out')
            fsync(fd)
        async with self.app.run_test(size=(130, 45)) as pilot:
            await self.open_repos(pilot)
            with patch('lightkeeper.settings.os.fsync', slow_fsync):
                await pilot.press('space')
                await pilot.pause()
                self.assertTrue(entered.is_set())
                second = Mock()
                self.app.run_disk(second, lambda result, error: None)
                with patch.object(self.app, 'notify') as notify:
                    await pilot.press('?')
                    notify.assert_called_once()
                second.assert_not_called()
                self.assertFalse(self.runner.cancelled)
                release.set()
                await pilot.pause()
            self.assertFalse(load_settings(self.root).repo('example/widget').enabled)
            second.assert_called_once()
            self.assertIn('saved;', contents(self.app))

    async def test_help_on_panels_forms_confirmations_and_editor_failure_survival(self):
        async with self.app.run_test(size=(130, 45)) as pilot:
            await self.open_repos(pilot)
            await pilot.press('enter')
            with patch.object(self.app, 'notify') as notify:
                await pilot.press('?')
                notify.assert_called_once()
            await pilot.click('#remove')
            await pilot.pause()
            with patch.object(self.app, 'notify') as notify:
                await pilot.press('?')
                notify.assert_called_once()
            await pilot.click('#cancel')
            await pilot.press('escape', '3')
            with patch.object(self.app, 'notify') as notify:
                await pilot.press('?')
                notify.assert_called_once()
            with patch.object(self.app, 'suspend', side_effect=SuspendNotSupported('no terminal')):
                await pilot.press('r')
                await pilot.pause()
                await pilot.press('ctrl+e')
                await pilot.pause()
                self.assertIn('editor unavailable: no terminal', contents(self.app))
                self.assertFalse(self.runner.cancelled)
                await pilot.press('escape', 't')
                self.assertEqual(self.commands()[-1].kind, 'probe_engine')
            with patch.object(self.app, 'suspend', side_effect=nullcontext), \
                 patch('lightkeeper.ui.editing.subprocess.call', return_value=0), \
                 patch('lightkeeper.ui.agent.nonempty_utf8', side_effect=LookupError('validator failed')):
                await pilot.press('r')
                await pilot.pause()
                await pilot.press('ctrl+e')
                await pilot.pause()
                self.assertIn('editor unavailable: validator failed', contents(self.app))
                self.assertFalse(self.runner.cancelled)
                await pilot.press('escape', '1')
                self.assertIs(self.app.screen, self.app.overview)



class BoundaryTests(TestCase):
    def test_ui_has_no_github_or_journal_dependencies(self):
        import ast
        root = Path(__file__).resolve().parents[1] / 'lightkeeper/ui'
        for path in root.glob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    imported = (getattr(node, 'module', '') or '') + ' '.join(a.name for a in node.names)
                    self.assertNotIn('github', imported, path)
                    self.assertNotIn('journal', imported, path)
                    self.assertNotIn('Gh', imported, path)
                    self.assertNotIn('Journal', imported, path)
