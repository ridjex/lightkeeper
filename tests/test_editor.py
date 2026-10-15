"""Offline editor and focus regressions with private scratch files and a fake Runner."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

if any(importlib.util.find_spec(name) is None for name in ('rich', 'textual')):
    raise unittest.SkipTest('optional UI dependencies are not installed')

from textual.widgets import OptionList, TextArea

from lightkeeper.commands import CommandInbox
from lightkeeper.settings import load_settings, settings_digest
from lightkeeper.ui.editor import Editor, open_editor
from lightkeeper.ui.editing import nonempty_utf8
from lightkeeper.ui.menu import Menu, ScreenForm
from lightkeeper.ui.model import Runtime
from lightkeeper.ui.project import Observer, Projection
from lightkeeper.ui.repos import Confirm, RepoCheckbox, RepoForm
from lightkeeper.ui.textual_app import TextualApp
from tests.test_textual import contents, wait_until


class EditorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'settings.toml').write_text('[identity]\nlogin="reviewer"\n', encoding='utf-8')
        (self.root / 'rules.md').write_text('Review café carefully.\n', encoding='utf-8')
        (self.root / 'template.md').write_text('{why}\n', encoding='utf-8')
        rt = Runtime(login='reviewer', config_dir=str(self.root))
        worker = Mock(runner=Mock(cancelled=False, commands=CommandInbox(threading.Event())),
                      poll_join=Mock(return_value=False))
        self.app = TextualApp(Observer(Projection(), rt), worker, clock=lambda: 100)

    async def open(self, pilot, name='rules.md'):
        await pilot.press('m')
        choices = self.app.screen.query_one(OptionList)
        choices.highlighted = [key for key, _ in Menu.ENTRIES].index(name)
        await pilot.press('enter')
        await wait_until(lambda: isinstance(self.app.screen, Editor))
        return self.app.screen

    async def test_menu_reads_each_file_and_typing_saves_private_utf8_and_wakes(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            for name in ('settings.toml', 'rules.md', 'template.md'):
                editor = await self.open(pilot, name)
                area = editor.query_one(TextArea)
                self.assertEqual(area.text, (self.root / name).read_text(encoding='utf-8'))
                self.assertEqual(area.soft_wrap, name.endswith('.md'))
                self.assertFalse(area.show_line_numbers)
                self.assertIsNone(area.language)
                await pilot.pause()
                self.assertGreater(area.content_size.height, 0)
                self.assertLessEqual(editor.query_one('#editor-dialog').size.width, 80)
                self.assertLessEqual(editor.query_one('#editor-dialog').size.height, 24 * .9)
                self.assertIn('Ctrl-E open in $EDITOR', contents(self.app))
                await pilot.press('escape')
            editor = await self.open(pilot)
            await pilot.press('m', 'q', 'question_mark', '1', '2', '3')
            self.assertIs(self.app.screen, editor)
            self.assertTrue(editor.query_one(TextArea).text.startswith('mq?123'))
            self.app.worker.request_stop.assert_not_called()
            self.assertTrue(self.app.dialog_open())
            await pilot.press('ctrl+s')
            await wait_until(lambda: self.app.screen is not editor)
            path = self.root / 'rules.md'
            self.assertTrue(path.read_text(encoding='utf-8').startswith('mq?123Review café'))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.app.worker.runner.commands.get_nowait().kind, 'wake')
            self.assertIn('saved;', contents(self.app))

    async def test_invalid_settings_and_template_keep_buffer_and_disk(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            for name, bad, message in [('settings.toml', '[broken', 'TOML'),
                                       ('template.md', '{unknown}', 'unknown placeholder')]:
                path = self.root / name
                original = path.read_bytes()
                editor = await self.open(pilot, name)
                editor.query_one(TextArea).load_text(bad)
                await pilot.press('ctrl+s')
                await wait_until(lambda: editor.message not in ('', 'saving'))
                self.assertIs(self.app.screen, editor)
                self.assertIn(message.lower(), editor.message.lower())
                self.assertEqual(editor.query_one(TextArea).text, bad)
                self.assertEqual(path.read_bytes(), original)
                await pilot.press('escape')
                await pilot.click('#confirm')
            self.assertEqual(list(self.app.worker.runner.commands.queue), [])

    async def test_concurrent_disk_change_is_refused(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            editor = await self.open(pilot)
            (self.root / 'rules.md').write_text('Other writer', encoding='utf-8')
            await pilot.press('x', 'ctrl+s')
            await wait_until(lambda: editor.message not in ('', 'saving'))
            self.assertEqual(editor.message, 'file changed on disk; reopen the editor')
            self.assertEqual((self.root / 'rules.md').read_text(encoding='utf-8'), 'Other writer')
            self.assertIs(self.app.screen, editor)

    async def test_escape_clean_closes_dirty_confirms_and_cancel_preserves_text(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            editor = await self.open(pilot)
            await pilot.press('escape')
            self.assertNotIn(editor, self.app.screen_stack)
            editor = await self.open(pilot)
            await pilot.press('x', 'escape')
            self.assertIsInstance(self.app.screen, Confirm)
            await pilot.press('escape')
            self.assertIs(self.app.screen, editor)
            self.assertTrue(editor.query_one(TextArea).text.startswith('x'))
            await pilot.press('escape')
            await pilot.click('#confirm')
            self.assertNotIn(editor, self.app.screen_stack)
            self.assertEqual(list(self.app.worker.runner.commands.queue), [])

    async def test_external_clean_calls_edit_file_dirty_refuses(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            editor = await self.open(pilot)
            with patch('lightkeeper.ui.editor.edit_file', return_value=None) as edit:
                await pilot.press('x', 'ctrl+e')
                self.assertEqual(editor.message, 'save or close first')
                edit.assert_not_called()
                editor.query_one(TextArea).load_text(editor.initial_text)
                await pilot.press('ctrl+e')
                await wait_until(lambda: self.app.screen is not editor)
                edit.assert_called_once_with(self.app, self.root / 'rules.md', nonempty_utf8, config_dir=self.root)
            self.assertEqual(self.app.worker.runner.commands.get_nowait().kind, 'wake')

    async def test_real_worker_launch_blocks_textual_only_and_resumes_on_error(self):
        main = threading.get_ident()
        entered, progressed = threading.Event(), threading.Event()
        timer_calls = []
        suspended = []

        def runner():
            if entered.wait(3):
                progressed.set()

        @contextmanager
        def suspend():
            self.assertEqual(threading.get_ident(), main)
            suspended.append(True)
            yield
            suspended.append(False)

        def editor(args):
            self.assertEqual(threading.get_ident(), main)
            before = len(timer_calls)
            entered.set()
            self.assertTrue(progressed.wait(3))
            self.assertEqual(len(timer_calls), before)
            raise OSError('test launch failure')

        async with self.app.run_test(size=(80, 24)) as pilot:
            screen = await self.open(pilot)
            self.app.set_interval(.01, lambda: timer_calls.append(True))
            runner_thread = threading.Thread(target=runner)
            runner_thread.start()
            try:
                with patch.object(self.app, 'suspend', suspend), patch('lightkeeper.ui.editing.subprocess.call', editor):
                    await pilot.press('ctrl+e')
                    await wait_until(lambda: bool(screen.message))
            finally:
                entered.set()
                runner_thread.join(3)
            self.assertEqual(suspended, [True, False])
            self.assertIn('test launch failure', screen.message)
            await pilot.press('x')
            self.assertTrue(screen.query_one(TextArea).text.startswith('x'))

    async def test_ctrl_c_quits_a_clean_editor_and_warns_a_dirty_one(self):
        async with self.app.run_test(size=(80, 24)) as pilot:
            editor = await self.open(pilot)
            await pilot.press('x', 'ctrl+c')
            self.app.worker.request_stop.assert_not_called()
            self.assertEqual(editor.message, 'unsaved changes: Ctrl-S saves, Esc discards')
            self.assertIs(self.app.screen, editor)
            editor.query_one(TextArea).load_text(editor.initial_text)
            await pilot.press('ctrl+c')
            self.app.worker.request_stop.assert_called_once_with()

    async def test_open_refuses_symlink_and_read_has_no_side_effects(self):
        path = self.root / 'rules.md'
        path.chmod(0o640)
        self.root.chmod(0o750)
        async with self.app.run_test(size=(80, 24)) as pilot:
            editor = await self.open(pilot)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(self.root.stat().st_mode & 0o777, 0o750)
            await pilot.press('escape')
            link = self.root / 'link.md'
            link.symlink_to(path)
            edited = Mock()
            open_editor(self.app, link, nonempty_utf8, edited, config_dir=self.root)
            await wait_until(lambda: edited.called)
            self.assertIn('symlinked', str(edited.call_args.args[1]))
            self.assertNotIn(editor, self.app.screen_stack)

    async def test_shared_checkbox_focus_keeps_one_content_row_and_mark(self):
        async with self.app.run_test(size=(100, 45)) as pilot:
            settings = load_settings(self.root)
            digest = settings_digest(self.root)
            for form, selector in [(ScreenForm(settings, digest), '#moon'),
                                   (RepoForm(settings, digest), '#enabled')]:
                await self.app.push_screen(form)
                await pilot.pause()
                checkbox = form.query_one(selector, RepoCheckbox)
                original = checkbox.value
                await pilot.click(selector)
                self.assertTrue(checkbox.has_focus)
                self.assertEqual(checkbox.region.height, 1)
                self.assertEqual(checkbox.content_size.height, 1)
                self.assertEqual(checkbox.value, not original)
                mark = '[X]' if checkbox.value else '[ ]'
                self.assertIn(mark, checkbox.render().plain)
                self.assertIn(mark, checkbox.render_line(0).text)
                self.assertTrue(checkbox.get_visual_style('toggle--label').underline)
                await pilot.press('tab', 'shift+tab', 'space')
                self.assertEqual(checkbox.content_size.height, 1)
                self.assertEqual(checkbox.value, original)
                await pilot.press('escape')
