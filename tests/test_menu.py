"""Offline Textual pilots for release reminders, the menu and live screen settings."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock

if any(importlib.util.find_spec(name) is None for name in ('rich', 'textual')):
    raise unittest.SkipTest('optional UI dependencies are not installed')

from textual.widgets import Static

from lightkeeper import __version__
from lightkeeper.commands import CommandInbox
from lightkeeper.ui.model import Runtime
from lightkeeper.ui.project import Observer, Projection
from lightkeeper.ui.textual_app import TextualApp
from tests.test_textual import contents


class MenuTests(IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'settings.toml').write_text('[identity]\nlogin="reviewer"\n')

    def app(self, **fields):
        rt = Runtime(login='reviewer', gh_login='reviewer', config_dir=str(self.root), **fields)
        observer = Observer(Projection(), rt)
        worker = Mock(runner=Mock(cancelled=False, commands=CommandInbox(threading.Event())),
                      poll_join=Mock(return_value=False))
        return TextualApp(observer, worker, clock=lambda: 100)

    async def test_update_header_and_agent_version_line(self):
        app = self.app(update_version='0.2.0')
        async with app.run_test(size=(200, 40)) as pilot:
            header = app.query_one('#header', Static).content.renderables[0].columns[0]._cells[0]
            self.assertIn('update 0.2.0', header.plain)
            self.assertTrue(header.get_style_at_offset(app.console, header.plain.index('update')).dim)
            self.assertGreater(header.plain.index('update'), header.plain.index('claude'))
            await pilot.press('3')
            self.assertIn(f'lightkeeper {__version__} · update 0.2.0 available: brew upgrade lightkeeper or pipx upgrade lightkeeper', contents(app))
            app.observer('policy', updates=False)
            app.refresh_snapshot()
            self.assertIn(f'lightkeeper {__version__}', contents(app))
            self.assertNotIn('update 0.2.0', contents(app))

    async def choose(self, app, pilot, key):
        from lightkeeper.ui.menu import Menu
        from textual.widgets import OptionList
        await pilot.press('m')
        self.assertIsInstance(app.screen, Menu)
        choices = app.screen.query_one(OptionList)
        choices.highlighted = [k for k, _ in Menu.ENTRIES].index(key)
        await pilot.press('enter')
        await pilot.pause()

    async def test_menu_opens_closes_and_navigation_entries(self):
        from lightkeeper.ui.menu import Menu, ScreenForm
        from lightkeeper.ui.repos import Repositories, RepoForm
        from lightkeeper.ui.agent import AgentPanel
        from lightkeeper.ui.detail import PRDetail
        from lightkeeper.settings import load_settings, settings_digest
        from tests.test_ui import fixture_screen
        from textual.widgets import OptionList
        app = self.app()
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.press('m')
            self.assertIsInstance(app.screen, Menu)
            self.assertEqual(app.screen.query_one(OptionList).option_count, 11)
            await pilot.press('m')
            self.assertIs(app.screen, app.overview)
            await pilot.press('m', 'escape')
            self.assertIs(app.screen, app.overview)
            for key, expected in [('repositories', Repositories), ('agent', AgentPanel), ('overview', None)]:
                await self.choose(app, pilot, key)
                self.assertIsInstance(app.screen, expected) if expected else self.assertIs(app.screen, app.overview)
            for key in ('2', '3', '1'):
                await pilot.press(key)
                await pilot.press('m', 'escape')
            # m works above details and forms; text inputs keep normal typing.
            settings = load_settings(self.root)
            for screen in (PRDetail(fixture_screen().watching[0]),
                           RepoForm(settings, settings_digest(self.root)),
                           ScreenForm(settings, settings_digest(self.root))):
                await app.push_screen(screen)
                from textual.widgets import Input
                if isinstance(app.focused, Input):
                    field = app.focused
                    await pilot.press('m')
                    self.assertIn('m', field.value)
                    await pilot.press('tab')
                await pilot.press('m')
                self.assertIsInstance(app.screen, Menu)
                await pilot.press('escape')
                self.assertIs(app.screen, screen)
                await app.pop_screen()
            await self.choose(app, pilot, 'screen_settings')
            self.assertIsInstance(app.screen, ScreenForm)
            await pilot.press('escape')
            self.assertIs(app.screen, app.overview)

    async def test_menu_commands_editors_help_and_quit(self):
        from unittest.mock import patch
        from lightkeeper.ui.agent import AgentPanel
        app = self.app()
        async with app.run_test(size=(160, 45)) as pilot:
            for name in ('settings.toml', 'rules.md', 'template.md'):
                with patch.object(AgentPanel, 'action_edit') as edit:
                    await self.choose(app, pilot, name)
                    self.assertIsInstance(app.screen, AgentPanel)
                    edit.assert_called_once_with(name)
            await pilot.press('1')
            for key in ('wake', 'check_updates'):
                await self.choose(app, pilot, key)
                command = app.worker.runner.commands.get_nowait()
                self.assertEqual(command.kind, key)
                app.worker.runner.commands.complete(command)
                app.observer('command_done', id=command.id, outcome='done', text='manual check result')
                app.refresh_snapshot()
                self.assertEqual(app.query_one('#footer', Static).content.plain.count('manual check result'), 1)
            await self.choose(app, pilot, 'help')
            self.assertTrue(app.help_visible)
            await self.choose(app, pilot, 'quit')
            app.worker.request_stop.assert_called_once_with()

    async def test_menu_navigation_preserves_forms_and_confirmation_callbacks(self):
        from lightkeeper.settings import load_settings, settings_digest
        from lightkeeper.ui.repos import Confirm, RepoForm
        from textual.widgets import Input
        app = self.app()
        async with app.run_test(size=(160, 45)) as pilot:
            form = RepoForm(load_settings(self.root), settings_digest(self.root))
            await app.push_screen(form)
            field = form.query_one(Input)
            field.value = 'org/typed-repo'
            await pilot.press('tab')
            for key in ('overview', 'repositories', 'agent', 'settings.toml', 'rules.md', 'template.md'):
                await self.choose(app, pilot, key)
                self.assertIs(app.screen, form)
                self.assertEqual(field.value, 'org/typed-repo')
                self.assertIn('Close the current dialog', app.message)
            for key in ('1', '2', '3'):  # Number keys over the open menu must not discard the form either.
                await pilot.press('m')
                await pilot.press(key)
                self.assertIn(form, app.screen_stack)
                self.assertEqual(field.value, 'org/typed-repo')
                await pilot.press('escape')
            await pilot.press('escape')
            callback = Mock()
            confirm = Confirm('Approve this change?')
            await app.push_screen(confirm, callback)
            await self.choose(app, pilot, 'overview')
            self.assertIs(app.screen, confirm)
            callback.assert_not_called()
            await pilot.press('escape')
            callback.assert_called_once_with(False)

    async def test_screen_form_saves_only_changed_keys_and_applies_picture(self):
        from unittest.mock import patch
        from textual.widgets import Button, Checkbox, Select
        from lightkeeper.settings import load_settings, save_settings
        from lightkeeper.ui.idle import Look
        from lightkeeper.ui.rich_view import idle_panel
        from tests.test_textual import wait_until
        (self.root / 'settings.toml').write_text('[identity]\nlogin="reviewer"\n[screen]\nidle="quiet"\n')
        original = load_settings(self.root)
        app = self.app(idle='quiet')
        async with app.run_test(size=(160, 50)) as pilot:
            self.assertTrue(app.query_one('#idle').display)
            await self.choose(app, pilot, 'screen_settings')
            app.screen.query_one('#idle-setting', Select).value = 'always'
            app.screen.query_one('#tempo', Select).value = 1.2
            for key in ('clock', 'moon', 'events', 'updates'):
                app.screen.query_one(f'#{key}', Checkbox).value = False
            with patch('lightkeeper.ui.menu.save_settings', wraps=save_settings) as save:
                app.screen.query_one('#save', Button).press()
                await wait_until(lambda: app.screen is app.overview)
                save.assert_called_once()
                self.assertEqual(save.call_args.args[1], {'screen': {'idle': 'always', 'tempo': 1.2,
                                 'clock': False, 'moon': False, 'events': False, 'updates': False}})
            saved = load_settings(self.root)
            self.assertEqual(replace(saved, screen=original.screen), original)
            self.assertEqual(app.snapshot.runtime.tempo, 1.2)
            await pilot.pause()
            self.assertTrue(app.query_one('#idle-above').display)
            self.assertFalse(app.query_one('#idle').display)
            self.assertTrue(app._idle_timer._active.is_set())
            with patch('lightkeeper.ui.textual_app.idle_panel', wraps=idle_panel) as render:
                app.refresh_idle(app.snapshot, 100, 160, force=True)
                self.assertEqual(render.call_args.kwargs['look'], Look(1.2, False, False, False))
            # A pending preview keeps snapshot identity stable, so timed refreshes stay cheap.
            self.assertIs(app.snapshot, app.snapshot)
            app.refresh_snapshot()
            with patch.object(app, 'rebuild_rows', wraps=app.rebuild_rows) as rebuild:
                app.refresh_snapshot()
                app.refresh_snapshot()
                rebuild.assert_not_called()
            # A newer settings reload wins even if an editor changed the saved values again.
            from dataclasses import asdict
            app.observer('policy', **asdict(replace(saved.screen, tempo=1.0)))
            app.refresh_snapshot()
            self.assertIsNone(app._screen_settings)
            self.assertEqual(app.snapshot.runtime.tempo, 1.0)
            app.observer('policy', idle='quiet')
            app.refresh_snapshot()
            self.assertTrue(app.query_one('#idle').display)
            self.assertFalse(app.query_one('#idle-above').display)
            await self.choose(app, pilot, 'screen_settings')
            with patch('lightkeeper.ui.menu.save_settings') as save:
                app.screen.query_one('#save', Button).press()
                await pilot.pause()
                save.assert_not_called()

    async def test_screen_form_conflict_and_cancel_preserve_disk(self):
        from textual.widgets import Button, Select
        from tests.test_textual import wait_until
        app = self.app()
        async with app.run_test(size=(160, 45)) as pilot:
            await self.choose(app, pilot, 'screen_settings')
            app.screen.query_one('#tempo', Select).value = 1.5
            path = self.root / 'settings.toml'
            changed = path.read_text() + '# external edit\n'
            path.write_text(changed)
            app.screen.query_one('#save', Button).press()
            await wait_until(lambda: bool(app.screen.message))
            self.assertIn('changed on disk', contents(app))
            self.assertEqual(path.read_text(), changed)
            await pilot.press('escape')
            self.assertIs(app.screen, app.overview)
            self.assertEqual(path.read_text(), changed)

    async def test_default_idle_picture_above_watching_and_timer_follows_menu(self):
        from lightkeeper.settings import ScreenSettings, load_settings
        from lightkeeper.migrate import settings_toml
        self.assertEqual(ScreenSettings().idle, 'always')
        self.assertEqual(Runtime().idle, 'always')
        self.assertIn('idle = "always"', settings_toml(load_settings(self.root)))
        app = self.app()
        async with app.run_test(size=(160, 45)) as pilot:
            self.assertTrue(app.query_one('#idle-above').display)
            self.assertFalse(app.query_one('#idle').display)
            self.assertTrue(app._idle_timer._active.is_set())
            await pilot.pause()
            self.assertEqual(app.query_one('#idle-above').region.bottom,
                             app.query_one('#watching-heading').region.y)
            await pilot.press('m')
            self.assertFalse(app._idle_timer._active.is_set())
            await pilot.press('m')
            self.assertTrue(app._idle_timer._active.is_set())

    async def test_one_changed_key_does_not_materialize_other_changes(self):
        from unittest.mock import patch
        from textual.widgets import Button, Checkbox
        from lightkeeper.settings import save_settings
        from tests.test_textual import wait_until
        app = self.app()
        async with app.run_test(size=(160, 45)) as pilot:
            await self.choose(app, pilot, 'screen_settings')
            app.screen.query_one('#updates', Checkbox).value = False
            with patch('lightkeeper.ui.menu.save_settings', wraps=save_settings) as save:
                app.screen.query_one('#save', Button).press()
                await wait_until(lambda: app.screen is app.overview)
                self.assertEqual(save.call_args.args[1], {'screen': {'updates': False}})
