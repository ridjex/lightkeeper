"""Release CLI entry points use only isolated local state and fake executables."""
from importlib.resources import files
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lightkeeper import __main__ as cli, __version__
from lightkeeper.github import Gh
from tests.helpers import isolate_cli


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        isolate_cli(self, self.root)
        self.out = self.enterContext(patch('sys.stdout', new_callable=io.StringIO))
        self.err = self.enterContext(patch('sys.stderr', new_callable=io.StringIO))
        # No test can accidentally invoke the user's authenticated CLI.
        self.enterContext(patch.object(cli, 'Gh', side_effect=lambda: Gh(runner=self.missing)))
        self.model = self.enterContext(patch.object(cli, 'ClaudeReviewer'))

    @staticmethod
    def missing(*args, **kwargs):
        raise FileNotFoundError('gh')

    def test_notifications_skip_subprocess_on_non_macos(self):
        with patch('sys.platform', 'linux'), patch.object(cli.subprocess, 'run') as run, patch.object(cli, 'log') as log:
            cli.macos_notify('Title', 'Text')
        run.assert_not_called()
        log.assert_not_called()

    def test_help_and_version_without_executables(self):
        for flag in ('--help', '--version'):
            with self.assertRaises(SystemExit) as error:
                cli.main([flag])
            self.assertEqual(error.exception.code, 0)
        self.assertIn(__version__, self.out.getvalue())
        self.model.assert_not_called()

    def test_init_locations_permissions_and_no_overwrite(self):
        for option in ('default', 'environment', 'argument'):
            with self.subTest(option=option):
                env_config = self.root / 'environment'
                with patch.dict(os.environ, LIGHTKEEPER_CONFIG=str(env_config)):
                    args = ['--init']
                    if option == 'default':
                        del os.environ['LIGHTKEEPER_CONFIG']
                        config = self.root / '.config/lightkeeper'
                    elif option == 'argument':
                        config = self.root / 'argument'
                        args += ['--config-dir', str(config)]
                    else:
                        config = env_config
                    self.assertEqual(cli.main(args), 0)
                    self.assertEqual(config.stat().st_mode & 0o777, 0o700)
                    for name in ('settings.toml', 'rules.md'):
                        path = config / name
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                        self.assertEqual(path.read_bytes(), files('lightkeeper').joinpath('templates', name).read_bytes())
                        self.assertIn(f'{path}: created', self.out.getvalue())
                        path.write_text('user content', encoding='utf-8')
                    self.assertEqual(cli.main(args), 0)
                    for name in ('settings.toml', 'rules.md'):
                        path = config / name
                        self.assertEqual(path.read_text(encoding='utf-8'), 'user content')
                        self.assertIn(f'{path}: already existed', self.out.getvalue())
                    (config / 'rules.md').unlink()
                    self.assertEqual(cli.main(args), 0)
                    self.assertEqual((config / 'settings.toml').read_text(encoding='utf-8'), 'user content')
        self.model.assert_not_called()

    def test_missing_gh_startup_and_check_repo_exit_two(self):
        (self.root / 'settings.toml').write_text('[identity]\nlogin="me"\n', encoding='utf-8')
        for args in (['--check-repo', 'org/repo'], ['--plain', '--once', '--config-dir', str(self.root)]):
            with self.subTest(args=args), patch.object(cli, 'configure_diagnostics'):
                self.assertEqual(cli.main(args), 2)
                self.assertIn('gh CLI not found on PATH; install GitHub CLI and run gh auth login', self.err.getvalue())
        self.model.assert_not_called()

    def test_broken_gh_reports_startup_error(self):
        with patch.object(cli, 'Gh', return_value=Gh(runner=lambda *a, **kw: self.denied())):
            self.assertEqual(cli.main(['--check-repo', 'org/repo']), 2)
        self.assertIn('gh CLI could not start:', self.err.getvalue())

    @staticmethod
    def denied():
        raise PermissionError('gh is not executable')

    def test_interrupted_init_leaves_no_partial_file(self):
        config = self.root / 'config'
        with patch('lightkeeper.settings.os.fchmod', side_effect=OSError('disk full')):
            self.assertEqual(cli.main(['--init', '--config-dir', str(config)]), 2)
        self.assertEqual(list(config.iterdir()), [])
        self.assertEqual(cli.main(['--init', '--config-dir', str(config)]), 0)

    def test_status_ignores_cwd_legacy_policy_without_executables(self):
        (self.root / 'policy.toml').write_text('rules="missing.md"', encoding='utf-8')
        (self.root / '.config/lightkeeper').mkdir(parents=True)
        self.assertEqual(cli.main(['--status']), 0)
        self.assertFalse((self.root / '.config/lightkeeper/migration.json').exists())
        self.model.assert_not_called()

    def test_preview_without_ui_extra(self):
        with patch.dict('sys.modules', {'lightkeeper.ui.preview': None}), patch.object(self.out, 'isatty', return_value=True):
            self.assertEqual(cli.main(['--preview-idle']), 2)
        self.assertIn('install lightkeeper[ui] to use --preview-idle', self.err.getvalue())

    def test_preview_requires_a_terminal(self):
        self.assertEqual(cli.main(['--preview-idle']), 2)
        self.assertIn('the preview needs an interactive terminal', self.err.getvalue())
        self.model.assert_not_called()

    def test_kitty_workaround_overrides_inherited_setting(self):
        result = subprocess.run([sys.executable, '-c',
                                 'import lightkeeper.__main__, os; print(os.environ["TEXTUAL_DISABLE_KITTY_KEY"])'],
                                env={**os.environ, 'TEXTUAL_DISABLE_KITTY_KEY': '0'},
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), '1')

    def test_once_creates_private_session_and_journal_with_raw_artifacts(self):
        from lightkeeper.reviewer import ClaudeReviewer
        from tests.helpers import FakeGh, make_pr
        from tests.test_reviewer import FakeRunner
        fixture = Path(__file__).parent / 'fixtures/engines/claude.json'
        envelope = json.loads(fixture.read_text())['success']
        engine = FakeRunner(stdout=json.dumps(envelope))
        self.model.side_effect = lambda *args, **kwargs: ClaudeReviewer(*args, **kwargs, runner=engine)
        config = self.root / 'config'
        config.mkdir()
        (config / 'settings.toml').write_text('[identity]\nlogin="me"\n[screen]\nupdates=false\n[repos."org/repo"]\n')
        (config / 'rules.md').write_text('Review carefully.')
        remote = 'Title\x1b]52;c;cGF5bG9hZA==\x07'
        gh = FakeGh()
        gh.pr_snapshots[('org/repo', 7)] = [make_pr(title=remote)]
        state = self.root / 'state'
        previous = os.umask(0o022)
        try:
            with patch.object(cli, 'Gh', return_value=gh), patch.object(cli, 'configure_diagnostics'):
                self.assertEqual(cli.main(['--once', '--plain', '--config-dir', str(config), '--state-dir', str(state)]), 0)
        finally:
            os.umask(previous)
        prompt, = (state / 'sessions').glob('*/prompt.md')
        journal = state / 'journal.jsonl'
        for path in (prompt, journal):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(prompt.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn(remote, prompt.read_text())
        self.assertTrue(any(record.get('title') == remote for record in map(json.loads, journal.read_text().splitlines())))
        self.assertNotIn('\x1b', self.out.getvalue())
        self.assertNotIn('\x1b', (state / 'run.log').read_text())

    def test_status_does_not_resume_recorded_migration(self):
        from lightkeeper.migrate import migrate
        source = self.root / 'legacy.toml'
        source.write_text('login="me"\nrepos=["org/repo"]\nrules="legacy.md"\n', encoding='utf-8')
        (self.root / 'legacy.md').write_text('Review carefully.', encoding='utf-8')
        config = self.root / 'config'
        with patch('lightkeeper.migrate.load_settings', side_effect=OSError('crash')):
            with self.assertRaises(OSError):
                migrate(source, config)
        (config / 'rules.md').unlink()
        before = (config / 'migration.json').read_bytes()
        self.assertEqual(cli.main(['--status', '--config-dir', str(config)]), 0)
        self.assertEqual((config / 'migration.json').read_bytes(), before)
        self.assertFalse((config / 'rules.md').exists())

    def test_status_leaves_files_and_modes_unchanged_and_does_not_migrate(self):
        config = self.root / 'config'
        config.mkdir()
        (config / 'settings.toml').write_text('[identity]\nlogin="me"\n')
        (config / 'rules.md').write_text('Rules')
        (config / 'repos').mkdir()
        (config / 'repos/org__repo.md').write_text('Notes')
        source = self.root / 'policy.toml'
        source.write_text('login="me"\nrepos=["org/repo"]\n')
        state = self.root / 'state'
        state.mkdir()
        (state / 'journal.jsonl').write_text('{"event":"notify","kind":"pr","key":["org/repo",7,"head"],"text":"Example"}\n')
        for path in [config, state, *config.rglob('*'), *state.rglob('*')]:
            path.chmod(0o750 if path.is_dir() else 0o640)
        def snapshot():
            return {p.relative_to(self.root): (p.stat().st_mode, p.read_bytes() if p.is_file() else None)
                    for p in self.root.rglob('*')}
        for settings_present in (True, False):
            with self.subTest(settings_present=settings_present):
                if not settings_present:
                    (config / 'settings.toml').unlink()
                before = snapshot()
                self.assertEqual(cli.main(['--status', '--config-dir', str(config), '--state-dir', str(state),
                                           '--policy', str(source)]), 0)
                self.assertEqual(snapshot(), before)
                self.assertFalse((config / 'migration.json').exists())

    def test_status_missing_config_directory_is_a_read_only_error(self):
        config = self.root / 'missing'
        self.assertEqual(cli.main(['--status', '--config-dir', str(config)]), 2)
        self.assertIn('configuration directory does not exist', self.err.getvalue())
        self.assertFalse(config.exists())


class RulesCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        isolate_cli(self, self.root)
        self.config = self.root / 'config'
        self.config.mkdir(mode=0o700)
        (self.config / 'settings.toml').write_text('[identity]\nlogin = "me"\n\n[repos."org/repo"]\n')

    def test_form_validation_names_the_real_rules_file(self):
        from lightkeeper.settings import SettingsError, validate_settings_file
        with self.assertRaises(SettingsError) as error:
            validate_settings_file(self.config / 'settings.toml', self.config)
        self.assertEqual(error.exception.file, self.config / 'rules.md')

    def test_status_needs_no_rules(self):
        out = self.enterContext(patch('sys.stdout', new_callable=io.StringIO))
        self.enterContext(patch('sys.stderr', new_callable=io.StringIO))
        state = self.root / 'state'
        self.assertEqual(cli.main(['--status', '--config-dir', str(self.config), '--state-dir', str(state)]), 0)
        self.assertIn('"records": 0', out.getvalue())
