from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from lightkeeper import migrate as migration
from lightkeeper.journal import Journal
from lightkeeper.migrate import legacy_policy_hash, migrate, rehash, settings_toml
from lightkeeper.policy import PolicyError, load_policy
from lightkeeper.settings import SettingsError, load_settings

FIXTURE = Path(__file__).parent / "fixtures/migration"


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "legacy"
        shutil.copytree(FIXTURE, self.source)
        self.policy = self.source / "policy.toml"
        (self.source / "policy.example.toml").rename(self.policy)
        self.config = self.root / "config"

    def source_snapshot(self):
        return {p.relative_to(self.source): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                for p in self.source.rglob("*") if p.is_file()}

    def interrupt_migration(self):
        with patch.object(migration, 'load_settings', side_effect=OSError('interrupted')):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                migrate(self.policy, self.config)

    def test_resume_uses_recorded_source_despite_new_policy_argument(self):
        self.interrupt_migration()
        (self.config / 'rules.md').unlink()
        report = migrate(self.root / 'unrelated-policy.toml', self.config)
        self.assertEqual(report.policy_path, str(self.policy))
        self.assertEqual((self.config / 'rules.md').read_bytes(), (self.source / 'RULES.md').read_bytes())

    def test_resume_accepts_new_serialized_default_with_unchanged_inputs(self):
        write_file = migration._write_file
        def interrupt_settings(path, content, **kwargs):
            if path.name == 'settings.toml':
                raise OSError('interrupted')
            return write_file(path, content, **kwargs)
        def old_serializer(settings):
            return settings_toml(settings).replace('updates = true\n', '')
        with patch.object(migration, 'settings_toml', side_effect=old_serializer), \
             patch.object(migration, '_write_file', side_effect=interrupt_settings), \
             self.assertRaisesRegex(OSError, 'interrupted'):
            migrate(self.policy, self.config)
        marker = self.config / 'migration.json'
        original = json.loads(marker.read_text())
        # Even a second interruption must leave the refreshed digest ready for the next resume.
        with patch.object(migration, '_write_file', side_effect=interrupt_settings), \
             self.assertRaisesRegex(OSError, 'interrupted'):
            migrate(None, self.config)
        refreshed = json.loads(marker.read_text())
        self.assertNotEqual(original['files']['settings.toml'], refreshed['files']['settings.toml'])
        self.assertEqual(original['policy_sha256'], refreshed['policy_sha256'])
        migrate(None, self.config)
        self.assertIn('updates = true', (self.config / 'settings.toml').read_text())
        completed = json.loads(marker.read_text())
        self.assertTrue(completed['done'])
        for name, digest in completed['files'].items():
            self.assertEqual(hashlib.sha256((self.config / name).read_bytes()).hexdigest(), digest)

    def test_resume_verifies_both_sources_even_when_destinations_are_complete(self):
        for name in ('policy.toml', 'RULES.md'):
            with self.subTest(name=name):
                self.interrupt_migration()
                source = self.source / name
                original = source.read_bytes()
                source.write_bytes(original + b'\n# changed\n')
                with self.assertRaisesRegex(SettingsError, 'input hashes changed'):
                    migrate(None, self.config)
                source.write_bytes(original)
                source.rename(source.with_suffix('.moved'))
                with self.assertRaisesRegex(SettingsError, 'recorded source'):
                    migrate(None, self.config)
                source.with_suffix('.moved').rename(source)
                migrate(None, self.config)
                shutil.rmtree(self.config)

    def test_frozen_hash_matches_legacy_loader(self):
        raw = tomllib.loads(self.policy.read_text())
        rules = (self.source / "RULES.md").read_text()
        digest = legacy_policy_hash(raw, rules)
        self.assertRegex(digest, r"^[0-9a-f]{6}$")
        self.assertEqual(digest, load_policy(self.policy).policy_hash)
        with patch("lightkeeper.policy.SEMANTICS_VERSION", "future"):
            self.assertEqual(digest, legacy_policy_hash(raw, rules))
            self.assertNotEqual(digest, load_policy(self.policy).policy_hash)
        for changed, text in (({**raw, "model": "sonnet"}, rules), (raw, rules + "\n")):
            self.assertNotEqual(digest, legacy_policy_hash(changed, text))

    def test_happy_path_provenance_permissions_and_sources_untouched(self):
        before = self.source_snapshot()
        report = migrate(self.policy, self.config)
        settings = load_settings(self.config)
        policy = load_policy(self.policy)
        self.assertFalse(settings.fresh)
        self.assertEqual(settings.login, "octocat")
        self.assertEqual((settings.agent.engine, settings.agent.model, settings.agent.effort,
                          settings.agent.profile, settings.agent.timeout_s),
                         ("claude", "opus", "high", str(self.source / "profiles/reviewer"), 600))
        self.assertEqual(tuple(settings.repos), policy.repos)
        for repo in settings.repos.values():
            self.assertEqual((repo.enabled, repo.start, repo.post, repo.allowed, repo.remarks, repo.deployments),
                             (True, "request", "live", ("approve", "request_changes", "comment"), "important", True))
        self.assertEqual((self.config / "rules.md").read_bytes(), (self.source / "RULES.md").read_bytes())
        self.assertEqual(settings.rules, policy.rules)
        marker = json.loads((self.config / "migration.json").read_text())
        self.assertEqual(set(marker), {"policy_path", "policy_sha256", "rules_sha256", "legacy_hash", "created",
                                       "done", "files", "baseline_judgements"})
        self.assertEqual(marker["baseline_judgements"],
                         {name: settings.judgement(name).hash for name in settings.repos})
        self.assertIs(marker["done"], True)
        self.assertEqual(marker["files"], {name: hashlib.sha256((self.config / name).read_bytes()).hexdigest()
                                          for name in ("settings.toml", "rules.md")})
        self.assertEqual(marker["policy_path"], str(self.policy))
        self.assertEqual(marker["policy_sha256"], hashlib.sha256(self.policy.read_bytes()).hexdigest())
        self.assertEqual(marker["rules_sha256"], hashlib.sha256((self.source / "RULES.md").read_bytes()).hexdigest())
        self.assertEqual(marker["legacy_hash"], policy.policy_hash)
        self.assertRegex(report.created, r"^\d{4}-\d{2}-\d{2}T.*Z$")
        self.assertEqual([p.name for p in report.written], ["settings.toml", "rules.md", "migration.json"])
        self.assertIn("repos/<org>__<name>.md", str(report))
        for file in report.written:
            self.assertIn(str(file), str(report))
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.source_snapshot(), before)
        self.assertFalse(list(self.config.glob("*.partial")))

    def test_minimum_policy_and_custom_rules_path(self):
        self.policy.write_text('login = "octocat"\nrepos = ["example/widget"]\nrules = "guides/custom.md"\n')
        rules = self.source / "guides/custom.md"
        rules.parent.mkdir()
        rules.write_bytes(b"# Custom rules\r\nKeep byte-for-byte.\r\n")
        (self.source / "RULES.md").unlink()
        before = self.source_snapshot()
        report = migrate(self.policy, self.config)
        self.assertEqual((self.config / "rules.md").read_bytes(), rules.read_bytes())
        self.assertEqual(report.rules_sha256, hashlib.sha256(rules.read_bytes()).hexdigest())
        self.assertEqual(report.legacy_hash, load_policy(self.policy).policy_hash)
        settings = load_settings(self.config)
        self.assertEqual(settings.repo("example/widget").post, "dry-run")
        self.assertIsNone(settings.agent.profile)
        self.assertEqual(self.source_snapshot(), before)

    def test_toml_writer_round_trip_including_escaping(self):
        migrate(self.policy, self.config)
        original = load_settings(self.config)
        settings = replace(original, agent=replace(original.agent, model='quote" backslash\\ tab\t newline\n'
                                                  ' carriage\r form\f back\b null\x00 del\x7f é 🐙'))
        text = settings_toml(settings)
        raw = tomllib.loads(text)
        self.assertEqual(raw["agent"]["model"], settings.agent.model)
        self.assertEqual(raw["agent"]["timeout_s"], 600)
        self.assertEqual(raw["repos"]["example/widget"]["allowed"], ["approve", "request_changes", "comment"])
        self.assertIs(raw["repos"]["example/widget"]["deployments"], True)
        self.assertIn('[repos."example/widget"]', text)
        (self.config / "settings.toml").write_text(text)
        self.assertEqual(settings, load_settings(self.config))

    def test_abandoned_partial_at_each_destination_is_retryable(self):
        for name in ("settings.toml", "rules.md", "migration.json"):
            with self.subTest(name=name):
                config = self.root / name
                config.mkdir()
                partial = config / f"{name}.{os.getpid()}.partial"
                partial.symlink_to(self.policy)          # a stale symlink must not be followed
                other_partial = config / f"{name}.{os.getpid() + 1}.partial"
                other_partial.write_text("another migration")
                migrate(self.policy, config)
                self.assertFalse(partial.exists())
                self.assertEqual(other_partial.read_text(), "another migration")
                self.assertEqual(load_settings(config).login, "octocat")

    def test_crash_at_every_publication_boundary_resumes_without_rewriting_files(self):
        before = self.source_snapshot()
        operations = (("os.fsync", os.fsync, 4), ("os.link", os.link, 3),
                      ("os.replace", os.replace, 1), ("load_settings", load_settings, 1))
        for operation, original, calls in operations:
            for step in range(1, calls + 1):
                for after in (False, True):
                    config = self.root / f"{operation}-{step}-{after}"
                    count = 0

                    def crash(*args, **kwargs):
                        nonlocal count
                        count += 1
                        if count == step and not after:
                            raise OSError("simulated crash")
                        result = original(*args, **kwargs)
                        if count == step and after:
                            raise OSError("simulated crash")
                        return result

                    with self.subTest(operation=operation, step=step, after=after):
                        with patch(f"lightkeeper.migrate.{operation}", crash), self.assertRaisesRegex(OSError, "simulated crash"):
                            migrate(self.policy, config)
                        published = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
                                     for p in (config / "settings.toml", config / "rules.md") if p.exists()}
                        marker = config / "migration.json"
                        manifest = json.loads(marker.read_text()) if marker.exists() else None
                        report = migrate(self.policy, config)
                        self.assertEqual(published, {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
                                                     for p in published})
                        completed = json.loads(marker.read_text())
                        self.assertIs(completed["done"], True)
                        if manifest:
                            self.assertEqual(completed, {**manifest, "done": True})
                        self.assertEqual(load_settings(config).login, "octocat")
                        self.assertEqual(migrate(self.policy, config), report)
        self.assertEqual(self.source_snapshot(), before)

    def test_incomplete_manifest_refuses_named_conflicts_at_each_file_boundary(self):
        link = os.link
        for published_count in range(3):
            for name in ("settings.toml", "rules.md"):
                config = self.root / f"conflict-{published_count}-{name}"
                count = 0

                def crash(partial, target):
                    nonlocal count
                    link(partial, target)
                    count += 1
                    if count == published_count + 1:     # manifest, then zero/one/two destinations
                        raise OSError("simulated crash")

                with self.subTest(published_count=published_count, name=name):
                    with patch("lightkeeper.migrate.os.link", crash), self.assertRaises(OSError):
                        migrate(self.policy, config)
                    (config / name).write_text("keep conflicting bytes")
                    files = {p.name: p.read_bytes() for p in config.iterdir()}
                    with self.assertRaisesRegex(SettingsError, "refusing to overwrite") as caught:
                        migrate(self.policy, config)
                    self.assertEqual(caught.exception.file, config / name)
                    self.assertEqual(files, {p.name: p.read_bytes() for p in config.iterdir()})

    def test_concurrent_destination_creation_is_never_clobbered(self):
        link = os.link
        for name in ("migration.json", "settings.toml", "rules.md"):
            config = self.root / f"race-{name}"

            def race(partial, target):
                self.assertEqual(partial.name, f"{target.name}.{os.getpid()}.partial")
                self.assertTrue(partial.is_file())
                if target.name == name:
                    target.write_bytes(b"concurrently created")
                return link(partial, target)

            with self.subTest(name=name):
                with patch("lightkeeper.migrate.os.link", race), self.assertRaisesRegex(SettingsError, "refusing to overwrite") as caught:
                    migrate(self.policy, config)
                self.assertEqual(caught.exception.file, config / name)
                self.assertEqual((config / name).read_bytes(), b"concurrently created")

    def test_symlinked_policy_uses_given_parent_for_rules_and_profile(self):
        given = self.root / "given"
        given.mkdir()
        policy = given / "policy.toml"
        policy.symlink_to(self.policy)
        rules = given / "RULES.md"
        rules.write_text("# Linked policy rules\nCheck the given directory.\n")
        before = self.source_snapshot()
        report = migrate(policy, self.config)
        settings = load_settings(self.config)
        self.assertEqual(settings.rules, rules.read_text())
        self.assertEqual(settings.agent.profile, str(given / "profiles/reviewer"))
        self.assertEqual(report.legacy_hash, load_policy(policy).policy_hash)
        self.assertEqual(report.rules_sha256, hashlib.sha256(rules.read_bytes()).hexdigest())
        self.assertEqual(report.policy_path, str(self.policy.resolve()))
        self.assertEqual(self.source_snapshot(), before)

    def test_empty_legacy_profile_is_omitted_and_settings_validated_before_completion(self):
        self.policy.write_text(self.policy.read_text().replace('claude_config_dir = "profiles/reviewer"',
                                                              'claude_config_dir = ""'))

        def validate(config):
            self.assertIs(json.loads((config / "migration.json").read_text())["done"], False)
            return load_settings(config)

        with patch("lightkeeper.migrate.load_settings", side_effect=validate) as loader:
            migrate(self.policy, self.config)
        loader.assert_called_once_with(self.config)
        self.assertNotIn("profile", tomllib.loads((self.config / "settings.toml").read_text())["agent"])
        self.assertIsNone(load_settings(self.config).agent.profile)
        self.assertIs(json.loads((self.config / "migration.json").read_text())["done"], True)

    def test_invalid_generated_settings_never_marked_done(self):
        def invalid(settings):
            return settings_toml(replace(settings, agent=replace(settings.agent, timeout_s=0)))

        with patch("lightkeeper.migrate.settings_toml", side_effect=invalid):
            with self.assertRaisesRegex(SettingsError, "agent.timeout_s"):
                migrate(self.policy, self.config)
        marker = self.config / "migration.json"
        self.assertIs(json.loads(marker.read_text())["done"], False)
        with self.assertRaisesRegex(SettingsError, "agent.timeout_s"):
            migrate(self.policy, self.config)
        self.assertIs(json.loads(marker.read_text())["done"], False)

    def test_resume_refuses_changed_legacy_rules_when_missing(self):
        link = os.link

        def crash(partial, target):
            if target.name == "rules.md":
                raise OSError("simulated crash")
            return link(partial, target)

        with patch("lightkeeper.migrate.os.link", crash), self.assertRaises(OSError):
            migrate(self.policy, self.config)
        rules = self.source / "RULES.md"
        rules.write_text(rules.read_text() + "\nRequire an explicit regression test.\n")
        marker = self.config / "migration.json"
        manifest = marker.read_bytes()
        with self.assertRaisesRegex(SettingsError, "legacy input changed") as caught:
            migrate(self.policy, self.config)
        self.assertEqual(caught.exception.file, self.config / "rules.md")
        self.assertFalse((self.config / "rules.md").exists())
        self.assertEqual(marker.read_bytes(), manifest)

    def test_existing_destination_refused_without_any_writes(self):
        for name in ("settings.toml", "rules.md", "template.md", "repos/example__widget.md"):
            with self.subTest(name=name):
                config = self.root / (name.replace("/", "-"))
                target = config / name
                target.parent.mkdir(parents=True)
                target.write_text("keep me")
                with self.assertRaises(SettingsError) as caught:
                    migrate(self.policy, config)
                self.assertEqual(caught.exception.file, target)
                self.assertEqual(target.read_text(), "keep me")
                self.assertEqual([p for p in config.rglob("*") if p.is_file()], [target])

    def test_second_run_reads_only_completion_report(self):
        first = migrate(self.policy, self.config)
        files = {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in self.config.iterdir()}
        shutil.rmtree(self.source)
        with patch("lightkeeper.migrate.prepare_config_dir", side_effect=AssertionError("no writes")):
            self.assertEqual(migrate(self.policy, self.config), first)
        self.assertEqual(files, {p: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in self.config.iterdir()})

    def test_missing_or_invalid_legacy_input_writes_nothing(self):
        for content in ("", " ", None):
            with self.subTest(content=content):
                rules = self.source / "RULES.md"
                if content is None:
                    rules.unlink()
                else:
                    rules.write_text(content)
                with self.assertRaises(PolicyError):
                    migrate(self.policy, self.config)
                self.assertFalse(self.config.exists())

    def test_invalid_marker_refuses(self):
        self.config.mkdir()
        marker = self.config / "migration.json"
        marker.write_text("{}")
        with self.assertRaises(SettingsError) as caught:
            migrate(self.policy, self.config)
        self.assertEqual(caught.exception.file, marker)
        self.assertEqual(marker.read_text(), "{}")

    def test_old_manifest_reconstructs_only_unchanged_migration_inputs(self):
        migrate(self.policy, self.config)
        settings = load_settings(self.config)
        marker = self.config / "migration.json"
        manifest = json.loads(marker.read_text())
        baselines = manifest.pop("baseline_judgements")
        marker.write_text(json.dumps(manifest))
        # New notes must not become part of a reconstructed migration baseline.
        (self.config / "repos").mkdir(exist_ok=True)
        (self.config / "repos/example__widget.md").write_text("New repository rules")
        result = rehash(load_settings(self.config), Journal(self.root / "unchanged/journal.jsonl"))
        self.assertEqual(result["judgements"], baselines)
        for name in ("rules.md", "settings.toml"):
            path = self.config / name
            original = path.read_text()
            path.write_text(original + "\n# Edited after migration\n")
            journal = Journal(self.root / name / "journal.jsonl")
            with self.subTest(name=name), self.assertRaisesRegex(
                    SettingsError, "baseline_judgements missing.*digest changed"):
                rehash(settings, journal)
            self.assertFalse(journal.path.exists())
            self.assertFalse((journal.path.parent / "rehash.json").exists())
            path.write_text(original)

    def test_marker_restores_missing_journal_mapping_from_marker_after_replay(self):
        report = migrate(self.policy, self.config)
        settings = load_settings(self.config)
        journal = Journal(self.root / "state/journal.jsonl")
        key = ("example/widget", 7, "abc")
        journal.append(kind="pr", key=key, event="nothing", policy=report.legacy_hash,
                       mode="live", fingerprint="fp")
        legacy_records = journal.path.read_bytes()
        first = rehash(settings, journal)
        journal.path.write_bytes(legacy_records)
        (self.config / "rules.md").write_text("Changed after migration")
        # Recovery uses the marker, even if an older manifest cannot reconstruct the baseline now.
        manifest_path = self.config / "migration.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.pop("baseline_judgements")
        manifest_path.write_text(json.dumps(manifest))
        replayed = Journal(journal.path)
        replayed.replay()
        self.assertFalse(replayed.is_final(key, settings.judgement(key[0]).hash, "live", "fp"))
        recovered = rehash(load_settings(self.config), replayed)
        self.assertEqual(recovered["judgements"], first["judgements"])
        restarted = Journal(journal.path)
        restarted.replay()
        for state in (replayed, restarted):
            self.assertTrue(state.is_final(key, settings.judgement(key[0]).hash, "live", "fp"))
        before = journal.path.read_bytes()
        self.assertIsNone(rehash(load_settings(self.config), restarted))
        self.assertEqual(journal.path.read_bytes(), before)

    def test_journal_fsync_precedes_rehash_marker_publication(self):
        migrate(self.policy, self.config)
        settings = load_settings(self.config)
        journal = Journal(self.root / "state/journal.jsonl")
        events = []
        fsync, write_file = os.fsync, migration._write_file
        def record_fsync(fd):
            if os.fstat(fd).st_ino == journal.path.stat().st_ino:
                record = json.loads(journal.path.read_text().splitlines()[-1])
                self.assertEqual(record["event"], "rehash")  # flush happened before fsync
                events.append("journal fsync")
            return fsync(fd)
        def record_marker(path, content, **kwargs):
            events.append("marker publication")
            self.assertIn(json.loads(content)["legacy_hash"], journal.rehashes)
            return write_file(path, content, **kwargs)
        with patch("lightkeeper.journal.os.fsync", side_effect=record_fsync), \
             patch("lightkeeper.migrate._write_file", side_effect=record_marker):
            rehash(settings, journal)
        self.assertEqual(events, ["journal fsync", "marker publication"])

    def test_failed_journal_fsync_never_publishes_marker(self):
        migrate(self.policy, self.config)
        journal = Journal(self.root / "state/journal.jsonl")
        with patch("lightkeeper.journal.os.fsync", side_effect=OSError("fsync failed")), \
             self.assertRaisesRegex(SettingsError, "fsync failed"):
            rehash(load_settings(self.config), journal)
        self.assertFalse(journal.rehashes)
        self.assertFalse((journal.path.parent / "rehash.json").exists())
