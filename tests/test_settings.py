from dataclasses import FrozenInstanceError
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lightkeeper.settings import (ACTIONS, AgentSettings, CommentSettings, RepoSettings,
                                        SettingsError, judgement, load_settings, prepare_config_dir)

MINIMUM = '[identity]\nlogin = "octocat"\n'
REPO = '[repos."example/widget"]\n'


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "config"
        self.config.mkdir()
        self.file = self.config / "settings.toml"

    def load(self, text=MINIMUM):
        self.file.write_text(text, encoding="utf-8")
        return load_settings(self.config)

    def test_defaults_and_frozen_records(self):
        (self.config / "rules.md").write_text("Review carefully.", encoding="utf-8")
        settings = self.load(MINIMUM + REPO)
        self.assertEqual(settings.login, "octocat")
        self.assertEqual(settings.agent, AgentSettings("claude", "opus", "medium", None, 600))
        self.assertEqual(settings.comment, CommentSettings("en", True))
        self.assertEqual(settings.repo("example/widget"), RepoSettings("example/widget"))
        self.assertEqual(settings.repo("example/widget").allowed, ACTIONS)
        self.assertEqual((settings.rules, settings.template, settings.fresh), ("Review carefully.", "", False))
        self.assertEqual(settings.config_dir, self.config.resolve())
        with self.assertRaises(KeyError):
            settings.repo("example/absent")
        for obj in (settings, settings.agent, settings.comment, settings.repo("example/widget")):
            with self.assertRaises(FrozenInstanceError):
                obj.unexpected = True

    def test_login_is_safe_for_state_and_lock_paths(self):
        for login in ('..', '../outside', '/tmp/outside', 'a_b', 'a.b', 'a' * 40, 'me user', 'é'):
            with self.subTest(login=login), self.assertRaisesRegex(SettingsError, 'identity.login'):
                self.load(f'[identity]\nlogin = "{login}"\n')
        for login in ('octocat', 'A-9', 'a' * 39):
            self.assertEqual(self.load(f'[identity]\nlogin = "{login}"\n').login, login)

    def test_enabled_repositories_require_rules_but_onboarding_does_not(self):
        rules = self.config / "rules.md"
        for content in (None, "", " \n\t"):
            with self.subTest(content=content):
                if content is not None:
                    rules.write_text(content, encoding="utf-8")
                self.load(MINIMUM)
                self.load(MINIMUM + REPO + 'enabled = false\n')
                with self.assertRaisesRegex(SettingsError, 'lightkeeper --init') as error:
                    self.load(MINIMUM + REPO)
                self.assertEqual(error.exception.file, rules)

    def test_explicit_disclosure_setting_is_preserved(self):
        for value in (True, False):
            settings = self.load(MINIMUM + '[comment]\ndisclose = ' + str(value).lower() + '\n')
            self.assertEqual(settings.comment.disclose, value)

    def test_screen_idle_setting_validates_and_survives_form_saves(self):
        from lightkeeper.settings import save_settings, settings_digest
        self.assertEqual(self.load().screen.idle, 'always')
        self.assertEqual(load_settings(self.config).screen.idle, 'always')
        self.assertEqual(self.load(MINIMUM + '[screen]\nidle="quiet"\n').screen.idle, 'quiet')
        self.assertEqual(self.load(MINIMUM + '[screen]\nidle="always"\n').screen.idle, 'always')
        saved = save_settings(self.config, {'comment': {'disclose': True}}, settings_digest(self.config))
        self.assertEqual(saved.settings.screen.idle, 'always')
        self.assertEqual(load_settings(self.config).screen.idle, 'always')
        for value in ('"sometimes"', 'true', '1'):
            with self.assertRaises(SettingsError) as error:
                self.load(MINIMUM + '[screen]\nidle=' + value)
            self.assertEqual(error.exception.key, 'screen.idle')

    def test_screen_tempo_load_validate_serialise_and_preserve(self):
        from lightkeeper.migrate import settings_toml
        from lightkeeper.settings import save_settings, settings_digest
        self.assertEqual(load_settings(self.config).screen.tempo, .8)
        self.assertEqual(self.load().screen.tempo, .8)
        for value in (.2, .8, 1.2, 1.5):
            settings = self.load(MINIMUM + f'[screen]\ntempo={value}\n')
            self.assertEqual(settings.screen.tempo, value)
            self.assertIn(f'tempo = {value}', settings_toml(settings))
            saved = save_settings(self.config, {'comment': {'disclose': True}}, settings_digest(self.config))
            self.assertEqual(saved.settings.screen.tempo, value)
            self.assertEqual(load_settings(self.config).screen.tempo, value)
        self.assertEqual(self.load(MINIMUM + '[screen]\ntempo=1\n').screen.tempo, 1.)
        for value in ('0.1', '1.6', 'nan', 'inf', '-inf', 'true', '"0.8"'):
            with self.subTest(value=value), self.assertRaises(SettingsError) as error:
                self.load(MINIMUM + '[screen]\ntempo=' + value)
            self.assertEqual(error.exception.key, 'screen.tempo')

    def test_screen_booleans_load_validate_serialise_and_preserve(self):
        from lightkeeper.migrate import settings_toml
        from lightkeeper.settings import save_settings, settings_digest
        for name in ('clock', 'moon', 'events', 'updates'):
            self.assertIs(getattr(load_settings(self.config).screen, name), True)
            self.assertIs(getattr(self.load().screen, name), True)
            for value in (True, False):
                with self.subTest(name=name, value=value):
                    settings = self.load(MINIMUM + f'[screen]\n{name}={str(value).lower()}\n')
                    self.assertIs(getattr(settings.screen, name), value)
                    self.assertIn(f'{name} = {str(value).lower()}', settings_toml(settings))
                    saved = save_settings(self.config, {'comment': {'disclose': True}}, settings_digest(self.config))
                    self.assertIs(getattr(saved.settings.screen, name), value)
                    self.assertIs(getattr(load_settings(self.config).screen, name), value)
            for value in ('"yes"', '1', '"true"', '0', '[]'):
                with self.subTest(name=name, invalid=value), self.assertRaises(SettingsError) as error:
                    self.load(MINIMUM + f'[screen]\n{name}={value}\n')
                self.assertEqual(error.exception.key, f'screen.{name}')
            self.file.unlink()

    def test_fresh_install_and_config_locations(self):
        fresh_dir = self.config / "fresh"
        with patch.dict(os.environ, {"LIGHTKEEPER_CONFIG": str(fresh_dir)}):
            settings = load_settings()
            self.assertTrue(settings.fresh)
            self.assertEqual((settings.login, settings.repos, settings.notes), ("", {}, {}))
            self.assertFalse((fresh_dir / "settings.toml").exists())
            self.assertFalse(fresh_dir.exists())
            self.assertEqual(load_settings(self.config).config_dir, self.config)
        with patch.dict(os.environ, {"HOME": self.temp.name}):
            with patch.dict(os.environ):
                os.environ.pop("LIGHTKEEPER_CONFIG", None)
                self.assertEqual(load_settings().config_dir, self.root / ".config/lightkeeper")

    def test_validation(self):
        cases = [
            ("broken = [", "toml"), ("", "identity.login"),
            ('identity = "octocat"', "identity"),
            ('[identity]\nlogin = 1', "identity.login"),
            ('[identity]\nlogin = " "', "identity.login"),
            ('unknown = true\n' + MINIMUM, "unknown"),
            (MINIMUM + 'extra = 1', "identity.extra"),
            ('agent = []\n' + MINIMUM, "agent"),
            ('comment = false\n' + MINIMUM, "comment"),
            ('repos = []\n' + MINIMUM, "repos"),
        ]
        for table, field, values in [
            ("agent", "engine", ['false', '""']), ("agent", "model", ['1', '" "']),
            ("agent", "effort", ['[]', '""']), ("agent", "profile", ['true', '""']),
            ("agent", "timeout_s", ['true', '0', '-1', '1.5', '"600"']),
            ("agent", "unknown", ['true']), ("comment", "language", ['1', '""']),
            ("comment", "disclose", ['1', '"false"']), ("comment", "unknown", ['true']),
        ]:
            cases.extend((MINIMUM + f'[{table}]\n{field} = {value}', f"{table}.{field}") for value in values)
        for field, values in [
            ("enabled", ['1', '"true"']), ("start", ['"open"', 'true']),
            ("post", ['"preview"', 'false']), ("allowed", ['"approve"', '[1]', '["merge"]']),
            ("remarks", ['"some"', '1']), ("deployments", ['1', '"false"']), ("unknown", ['true']),
        ]:
            cases.extend((MINIMUM + REPO + f"{field} = {value}", f'repos."example/widget".{field}') for value in values)
        for name in ("example", "/widget", "example/", "example/widget/extra", "../widget", "example/..",
                     "example/.", "example/has space", "example/evil\\\\name"):
            cases.append((MINIMUM + f'[repos."{name}"]', "repos."))
        cases.append((MINIMUM + '[repos]\n"example/widget" = 1', 'repos."example/widget"'))
        for text, key in cases:
            with self.subTest(text=text), self.assertRaises(SettingsError) as caught:
                self.load(text)
            self.assertEqual(caught.exception.file, self.file)
            self.assertIn(key, caught.exception.key)
            self.assertTrue(caught.exception.message)
        with self.assertRaisesRegex(SettingsError, "only 'request' is supported"):
            self.load(MINIMUM + REPO + 'start = "any"')

    def test_all_valid_repo_options(self):
        (self.config / "rules.md").write_text("Review carefully.", encoding="utf-8")
        for remarks in ("blockers", "important", "all"):
            for post in ("live", "dry-run"):
                settings = self.load(MINIMUM + REPO + f'allowed = []\nenabled = false\ndeployments = true\n'
                                     f'post = "{post}"\nremarks = "{remarks}"')
                self.assertEqual(settings.repo("example/widget"),
                                 RepoSettings("example/widget", False, "request", post, (), remarks, True))
        self.assertEqual(self.load(MINIMUM + REPO + 'allowed = ["comment"]').repo("example/widget").allowed,
                         ("comment",))

    def test_profile_preserved_and_resolved_at_engine_boundary(self):
        with patch.dict(os.environ, {"HOME": self.temp.name}):
            for value, expected in (("~/profile", Path(self.temp.name) / "profile"),
                                    ("profiles/reviewer", self.config / "profiles/reviewer"),
                                    (str(self.config / "absolute"), self.config / "absolute")):
                with self.subTest(value=value):
                    settings = self.load(MINIMUM + f'[agent]\nprofile = "{value}"')
                    self.assertEqual(settings.agent.profile, value)
                    self.assertEqual(settings.resolved_profile, str(expected.resolve()))

    def test_text_files_notes_and_permissions(self):
        (self.config / "repos").mkdir(mode=0o700)
        texts = {"rules.md": "# Rules\nDo good.\n", "template.md": "{summary}\n",
                 "repos/example__widget.md": "# Notes\nA widget.\n"}
        for name, text in texts.items():
            (self.config / name).write_text(text, encoding="utf-8")
        settings = self.load(MINIMUM + REPO + '[repos."example/gadget"]')
        self.assertEqual((settings.rules, settings.template), (texts["rules.md"], texts["template.md"]))
        self.assertEqual(settings.notes, {"example/widget": texts["repos/example__widget.md"], "example/gadget": ""})
        prepare_config_dir(self.config)
        for name in ("settings.toml", *texts):
            self.assertEqual((self.config / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o700)

    def test_screen_patch_validates_and_preserves_other_sections(self):
        from lightkeeper.settings import save_settings, settings_digest
        from dataclasses import replace
        (self.config / "rules.md").write_text("Review carefully.")
        original = self.load(MINIMUM + REPO)
        digest = settings_digest(self.config)
        before = self.file.read_bytes()
        for values in ({"updates": 1}, {"tempo": 2}, {"idle": "never"}, {"unknown": True}):
            with self.subTest(values=values), self.assertRaises(SettingsError):
                save_settings(self.config, {"screen": values}, digest)
            self.assertEqual(self.file.read_bytes(), before)
        saved = save_settings(self.config, {"screen": {"updates": False, "tempo": 1.2}}, digest)
        self.assertEqual(replace(saved.settings, screen=original.screen), original)
        self.assertEqual(saved.settings.screen, replace(original.screen, updates=False, tempo=1.2))

    def test_unreadable_text_is_settings_error(self):
        (self.config / "rules.md").write_bytes(b"\xff")
        with self.assertRaises(SettingsError) as caught:
            load_settings(self.config)
        self.assertEqual(caught.exception.file.name, "rules.md")


class JudgementTests(unittest.TestCase):
    def test_each_component_changes_only_its_digest_and_the_judgement(self):
        values = dict(frame_version="1", rules="rules", notes="notes", remarks="important", schema_text='{"type":"object"}')
        original = judgement(**values)
        self.assertEqual(original, judgement(**dict(reversed(list(values.items())))))
        self.assertRegex(original.hash, r"^[0-9a-f]{12}$")
        for key, value in values.items():
            self.assertEqual(original.components[key], hashlib.sha256(value.encode()).hexdigest()[:8])
            changed = judgement(**{**values, key: value + "!"})
            self.assertNotEqual(original.hash, changed.hash)
            self.assertEqual([k for k in values if original.components[k] != changed.components[k]], [key])

    def test_component_boundaries_and_unicode(self):
        self.assertNotEqual(judgement("1", "ab", "c", "all", "{}"), judgement("1", "a", "bc", "all", "{}"))
        self.assertEqual(judgement("1", "é", "", "all", "{}"), judgement("1", "é", "", "all", "{}"))
