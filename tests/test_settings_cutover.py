"""Settings migration and reload integration: real settings files, fake GitHub/model, no external writes."""
import dataclasses
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from lightkeeper import __main__ as cli
from lightkeeper.github import Gh
from lightkeeper.journal import Journal, SETTLED_EVENTS
from lightkeeper.migrate import migrate, rehash, settings_toml
from lightkeeper.models import Decision, is_automated
from lightkeeper.settings import (AgentSettings, CommentSettings, RepoSettings, Settings, SettingsError,
                                        load_settings)
from lightkeeper.ui.model import Runtime
from lightkeeper.ui.project import Observer, Projection
from tests.helpers import FakeGh, FakeReviewer, HEAD, PENDING_PROD, PLAN_JOB, make_pr, make_run

REPO = 'org/repo'
KEY = (REPO, 7, HEAD)
APPROVE = Decision('approve', 'private assessment', 'ai', summary='Looks good.', notify='Check the change.')


class CutoverTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.config = self.root / 'config'
        self.config.mkdir()
        self.settings = Settings('me', AgentSettings(), CommentSettings(),
                                 {REPO: RepoSettings(REPO, post='live', deployments=True)},
                                 'Generic review rules.', '', {}, self.config)
        self.save()
        self.gh = FakeGh()
        self.gh.pr_snapshots[KEY[:2]] = [make_pr()]
        self.journal = Journal(self.root / 'state/journal.jsonl')
        self.observer = Observer(Projection(), Runtime(login='me', repos=(REPO,)))
        self.journal.observer = self.observer.feed
        self.reviewer = FakeReviewer([APPROVE])
        self.runner = cli.Runner(self.settings, self.gh, self.journal, self.reviewer,
                                 observer=self.observer, notifier=Mock(), live_lock_held=True)
        logging = patch.object(cli, 'log')
        self.log = logging.start()
        self.addCleanup(logging.stop)

    def save(self, **repo_changes):
        if repo_changes:
            self.settings = dataclasses.replace(self.settings, repos={
                **self.settings.repos, REPO: dataclasses.replace(self.settings.repo(REPO), **repo_changes)})
        (self.config / 'settings.toml').write_text(settings_toml(self.settings))
        (self.config / 'rules.md').write_text(self.settings.rules)
        (self.config / 'template.md').write_text(self.settings.template)
        (self.config / 'repos').mkdir(exist_ok=True)
        for repo, notes in self.settings.notes.items():
            (self.config / 'repos' / (repo.replace('/', '__') + '.md')).write_text(notes)

    def reload(self):
        self.runner.refresh_settings(load_settings(self.config))

    def records(self):
        return [json.loads(line) for line in self.journal.path.read_text().splitlines()]

    def test_journal_findings_ignore_unknown_fields_in_cache_and_projection(self):
        finding = {'severity': 'important', 'origin': 'diff', 'text': 'Review this', 'future': 'extra'}
        self.journal.append(kind='pr', key=KEY, event='assessment', mode='live',
                            policy=self.settings.judgement(REPO).hash, fingerprint='fp',
                            action='comment', reason='check', source='ai', findings=[finding])
        self.journal.append(kind='pr', key=KEY, event='withheld', mode='live', policy='p')
        replay = Journal(self.journal.path)
        self.observer.replay(replay)
        self.runner.journal = replay
        decision = self.runner._stored_decision(KEY, 'fp')
        self.assertEqual(decision.findings[0].text, 'Review this')
        self.assertEqual(self.observer.snapshot.details[KEY].latest_findings, decision.findings)

    def mutate_in_model(self, mutate):
        original = self.reviewer._next
        def ask():
            mutate()
            return original()
        self.reviewer._next = ask

    def arm_gate(self):
        self.gh.pr_snapshots.clear()
        self.gh.runs = [make_run()]
        self.gh.pending[(REPO, 100)] = [PENDING_PROD]
        self.gh.notifications = [(REPO, 100, '2026-10-13T00:00:00Z')]
        self.gh.jobs_by_attempt[(REPO, 100, 1)] = [PLAN_JOB]

    def test_prewrite_edits_inside_reviewer(self):
        edits = {
            'disabled': (lambda: self.save(enabled=False), 'defer', 'repository disabled'),
            'revoked': (lambda: self.save(allowed=('comment',)), 'withheld', 'approve disabled'),
            'rules': (lambda: (self.config / 'rules.md').write_text('Changed rules'), 'defer',
                      'stale assessment: rules changed'),
            'notes': (lambda: (self.config / 'repos/org__repo.md').write_text('Changed notes'), 'defer',
                      'stale assessment: notes changed for org/repo'),
            'remarks': (lambda: self.save(remarks='blockers'), 'defer', 'stale assessment: remarks changed'),
            'corrupt': (lambda: (self.config / 'settings.toml').write_text('invalid = ['), 'defer',
                        'settings invalid, not writing'),
        }
        for name, (mutate, outcome, reason) in edits.items():
            with self.subTest(name=name):
                self.settings = self.runner.settings
                self.save()
                (self.config / 'repos/org__repo.md').unlink(missing_ok=True)
                self.reviewer = FakeReviewer([APPROVE])
                self.runner.reviewer = self.reviewer
                self.journal = Journal(self.root / name / 'journal.jsonl')
                self.runner.journal = self.journal
                self.mutate_in_model(mutate)
                self.assertEqual(self.runner.process_pr(*KEY[:2]), outcome)
                self.assertEqual(self.journal.defer_reason(KEY), reason)
                self.assertFalse(self.gh.writes)
                assessment = next(r for r in self.records() if r['event'] == 'assessment')
                self.assertEqual(assessment['policy'], self.runner.settings.judgement(REPO).hash)
                self.assertEqual(assessment['components'], self.runner.settings.judgement(REPO).components)
                self.assertEqual(assessment['body'], APPROVE.body)

    def test_second_boundary_catches_edit_after_full_fetch(self):
        def observer(event, **fields):
            if event == 'phase' and fields.get('phase') == 'writing':
                self.save(allowed=())
        self.runner.observer = observer
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'withheld')
        self.assertFalse(self.gh.writes)

    def test_login_edits_block_post_including_replayed_withheld_assessments(self):
        original = self.settings
        edits = {
            'disabled': (lambda: self.save(enabled=False), 'defer', 'repository disabled'),
            'dry-run': (lambda: self.save(post='dry-run'), 'would_approve', None),
            'revoked': (lambda: self.save(allowed=()), 'withheld', 'approve disabled'),
            'rules': (lambda: (self.config / 'rules.md').write_text('Changed rules'), 'defer',
                      'stale assessment: rules changed'),
            'corrupt': (lambda: (self.config / 'settings.toml').write_text('invalid = ['), 'defer',
                        'settings invalid, not writing'),
            'deployments': (lambda: self.save(deployments=False), 'defer', 'deployments disabled'),
        }
        for deployment in (False, True):
            for replay in (False, True):
                for name, (mutate, outcome, reason) in edits.items():
                    if name == 'deployments' and not deployment:
                        continue
                    with self.subTest(deployment=deployment, replay=replay, edit=name):
                        self.settings = original
                        self.save()
                        self.reload()
                        self.gh.login = Mock(return_value='me')
                        self.gh.pr_snapshots[KEY[:2]] = [make_pr()]
                        self.runner.reviewer = FakeReviewer([APPROVE])
                        self.journal = Journal(self.root / f'{deployment}-{replay}-{name}/journal.jsonl')
                        self.runner.journal = self.journal
                        if deployment:
                            self.arm_gate()
                        key = (REPO, 100, 1, 555) if deployment else KEY
                        process = (lambda: self.runner.process_run(REPO, 100)) if deployment else (
                            lambda: self.runner.process_pr(*KEY[:2]))
                        if replay:
                            self.save(allowed=())
                            self.reload()
                            self.assertEqual(process(), 'withheld')
                            self.journal = Journal(self.journal.path)
                            self.journal.replay()
                            self.runner.journal = self.journal
                            self.save(allowed=('approve',))
                            self.reload()
                        def login():
                            mutate()
                            return 'me'
                        self.gh.login = Mock(side_effect=login)
                        self.assertEqual(process(), outcome)
                        self.gh.login.assert_called_once_with()
                        self.assertEqual(self.journal.defer_reason(key), reason)
                        self.assertFalse(self.gh.writes)
                        self.assertEqual(self.runner.reviewer.calls, 1)

    def test_gate_prewrite_edits_and_retry_without_model(self):
        self.arm_gate()
        self.mutate_in_model(lambda: self.save(deployments=False))
        self.assertEqual(self.runner.process_run(REPO, 100), 'defer')
        gate = (REPO, 100, 1, 555)
        self.assertEqual(self.journal.defer_reason(gate), 'deployments disabled')
        self.assertFalse(self.gh.writes)
        self.save(deployments=True)
        self.reload()
        self.assertEqual(self.runner.process_run(REPO, 100), 'applied')
        self.assertEqual(self.reviewer.calls, 1)
        self.assertNotIn(APPROVE.reason, self.gh.writes[0][-1])

    def test_gate_withheld_then_allowed_reuses_assessment_after_restart(self):
        self.arm_gate()
        self.save(allowed=())
        self.reload()
        self.assertEqual(self.runner.process_run(REPO, 100), 'withheld')
        self.save(allowed=('approve',))
        self.reload()
        self.runner.journal = Journal(self.journal.path)
        self.runner.journal.replay()
        self.assertEqual(self.runner.process_run(REPO, 100), 'applied')
        self.assertEqual(self.reviewer.calls, 1)

    def test_each_disallowed_action_is_withheld_never_substituted(self):
        self.save(allowed=())
        self.reload()
        for n, action in enumerate(('approve', 'comment', 'request_changes'), 7):
            with self.subTest(action=action):
                self.gh.pr_snapshots[(REPO, n)] = [make_pr(number=n)]
                self.runner.reviewer = FakeReviewer([dataclasses.replace(APPROVE, outcome=action)])
                self.assertEqual(self.runner.process_pr(REPO, n), 'withheld')
                rec = self.records()[-1]
                self.assertEqual((rec['action'], rec['reason'], rec['body'], rec['notify']),
                                 (action, f'{action} disabled', APPROVE.body, APPROVE.notify))
        self.assertFalse(self.gh.writes)

    def test_withheld_then_allowed_live_and_dry_without_model(self):
        for mode in ('live', 'dry-run'):
            with self.subTest(mode=mode):
                self.journal = Journal(self.root / mode / 'journal.jsonl')
                self.runner.journal = self.journal
                self.runner.reviewer = FakeReviewer([APPROVE])
                self.save(allowed=(), post=mode)
                self.reload()
                self.assertEqual(self.runner.process_pr(*KEY[:2]), 'withheld')
                self.assertEqual(self.runner.process_pr(*KEY[:2]), 'withheld')
                self.runner.journal = Journal(self.journal.path)
                self.runner.journal.replay()
                self.save(allowed=('approve',), post=mode)
                self.reload()
                self.assertEqual(self.runner.process_pr(*KEY[:2]), 'applied' if mode == 'live' else 'would_approve')
                self.assertEqual(self.runner.reviewer.calls, 1)
        self.assertEqual(len(self.gh.writes), 1)
        self.assertTrue(is_automated(self.gh.writes[0][-1]))
        self.assertIn('<!-- lightkeeper v2 ', self.gh.writes[0][-1])

    def test_authority_defer_then_restored_reuses_model(self):
        self.mutate_in_model(lambda: self.save(enabled=False))
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'defer')
        self.save(enabled=True)
        self.reload()
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'applied')
        self.assertEqual(self.reviewer.calls, 1)

    def test_changed_head_context_or_judgement_cannot_reuse_withheld(self):
        for change in ('head', 'context', 'rules'):
            with self.subTest(change=change):
                self.runner.journal = Journal(self.root / change / 'journal.jsonl')
                self.runner.reviewer = FakeReviewer([APPROVE, APPROVE])
                self.gh.pr_snapshots[KEY[:2]] = [make_pr()]
                self.save(allowed=())
                self.reload()
                self.assertEqual(self.runner.process_pr(*KEY[:2]), 'withheld')
                if change == 'rules':
                    self.settings = dataclasses.replace(self.settings, rules=self.settings.rules + '\nNew rule.')
                else:
                    self.gh.pr_snapshots[KEY[:2]] = [make_pr(**({'head_sha': 'a' * 40} if change == 'head'
                                                              else {'body': 'Changed description'}))]
                self.save(allowed=('approve',))
                self.reload()
                self.assertEqual(self.runner.process_pr(*KEY[:2]), 'applied')
                self.assertEqual(self.runner.reviewer.calls, 2)

    def test_screen_settings_reload_reach_runtime(self):
        self.settings = dataclasses.replace(
            self.settings, screen=dataclasses.replace(self.settings.screen, idle="always", tempo=1.2,
                                                     clock=False, moon=False, events=False))
        self.save()
        self.reload()
        runtime = self.observer.snapshot.runtime
        self.assertEqual((runtime.idle, runtime.tempo, runtime.clock, runtime.moon, runtime.events),
                         ("always", 1.2, False, False, False))

    def test_settings_error_reload_keeps_good_settings_and_tick_runs(self):
        (self.config / 'settings.toml').write_text('broken = [')
        self.assertEqual(cli._loop(SimpleNamespace(once=True), self.settings, self.runner, threading.Event()), 0)
        self.assertEqual(self.runner.settings, self.settings)
        self.assertEqual(self.reviewer.calls, 1)
        self.assertFalse(self.gh.writes)
        self.assertEqual(self.journal.defer_reason(KEY), 'settings invalid, not writing')
        self.assertEqual(next(a for a in self.observer.snapshot.alerts if a.key == 'settings_error').level, 'error')
        self.save()
        self.reload()
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'applied')
        self.assertEqual(self.reviewer.calls, 1)

    def test_missing_or_blank_rules_block_next_tick_and_recover(self):
        for content in (None, '', ' \n\t'):
            with self.subTest(content=content):
                self.journal = Journal(self.root / f'journal-{content!r}.jsonl')
                self.runner.journal = self.journal
                self.reviewer.decisions = [APPROVE]
                self.gh.writes.clear()
                rules = self.config / 'rules.md'
                if content is None:
                    rules.unlink()
                else:
                    rules.write_text(content, encoding='utf-8')
                self.assertEqual(cli._loop(SimpleNamespace(once=True), self.settings,
                                           self.runner, threading.Event()), 0)
                self.assertFalse(self.gh.writes)
                self.assertEqual(self.journal.defer_reason(KEY), 'settings invalid, not writing')
                self.save()
                self.reload()
                self.assertEqual(self.runner.process_pr(*KEY[:2]), 'applied')
                self.assertEqual(len(self.gh.writes), 1)

    def test_per_repo_enabled_post_and_deployments(self):
        self.settings = dataclasses.replace(self.settings, repos={
            REPO: RepoSettings(REPO, enabled=False, post='live', deployments=True),
            'org/dry': RepoSettings('org/dry'), 'org/live': RepoSettings('org/live', post='live')})
        self.save()
        self.reload()
        for name in ('org/dry', 'org/live'):
            self.gh.pr_snapshots[(name, 7)] = [make_pr(repo=name)]
        self.reviewer.decisions = [APPROVE, APPROVE]
        self.gh.notifications = [(r, 100, 'date') for r in self.settings.repos]
        self.gh.pending_deployments = Mock(side_effect=AssertionError('deployment disabled'))
        self.assertEqual(self.runner.tick(), {'would_approve': 1, 'applied': 1})
        self.assertEqual([w[1] for w in self.gh.writes], ['org/live'])
        self.assertEqual(self.reviewer.calls, 2)
        self.assertEqual(self.runner.process_pr(REPO, 7), 'skip')
        self.assertEqual(self.runner.process_run(REPO, 100), 'skip')
        self.assertTrue(next(r for r in self.observer.snapshot.watching if r.item.repo == 'org/dry').dry_run)

    def test_notes_are_selected_for_each_model_call(self):
        self.settings = dataclasses.replace(self.settings, notes={REPO: 'Only this repository.'})
        self.save()
        self.reload()
        self.mutate_in_model(lambda: self.assertEqual(self.reviewer.rules,
                             self.settings.rules + '\n\n# Repository notes\n\nOnly this repository.'))
        self.runner.process_pr(*KEY[:2])

    def test_component_changes_logged_including_after_restart(self):
        self.runner.reviewer = FakeReviewer([Decision('nothing', 'same result', 'ai')] * 3)
        self.runner.process_pr(*KEY[:2])
        for field, value, message in [('rules', 'New rules', 'rules changed'),
                                      ('notes', {REPO: 'Repo notes'}, 'notes changed for org/repo')]:
            self.settings = dataclasses.replace(self.settings, **{field: value})
            self.save()
            self.reload()
            self.runner.process_pr(*KEY[:2])
            self.assertTrue(any(message in c.args[0] for c in self.log.call_args_list))
            self.assertEqual(self.observer.snapshot.history[0].change_text, message)
        self.settings = dataclasses.replace(self.settings, rules='Rules edited while stopped')
        self.save()
        journal = Journal(self.journal.path)
        journal.replay()
        runner = cli.Runner(load_settings(self.config), self.gh, journal, live_lock_held=True, notifier=Mock())
        self.log.reset_mock()
        runner.refresh_settings(runner.settings)
        self.assertTrue(any('rules changed' in c.args[0] for c in self.log.call_args_list))

    def test_presentation_engine_options_and_repo_list_do_not_reopen(self):
        self.runner.reviewer = FakeReviewer([Decision('nothing', 'settled', 'ai')])
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'nothing')
        before = self.settings.judgement(REPO)
        self.settings = dataclasses.replace(self.settings, template='Different template',
                    agent=AgentSettings(model='sonnet', effort='high', profile='/tmp/profile', timeout_s=12),
                    comment=CommentSettings('fr', False), repos={**self.settings.repos, 'org/new': RepoSettings('org/new')})
        self.save(allowed=(), deployments=False)
        self.reload()
        self.assertEqual(self.settings.judgement(REPO), before)
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'final')
        self.assertEqual(self.runner.reviewer.calls, 1)
        other_engine = dataclasses.replace(self.settings, agent=AgentSettings(engine='future'))
        self.assertEqual(other_engine.judgement(REPO), before)
        (self.config / 'settings.toml').write_text(settings_toml(other_engine))
        with self.assertRaisesRegex(SettingsError, 'agent.engine'):
            load_settings(self.config)

    def test_live_to_dry_edit_at_boundary_records_dry_mode(self):
        self.mutate_in_model(lambda: self.save(post='dry-run'))
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'would_approve')
        self.assertFalse(self.gh.writes)
        self.assertEqual(self.records()[-1]['mode'], 'dry-run')

    def test_gate_boundary_revalidates_settings_and_judgement(self):
        self.arm_gate()
        edits = (
            (lambda: self.save(enabled=False), 'defer', 'repository disabled'),
            (lambda: self.save(allowed=()), 'withheld', 'approve disabled'),
            (lambda: (self.config / 'rules.md').write_text('Edited rules'), 'defer', 'stale assessment: rules changed'),
            (lambda: (self.config / 'settings.toml').write_text('bad = ['), 'defer', 'settings invalid, not writing'),
        )
        for n, (edit, outcome, reason) in enumerate(edits):
            with self.subTest(reason=reason):
                self.settings = self.runner.settings
                self.save()
                self.runner.journal = Journal(self.root / f'gate-{n}/journal.jsonl')
                self.reviewer = FakeReviewer([APPROVE])
                self.runner.reviewer = self.reviewer
                self.mutate_in_model(edit)
                self.assertEqual(self.runner.process_run(REPO, 100), outcome)
                self.assertEqual(self.runner.journal.defer_reason((REPO, 100, 1, 555)), reason)
                self.assertFalse(self.gh.writes)

    def test_gate_second_boundary_rechecks_deployment_switch(self):
        self.arm_gate()
        def observe(event, **fields):
            if event == 'phase' and fields.get('phase') == 'writing':
                self.save(deployments=False)
        self.runner.observer = observe
        self.assertEqual(self.runner.process_run(REPO, 100), 'defer')
        self.assertFalse(self.gh.writes)

    def test_notes_only_reopen_their_repository(self):
        other = 'org/other'
        self.settings = dataclasses.replace(self.settings, repos={**self.settings.repos, other: RepoSettings(other)})
        self.save()
        self.reload()
        self.gh.pr_snapshots[(other, 7)] = [make_pr(repo=other)]
        self.runner.reviewer = FakeReviewer([Decision('nothing', 'settled', 'ai')] * 3)
        self.runner.process_pr(REPO, 7)
        self.runner.process_pr(other, 7)
        self.settings = dataclasses.replace(self.settings, notes={REPO: 'New repository note'})
        self.save()
        self.reload()
        self.assertEqual(self.runner.process_pr(other, 7), 'final')
        self.assertEqual(self.runner.process_pr(REPO, 7), 'nothing')
        self.assertEqual(self.runner.reviewer.calls, 3)

    def test_dry_repository_marker_does_not_relabel_an_existing_live_approval(self):
        self.runner.tick()
        self.save(post='dry-run')
        self.reload()
        self.assertEqual(self.runner.tick(), {'final': 1})
        row = self.observer.snapshot.watching[0]
        self.assertTrue(row.repo_dry)
        self.assertFalse(row.dry_run)
        self.assertEqual(row.state, 'approved')

    @unittest.skipUnless(importlib.util.find_spec('rich'), 'Rich is optional')
    def test_screen_settings_path_dry_marker_withheld_and_change_text(self):
        from rich.console import Console
        from lightkeeper.ui.rich_view import render
        self.save(post='dry-run', allowed=())
        self.reload()
        self.runner.tick()
        output = io.StringIO()
        with patch.object(Path, 'home', return_value=self.root):
            console = Console(file=output, width=240, height=30, force_terminal=False)
            console.print(render(self.observer.snapshot, 0, 240, 30, ascii=True))
        text = ' '.join(output.getvalue().split())
        self.assertIn('settings ~/config', text)
        self.assertIn('dry withheld: approve disabled', text)
        self.assertNotIn('rules ' + self.settings.judgement(REPO).hash, text)
        self.settings = dataclasses.replace(self.settings, rules='New rules')
        self.save()
        self.reload()
        self.reviewer.decisions.append(APPROVE)
        self.runner.tick()
        output = io.StringIO()
        Console(file=output, width=240, height=30).print(render(self.observer.snapshot, 0, 240, 30, ascii=True))
        self.assertIn('(rules changed)', output.getvalue())


class StartupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        from tests.helpers import isolate_cli
        isolate_cli(self, self.root)
        self.config = self.root / 'config'
        self.state = self.root / 'state'
        self.source = self.root / 'policy.toml'
        self.gh = FakeGh()
        home = patch.object(Path, 'home', return_value=self.root)
        home.start()
        self.addCleanup(home.stop)

    def main(self, extra=(), loop=None):
        with patch.object(cli, 'Gh', return_value=self.gh), patch.object(cli, 'ClaudeReviewer') as model, \
             patch.object(cli, 'configure_diagnostics'), patch('sys.stdout', new_callable=io.StringIO) as out, \
             patch('sys.stderr', new_callable=io.StringIO) as err:
            argv = ['--plain', '--config-dir', str(self.config), '--state-dir', str(self.state), *extra]
            if self.source.exists():
                argv.extend(['--policy', str(self.source)])
            if loop:
                with patch.object(cli, '_loop', side_effect=loop):
                    result = cli.main(argv)
            else:
                result = cli.main(argv)
            return result, out.getvalue(), err.getvalue(), model

    def test_startup_logs_frame_two_alias_once(self):
        from lightkeeper._frame_v2 import FRAME, SCHEMA
        from lightkeeper.settings import judgement
        fixture = Path(__file__).parent / 'fixtures/migration'
        shutil.copyfile(fixture / 'policy.example.toml', self.source)
        shutil.copyfile(fixture / 'RULES.md', self.root / 'RULES.md')
        migrate(self.source, self.config)
        settings = load_settings(self.config)
        repo = 'example/widget'
        previous = judgement(FRAME, settings.rules, settings.notes.get(repo, ''),
                             settings.repo(repo).remarks, SCHEMA).hash
        Journal(self.state / 'journal.jsonl').append(
            kind='pr', key=(repo, 7, HEAD), event='nothing', policy=previous, mode='live', fingerprint='fp')
        self.gh.login = lambda: 'octocat'
        result = self.main(['--allow-live'], lambda *args: 0)
        self.assertEqual(result[0], 0, result[2])
        self.assertIn(f"rehash frame 2 -> { {repo: settings.judgement(repo).hash} }", result[1])
        result = self.main(['--allow-live'], lambda *args: 0)
        self.assertEqual(result[0], 0, result[2])
        self.assertNotIn('rehash frame 2', result[1])

    def test_startup_migration_and_rehash_are_idempotent(self):
        fixture = Path(__file__).parent / 'fixtures/migration'
        shutil.copyfile(fixture / 'policy.example.toml', self.source)
        shutil.copyfile(fixture / 'RULES.md', self.root / 'RULES.md')
        from lightkeeper.policy import load_policy
        legacy = load_policy(self.source).policy_hash
        journal = Journal(self.state / 'journal.jsonl')
        keys = []
        for n, event in enumerate((*SETTLED_EVENTS, 'applied', 'satisfied'), 1):
            key = ('example/widget', n, HEAD)
            keys.append(key)
            journal.append(kind='pr', key=key, event=event, policy=legacy, mode='live', fingerprint='fp')
        self.gh.login = lambda: 'octocat'
        def loop(args, settings, runner, stop):
            for key in keys:
                self.assertTrue(runner.journal.is_final(key, settings.judgement(key[0]).hash, 'live', 'fp'))
            return 0
        first = self.main(['--allow-live'], loop)
        self.assertEqual(first[0], 0, first[2])
        self.assertIn(str(self.config / 'rules.md'), first[1])
        self.assertIn('repos/<org>__<name>.md', first[1])
        self.assertIn('rehash', first[1])
        marker = json.loads((self.state / 'rehash.json').read_text())
        self.assertEqual(set(marker), {'legacy_hash', 'judgements', 'at'})
        self.assertEqual(marker['legacy_hash'], legacy)
        before = journal.path.read_bytes()
        self.source.write_text('this source is now ignored')
        second = self.main(['--allow-live'], loop)
        self.assertEqual(second[0], 0, second[2])
        self.assertNotIn('Migrated:', second[1])
        self.assertEqual(journal.path.read_bytes(), before)
        self.assertEqual(sum(json.loads(line)['event'] == 'rehash' for line in before.splitlines()), 1)
        self.assertTrue((self.root / 'RULES.md').exists())

    def test_rehash_recovers_missing_marker_without_second_record(self):
        self.source.write_text('login="me"\nrepos=["org/repo"]\n')
        (self.root / 'RULES.md').write_text('Rules')
        migrate(self.source, self.config)
        settings = load_settings(self.config)
        journal = Journal(self.state / 'journal.jsonl')
        rehash(settings, journal)
        before = journal.path.read_bytes()
        (self.state / 'rehash.json').unlink()
        (self.config / 'rules.md').write_text('Changed after the first append')
        replayed = Journal(journal.path)
        replayed.replay()
        rehash(load_settings(self.config), replayed)
        self.assertEqual(journal.path.read_bytes(), before)
        self.assertEqual(json.loads((self.state / 'rehash.json').read_text())['judgements'][REPO],
                         settings.judgement(REPO).hash)

    def test_first_start_after_rules_edit_only_grandfathers_migration_baseline(self):
        self.source.write_text('login="me"\nrepos=["org/repo"]\n')
        (self.root / 'RULES.md').write_text('Original rules')
        report = migrate(self.source, self.config)
        baseline = load_settings(self.config).judgement(REPO).hash
        journal = Journal(self.state / 'journal.jsonl')
        journal.append(kind='pr', key=KEY, event='nothing', policy=report.legacy_hash,
                       mode='live', fingerprint='fp')
        (self.config / 'rules.md').write_text('Changed before the first start')
        def loop(args, settings, runner, stop):
            self.assertNotEqual(settings.judgement(REPO).hash, baseline)
            self.assertFalse(runner.journal.is_final(KEY, settings.judgement(REPO).hash, 'live', 'fp'))
            self.assertTrue(runner.journal.is_final(KEY, baseline, 'live', 'fp'))
            return 0
        result = self.main(['--allow-live'], loop)
        self.assertEqual(result[0], 0, result[2])

    def test_fresh_install_warns_persistently_without_review_or_model_calls(self):
        self.gh = Mock()
        def loop(args, settings, runner, stop):
            observer = Observer(Projection(), Runtime())
            runner.observer = observer
            runner.refresh_settings(settings)
            runner.tick()
            runner.tick()
            alert = next(a for a in observer.snapshot.alerts if a.key == 'no_repositories')
            self.assertEqual(alert.text, f'no repositories configured; edit {self.config}/settings.toml')
            self.assertEqual(alert.level, 'warning')
            self.assertEqual(settings.repos, {})
            return 0
        result, output, error, model = self.main(['--once'], loop)
        self.assertEqual(result, 0, error)
        self.assertIn('no repositories configured', output)
        self.assertEqual(self.gh.mock_calls, [])
        self.assertFalse(model.return_value.mock_calls)

    def test_startup_invalid_settings_exits_two(self):
        self.config.mkdir()
        for content in ('broken = [', '[identity]\nlogin="me"\n[agent]\nengine="codex"'):
            (self.config / 'settings.toml').write_text(content)
            result, output, error, model = self.main(['--once'])
            self.assertEqual(result, 2)
            self.assertIn('settings error:', error)
            model.assert_not_called()

    def test_only_enabled_live_repository_takes_live_lock(self):
        self.config.mkdir()
        (self.config / 'rules.md').write_text('Review carefully.', encoding='utf-8')
        for enabled, post, extra, live in ((False, 'live', [], False), (True, 'dry-run', [], False),
                                         (True, 'live', [], True), (True, 'live', ['--once'], False)):
            with self.subTest(enabled=enabled, post=post, extra=extra):
                settings = Settings('me', AgentSettings(), CommentSettings(),
                                    {REPO: RepoSettings(REPO, enabled=enabled, post=post)}, '', '', {}, self.config)
                (self.config / 'settings.toml').write_text(settings_toml(settings))
                with patch.object(cli, 'Lock', wraps=cli.Lock) as locks:
                    def loop(args, settings, runner, stop):
                        self.assertEqual(runner.live_lock_held, live)
                        return 0
                    self.assertEqual(self.main(['--allow-live', *extra], loop)[0], 0)
                    self.assertEqual([c.args[0] for c in locks.call_args_list],
                                     [self.state / 'lock'] + ([cli.live_lock_path('me')] if live else []))

    def test_check_repo_uses_fake_gh_and_skips_config_model_journal(self):
        process = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(
            {'archived': False, 'default_branch': 'main'}), stderr=''))
        with patch.object(cli, 'Gh', return_value=Gh(runner=process)), \
             patch.object(cli, 'load_settings') as settings, patch.object(cli, 'Journal') as journal, \
             patch.object(cli, 'ClaudeReviewer') as model, patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(cli.main(['--check-repo', 'example/widget']), 0)
            self.assertEqual(process.call_args.args[0], ['gh', 'api', 'repos/example/widget'])
            self.assertEqual(out.getvalue(), 'example/widget: access ok · default branch main\n')
            settings.assert_not_called()
            journal.assert_not_called()
            model.assert_not_called()

    def test_config_precedence(self):
        from lightkeeper.settings import prepare_config_dir
        with patch.dict(os.environ, LIGHTKEEPER_CONFIG=str(self.root / 'env')):
            self.assertEqual(prepare_config_dir(), self.root / 'env')
            self.assertEqual(prepare_config_dir(self.config), self.config)
