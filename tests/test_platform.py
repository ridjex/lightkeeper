"""Desktop command selection is offline and works without the UI extra."""
import unittest
import subprocess
from unittest.mock import Mock, patch

from lightkeeper.ui.platform import clipboard, open_folder


class PlatformTests(unittest.TestCase):
    def test_clipboard_priority_arguments_and_utf8(self):
        cases = [('darwin', {'pbcopy'}, ['pbcopy']),
                 ('linux', {'wl-copy', 'xclip', 'xsel'}, ['wl-copy']),
                 ('linux', {'xclip', 'xsel'}, ['xclip', '-selection', 'clipboard']),
                 ('linux', {'xsel'}, ['xsel', '--clipboard', '--input'])]
        for platform, available, expected in cases:
            with self.subTest(platform=platform, available=available), \
                 patch('lightkeeper.ui.platform.sys.platform', platform), \
                 patch('lightkeeper.ui.platform.shutil.which', side_effect=lambda cmd: cmd if cmd in available else None), \
                 patch('lightkeeper.ui.platform.subprocess.run') as run:
                fallback = Mock()
                self.assertTrue(clipboard('café 日本', fallback))
                run.assert_called_once_with(expected, input='café 日本', encoding='utf-8', check=True, timeout=5,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                fallback.assert_not_called()

    def test_clipboard_without_executable_uses_terminal_fallback(self):
        with patch('lightkeeper.ui.platform.shutil.which', return_value=None), \
             patch('lightkeeper.ui.platform.subprocess.run') as run:
            fallback = Mock()
            self.assertFalse(clipboard('manual command', fallback))
            fallback.assert_called_once_with('manual command')
            run.assert_not_called()

    def test_installed_but_unusable_clipboards_fall_back_without_terminal_output(self):
        with patch('lightkeeper.ui.platform.sys.platform', 'linux'), \
             patch('lightkeeper.ui.platform.shutil.which', side_effect=lambda cmd: cmd), \
             patch('lightkeeper.ui.platform.subprocess.run', side_effect=[
                 subprocess.CalledProcessError(1, 'wl-copy'),
                 subprocess.TimeoutExpired('xclip', 5), OSError('xsel failed')]) as run:
            fallback = Mock()
            self.assertFalse(clipboard('manual command', fallback))
            fallback.assert_called_once_with('manual command')
            self.assertEqual(run.call_count, 3)
            for call in run.call_args_list:
                self.assertEqual(call.kwargs['stderr'], subprocess.DEVNULL)

    def test_folder_commands_and_missing_executable(self):
        for platform, command in [('darwin', 'open'), ('linux', 'xdg-open')]:
            with self.subTest(platform=platform), patch('lightkeeper.ui.platform.sys.platform', platform), \
                 patch('lightkeeper.ui.platform.shutil.which', return_value=command) as which, \
                 patch('lightkeeper.ui.platform.subprocess.run') as run:
                open_folder('/tmp/session')
                which.assert_called_once_with(command)
                run.assert_called_once_with([command, '/tmp/session'], check=True, timeout=5,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                which.return_value = None
                with self.assertRaises(FileNotFoundError):
                    open_folder('/tmp/session')
                self.assertEqual(run.call_count, 1)


class ScreenFallbackTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        if any(importlib.util.find_spec(name) is None for name in ('rich', 'textual')):
            self.skipTest('optional UI dependencies are not installed')

    def test_folder_failure_and_clipboard_fallback_show_manual_values(self):
        from lightkeeper.ui.textual_app import TextualApp
        app = Mock()
        app.call_from_thread.side_effect = lambda fn, *args: fn(*args)
        app.run_worker.side_effect = lambda fn, **kwargs: fn()
        app.folder_opener.side_effect = FileNotFoundError('xdg-open')
        TextualApp.open_folder_in_thread(app, '/tmp/session')
        self.assertIn('/tmp/session', app.show_message.call_args.args[0])
        app.selected = ('alerts', Mock())
        app.fix_command.return_value = 'claude auth login'
        app.copy_command.return_value = False
        TextualApp.action_copy_fix(app)
        self.assertEqual(app.show_message.call_args.args[0], 'claude auth login')
        app.copy_command.side_effect = FileNotFoundError('clipboard')
        TextualApp.action_copy_fix(app)
        self.assertIn('claude auth login', app.show_message.call_args.args[0])

    def test_textual_terminal_clipboard_is_used_when_no_command_exists(self):
        from textual.app import App
        from lightkeeper.ui.textual_app import TextualApp
        app = TextualApp(Mock(), Mock())
        with patch('lightkeeper.ui.platform.shutil.which', return_value=None), \
             patch.object(App, 'copy_to_clipboard') as fallback:
            self.assertFalse(app.copy_command('manual command'))
            fallback.assert_called_once_with('manual command')

    def test_native_textual_copy_does_not_launch_desktop_commands(self):
        from textual.app import App
        from lightkeeper.ui.textual_app import TextualApp
        app = TextualApp(Mock(), Mock())
        with patch.object(App, 'copy_to_clipboard') as native, \
             patch('lightkeeper.ui.platform.subprocess.run') as run:
            app.copy_to_clipboard('selected input')
            native.assert_called_once_with('selected input')
            run.assert_not_called()
