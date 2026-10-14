"""Parse → render → write contracts; fake engines and GitHub, real settings and journal."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from lightkeeper.__main__ import EVENT_WORDS, Runner
from lightkeeper.engines.base import Answer, Diagnostics
from lightkeeper.journal import Journal
from lightkeeper.migrate import settings_toml
from lightkeeper.models import Review, is_automated
from lightkeeper.reviewer import (DEPLOY_SCHEMA_V2, PR_SCHEMA_V2, Reviewer, UnclearAnswer,
                                        parse_deploy_answer_v2)
from lightkeeper.ui.model import Runtime
from lightkeeper.ui.project import Projection
from tests.helpers import FakeGh, HEAD, PENDING_PROD, PLAN_JOB, make_pr, make_run, make_settings

REPO = 'org/repo'
KEY = (REPO, 7, HEAD)
PRIVATE = 'PRIVATE_REASON_SENTINEL'
PUBLIC = 'Public summary.'
FINDINGS = [dict(severity=severity, origin=origin, text=text) for severity, origin, text in (
    ('blocker', 'diff', 'Fix the condition.'),
    ('important', 'ci', 'Investigate the failing check.'),
    ('minor', 'comment', 'Simplify the name.'),
)]


def answer(**changes):
    return dict(action='comment', why='Not approving yet: the condition is inverted.', watch='', summary=PUBLIC, findings=FINDINGS, reason=PRIVATE,
                notify='PRIVATE_NOTIFY_SENTINEL') | changes


class RenderedWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = make_settings()
        self.gh = FakeGh()
        self.gh.pr_snapshots[KEY[:2]] = [make_pr()]
        self.projection = Projection()
        self.journal = Journal(self.root / 'journal.jsonl', observer=self.projection.feed)
        self.engine = Mock(label='test/engine')
        self.reviewer = Reviewer(self.engine, self.settings.rules, self.root / 'sessions')
        self.runner = Runner(self.settings, self.gh, self.journal, self.reviewer,
                             notifier=Mock(), live_lock_held=True)

    def configure(self, *, post='live', remarks='important', disclose=True, template='', allowed=None):
        repo = replace(self.settings.repo(REPO), post=post, remarks=remarks)
        if allowed is not None:
            repo = replace(repo, allowed=allowed)
        self.settings = replace(self.settings, repos={**self.settings.repos, REPO: repo},
                                comment=replace(self.settings.comment, disclose=disclose), template=template)
        self.save()
        self.runner.refresh_settings(self.settings)

    def save(self):
        config = self.settings.config_dir
        (config / 'settings.toml').write_text(settings_toml(self.settings))
        (config / 'rules.md').write_text(self.settings.rules)
        (config / 'template.md').write_text(self.settings.template)

    def reply(self, obj):
        self.engine.ask.return_value = Answer(obj, Diagnostics(json.dumps(obj), '', 0, ['fake']))

    def records(self):
        return [json.loads(line) for line in self.journal.path.read_text().splitlines()]

    def trace(self):
        return (f'<!-- lightkeeper v2 head={HEAD[:7]} '
                f'judgement={self.settings.judgement(REPO).hash} engine=test/engine -->')

    def test_old_journal_assessment_loads_without_why_or_watch(self):
        self.journal.append(kind='pr', key=KEY, event='assessment', action='comment', reason=PRIVATE,
                            source='ai', policy=self.settings.judgement(REPO).hash,
                            mode='live', fingerprint=make_pr().fingerprint, summary=PUBLIC, findings=FINDINGS)
        self.journal.append(kind='pr', key=KEY, event='withheld', reason='comment disabled',
                            mode='live', policy=self.settings.judgement(REPO).hash)
        replayed = Journal(self.journal.path, observer=self.projection.feed)
        replayed.replay()
        self.runner.journal = replayed
        decision = self.runner._stored_decision(KEY, make_pr().fingerprint)
        self.assertEqual((decision.why, decision.watch, decision.summary), ('', '', PUBLIC))
        self.assertEqual(len(decision.findings), 3)
        rendered = self.runner._render(REPO, KEY[2], decision)
        self.assertEqual(rendered.body, '')
        self.assertEqual(rendered.withheld_reason, 'no publishable rationale: why is blank')
        detail = self.projection.screen(Runtime(repos=(REPO,)), 0).details[KEY]
        self.assertEqual((detail.latest_why, detail.latest_watch), ('', ''))

    def test_v2_parse_render_write_and_session_exact_body(self):
        phrases = {'blockers': 'report blockers only', 'important': 'blockers and important',
                   'all': 'everything you notice'}
        for post in ('live', 'dry-run'):
            for remarks, count in (('blockers', 1), ('important', 2), ('all', 3)):
                for disclose in (True, False):
                    with self.subTest(post=post, remarks=remarks, disclose=disclose):
                        self.configure(post=post, remarks=remarks, disclose=disclose,
                                       template='Review: {summary}\n{findings}\n{disclosure}')
                        self.runner.journal = self.journal = Journal(self.root / f'{post}-{remarks}-{disclose}.jsonl')
                        self.gh.writes.clear()
                        self.reply(answer())
                        event = 'applied' if post == 'live' else 'would_comment'
                        self.assertEqual(self.runner.process_pr(*KEY[:2]), event)
                        bullets = '\n'.join(f"- **{f['severity']}** ({f['origin']}): {f['text']}"
                                            for f in FINDINGS[:count])
                        expected = 'Review: ' + PUBLIC + '\n' + bullets
                        if disclose:
                            expected += '\nAutomated review for @me (lightkeeper)'
                        expected += '\n\n' + self.trace()
                        record = next(r for r in self.records() if r['event'] == event)
                        self.assertEqual(record['body'], expected)
                        self.assertEqual((record['why'], record['watch']), (answer()['why'], ''))
                        self.assertEqual((record['published_findings'], record['summary'], record['reason']),
                                         (count, PUBLIC, PRIVATE))
                        if post == 'live':
                            self.assertEqual(self.gh.writes[0][-1], expected)
                        else:
                            self.assertFalse(self.gh.writes)
                        assessment = next(r for r in self.records() if r['event'] == 'assessment')
                        session = Path(assessment['data']['session'])
                        self.assertEqual((session / 'rendered.md').read_text(), expected)
                        self.assertEqual(json.loads((session / 'decision.json').read_text())['summary'], PUBLIC)
                        self.assertNotIn('PRIVATE_', expected)
                        self.assertTrue(is_automated(expected))
                        system, prompt, schema, *_ = self.engine.ask.call_args.args
                        self.assertEqual(schema, PR_SCHEMA_V2)
                        self.assertIn(phrases[remarks], system)
                        self.assertIn('owner may not post every action', system)
                        self.assertIn('honest verdict', system)
                        self.assertIn('org/repo#7', prompt)

    def test_withheld_render_retains_assessment_and_retries_without_model(self):
        self.configure(remarks='blockers', template='{findings}\n{disclosure}')
        self.reply(answer(summary='', findings=[FINDINGS[-1]]))
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'withheld')
        record = self.records()[-1]
        self.assertEqual(record['reason'], 'no publishable rationale after the remarks filter')
        self.assertEqual((record['body'], record['published_findings'], record['summary']), ('', 0, ''))
        self.assertEqual(record['private_reason'], PRIVATE)
        self.assertFalse(self.gh.writes)
        self.assertFalse(self.journal.is_final(KEY, self.settings.judgement(REPO).hash, 'live', make_pr().fingerprint))
        session = next((self.root / 'sessions').iterdir())
        self.assertFalse((session / 'rendered.md').exists())  # no body was posted or would be posted
        self.runner.journal = self.journal = Journal(self.journal.path)
        self.journal.replay()
        self.configure(template='Public template rationale.\n{disclosure}', remarks='blockers')
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'applied')
        self.engine.ask.assert_called_once()
        self.assertEqual((session / 'rendered.md').read_text(), self.gh.writes[0][-1])

    def test_render_uses_template_and_disclosure_reloaded_at_last_boundary(self):
        self.reply(answer())
        def edit(event, **fields):
            if event == 'phase' and fields.get('phase') == 'writing':
                self.settings = replace(self.settings, template='Updated: {summary}',
                                        comment=replace(self.settings.comment, disclose=False))
                self.save()
        self.runner.observer = edit
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'applied')
        self.assertEqual(self.gh.writes[0][-1], 'Updated: ' + PUBLIC + '\n\n' + self.trace())
        self.assertEqual(next(r for r in self.records() if r['event'] == 'applied')['published_findings'], 0)

    def test_authority_retry_preserves_findings_and_answering_engine_after_restart(self):
        self.configure(allowed=(), remarks='important', template='{why}\n{findings}')
        self.reply(answer())
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'withheld')
        self.runner.journal = self.journal = Journal(self.journal.path)
        self.journal.replay()
        self.engine.label = 'replacement/engine'
        self.configure(allowed=('comment',), remarks='important', template='{why}\n{findings}')
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'applied')
        self.engine.ask.assert_called_once()
        body = self.gh.writes[0][-1]
        self.assertIn(FINDINGS[0]['text'], body)
        self.assertIn(FINDINGS[1]['text'], body)
        self.assertNotIn(FINDINGS[2]['text'], body)
        self.assertTrue(body.endswith(self.trace()))  # provenance belongs to the stored assessment
        self.assertEqual(next(r for r in self.records() if r['event'] == 'applied')['published_findings'], 2)

    def test_deployment_public_summary_trace_and_session_never_private_reason(self):
        for post in ('live', 'dry-run'):
            with self.subTest(post=post):
                self.configure(post=post, template='PR only: {findings}\n{disclosure}')
                self.runner.journal = self.journal = Journal(self.root / f'deploy-{post}.jsonl')
                self.gh.writes.clear()
                run = make_run()
                self.gh.runs = [run]
                self.gh.pending[(REPO, run.run_id)] = [PENDING_PROD]
                self.gh.jobs_by_attempt[(REPO, run.run_id, run.attempt)] = [PLAN_JOB]
                self.reply(dict(decision='approve', summary=PUBLIC, reason=PRIVATE))
                event = 'applied' if post == 'live' else 'would_approve'
                self.assertEqual(self.runner.process_run(REPO, run.run_id), event)
                expected = PUBLIC + '\n\n' + self.trace()
                record = self.records()[-1]
                self.assertEqual((record['body'], record['summary'], record['reason']), (expected, PUBLIC, PRIVATE))
                if post == 'live':
                    self.assertEqual(self.gh.writes[0][-1], expected)
                else:
                    self.assertFalse(self.gh.writes)
                assessment = next(r for r in self.records() if r['event'] == 'assessment')
                self.assertEqual((Path(assessment['data']['session']) / 'rendered.md').read_text(), expected)
                self.assertEqual(self.engine.ask.call_args.args[2], DEPLOY_SCHEMA_V2)
                self.assertNotIn(PRIVATE, expected)

    def test_v2_comment_then_approve_same_head(self):
        self.reply(answer())
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'applied')
        comment = Review('me', 'COMMENTED', HEAD, self.gh.writes[0][-1])
        self.gh.pr_snapshots[KEY[:2]] = [make_pr(requested_users=(), reviews=(comment,))]
        self.settings = replace(self.settings, rules=self.settings.rules + '\nReconsider this context.\n')
        self.save()
        self.runner.refresh_settings(self.settings)
        self.reply(answer(action='approve', findings=[]))
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'applied')
        self.assertEqual([w[-2] for w in self.gh.writes], ['comment', 'approve'])

    def test_frame_cutover_reopens_legacy_contextual_outcomes_only_once(self):
        old_hash = '9d04caa8e5f2'  # Legacy frame/schema with tests.helpers default settings
        new_hash = self.settings.judgement(REPO).hash
        self.assertNotEqual(old_hash, new_hash)
        for n, (event, action) in enumerate((('nothing', None), ('would_approve', None), ('applied', 'comment'),
                                             ('applied', 'approve'), ('satisfied', None)), 10):
            key = (REPO, n, HEAD)
            self.journal.append(kind='pr', key=key, event=event, policy=old_hash, mode='live',
                                fingerprint=make_pr().fingerprint, **({'action': action} if action else {}))
            final = event == 'satisfied' or action == 'approve'
            self.assertEqual(self.journal.is_final(key, new_hash, 'live', make_pr().fingerprint), final)
        self.reply(answer(action='nothing'))
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'nothing')
        self.assertEqual(self.runner.process_pr(*KEY[:2]), 'final')
        self.engine.ask.assert_called_once()

    def test_screen_uses_public_summary_for_all_posted_and_would_post_rows(self):
        for kind, key in (('pr', KEY), ('deployment', (REPO, 100, 1, 555))):
            for event in ('applied', 'would_comment', 'would_approve', 'would_request_changes'):
                for action in ('approve', 'comment', 'request_changes'):
                    with self.subTest(kind=kind, event=event, action=action):
                        p = Projection()
                        p.feed(dict(kind=kind, key=key, event=event, action=action, reason=PRIVATE,
                                    summary=PUBLIC, body='rendered body with trace', at='2026-10-13T12:00:00Z'))
                        screen = p.screen(Runtime(repos=(REPO,)), 0)
                        self.assertEqual(screen.watching[0].why, PUBLIC)
                        self.assertEqual(screen.history[0].why, PUBLIC)
        self.reply(answer(action='nothing'))
        self.runner.process_pr(*KEY[:2])
        self.assertEqual(self.projection.screen(Runtime(repos=(REPO,)), 0).history[0].why, PRIVATE)

    def test_deploy_v2_parser_rejects_bad_contract(self):
        valid = dict(decision='approve', summary=PUBLIC, reason=PRIVATE)
        for bad in ({'decision': 'approve', 'reason': PRIVATE}, valid | {'summary': 1},
                    valid | {'summary': 'x' * 301}, valid | {'decision': 'comment'}, valid | {'body': 'bad'}):
            with self.subTest(bad=bad), self.assertRaises(UnclearAnswer):
                parse_deploy_answer_v2(bad)

    def assert_unclear_settles_once(self, obj, *, deployment=False):
        self.reply(obj)
        if deployment:
            self.gh.pr_snapshots.clear()
            run = make_run()
            self.gh.runs = [run]
            self.gh.pending[(REPO, run.run_id)] = [PENDING_PROD]
            self.gh.jobs_by_attempt[(REPO, run.run_id, run.attempt)] = [PLAN_JOB]
            self.gh.notifications = [(REPO, run.run_id, '2026-10-13T12:00:00Z')]
            key = (REPO, run.run_id, run.attempt, PENDING_PROD.env_id)
        else:
            key = KEY
        self.assertEqual(self.runner.tick(), {'unclear': 1})
        record = next(r for r in self.records() if r['event'] == 'unclear')
        self.assertEqual(tuple(record['key']), key)
        session = Path(record['session'])
        self.assertTrue(session.is_dir())
        self.assertTrue((session / 'stdout.json').is_file())
        self.assertFalse((session / 'decision.json').exists())
        self.assertFalse((session / 'rendered.md').exists())
        self.assertFalse(any(r['event'] in ('assessment', 'ignore', 'applied') for r in self.records()))
        self.assertEqual(self.journal.settled_outcome(key), 'unclear')
        screen = self.projection.screen(Runtime(repos=(REPO,)), 0)
        self.assertEqual(screen.watching[0].state, 'error')
        self.assertEqual(screen.history[0].state, 'error')
        self.assertIn(str(session), screen.watching[0].needs_you)
        self.assertEqual(EVENT_WORDS['unclear'], 'answer unclear')
        self.assertEqual(self.runner.tick(), {'final': 1})
        replay = Journal(self.journal.path)
        replay.replay()
        self.runner = Runner(self.settings, self.gh, replay, self.reviewer, notifier=Mock(), live_lock_held=True)
        self.assertEqual(self.runner.tick(), {'final': 1})
        self.assertEqual(replay.settled_outcome(key), 'unclear')
        self.engine.ask.assert_called_once()
        self.assertFalse(self.gh.writes)

    def test_invalid_string_fields_settle_unclear_through_reviewer_and_runner(self):
        for bad in ('\x00', '\ud800', '\udfff'):
            for field in ('action', 'summary', 'reason', 'notify', 'severity', 'origin', 'text', 'path', 'url'):
                with self.subTest(field=field, bad=ascii(bad)):
                    case = RenderedWriteTests()
                    case.setUp()
                    self.addCleanup(case.doCleanups)
                    if field in ('action', 'summary', 'reason', 'notify'):
                        obj = answer(**{field: 'text' + bad})
                    else:
                        obj = answer(findings=[FINDINGS[0] | {field: 'text' + bad}])
                    case.assert_unclear_settles_once(obj)

    def test_deployment_malformed_answers_settle_unclear_with_session(self):
        valid = dict(decision='approve', summary=PUBLIC, reason=PRIVATE)
        malformed = [dict(decision='approve', reason=PRIVATE), valid | {'summary': 1}]
        malformed += [valid | {field: 'text' + bad}
                      for field in ('decision', 'summary', 'reason') for bad in ('\x00', '\ud800', '\udfff')]
        for index, obj in enumerate(malformed):
            with self.subTest(case=index):
                case = RenderedWriteTests()
                case.setUp()
                self.addCleanup(case.doCleanups)
                case.assert_unclear_settles_once(obj, deployment=True)
