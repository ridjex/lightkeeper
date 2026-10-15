"""Runner lifetime and Textual Overview pilots; no network or model calls."""
import asyncio
from dataclasses import FrozenInstanceError, replace
import io
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch
import importlib.util
import unittest

HAS_UI = all(importlib.util.find_spec(name) is not None for name in ("rich", "textual"))

if HAS_UI:
    from rich.console import Console
    from textual.widgets import DataTable, Input, Static
    from rich.text import Text
    from lightkeeper.ui.textual_app import TextualApp

from lightkeeper import __main__ as cli
from lightkeeper.commands import Command, CommandInbox
from lightkeeper.engines.claude import ClaudeEngine
from lightkeeper.github import Gh, GhError, GhTimeout
from lightkeeper.reviewer import ClaudeReviewer, ReviewerError
from lightkeeper.journal import Journal, JournalError, Lock
from lightkeeper.models import Decision
from lightkeeper.ui.model import Runtime
from lightkeeper.ui.project import Observer, Projection
from lightkeeper.worker import RunnerThread
from tests.helpers import FakeGh, FakeReviewer, PENDING_PROD, make_pr, make_run, make_settings
from tests.test_ui import fixture_screen


def contents(app):
    output = io.StringIO()
    console = Console(file=output, width=200, height=40, markup=False, no_color=True, force_terminal=False)
    for widget in app.screen.walk_children():
        if isinstance(widget, Static):
            console.print(widget.content)
        elif isinstance(widget, DataTable):
            for key in widget.rows:
                console.print(Text(" ").join(widget.get_row(key)))
    return output.getvalue()


async def wait_until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


class RunnerTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.observer = Observer(Projection(), Runtime(repos=('org/repo',)))
        self.journal = Journal(self.root / 'journal.jsonl', observer=self.observer.feed)
        self.runner = cli.Runner(make_settings(), FakeGh(), self.journal, observer=self.observer,
                                 notifier=Mock(), live_lock_held=True)
        self.addCleanup(setattr, cli, '_plain', True)
        cli.configure_logging(self.root, plain=False)

    def test_wait_wake_latency_and_consumption(self):
        entered = threading.Event()
        def wait():
            entered.set()
            self.runner.wait(self.runner.stop, 60)
        thread = threading.Thread(target=wait)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.runner.wake.set()
            thread.join(.2)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, .2)
            self.assertFalse(self.runner.wake.is_set())
        finally:
            self.runner.stop.set()
            thread.join(1)

    def test_commands_coalesce_are_bounded_and_results_survive_snapshots(self):
        commands = self.runner.commands
        self.assertIsInstance(commands, queue.Queue)
        first = Command('first', 'wake')
        with self.assertRaises(FrozenInstanceError):
            first.kind = 'other'
        self.assertTrue(commands.put(first))
        self.assertFalse(commands.put(Command('second', 'wake')))
        self.assertEqual(commands.qsize(), 1)
        self.runner.wait(self.runner.stop, 60)
        result = self.observer.runtime.last_command
        self.assertEqual((result.id, result.outcome), ('first', 'done'))
        for _ in range(100):
            self.observer('phase', phase='checking')
        self.assertIs(self.observer.snapshot.runtime.last_command, result)
        for n in range(16):
            commands.put(Command(str(n), 'unsupported'))
        with self.assertRaises(queue.Full):
            commands.put(Command('full', 'wake'))
        self.runner.drain_commands()
        self.assertEqual(self.observer.runtime.last_command.outcome, 'unsupported')
        self.assertEqual(commands.unfinished_tasks, 0)

    def test_explicit_wakes_preserve_backoff_and_rate_limit_deadlines(self):
        for rate_limited in (False, True):
            with self.subTest(rate_limited=rate_limited):
                now, waits = [0], []
                self.runner._rate_limited = rate_limited
                self.runner._next_fast_poll = 0
                self.runner.light_poll = Mock(return_value='changed')
                self.runner.sleeping(120, 'backoff')
                retry_at = self.observer.runtime.next_check_at
                self.runner.commands.put(Command('initial', 'wake'))
                def wait(seconds):
                    if self.runner.wake.is_set():
                        return True
                    waits.append(seconds)
                    now[0] += seconds
                    if now[0] in (30, 60):
                        self.runner.commands.put(Command(str(now[0]), 'wake'))
                    return self.runner.wake.is_set()
                with patch.object(self.runner.wake, 'wait', side_effect=wait):
                    self.runner.wait(self.runner.stop, 120, allow_wake=rate_limited, mono=lambda: now[0])
                self.assertEqual(now[0], 120)
                self.assertEqual(self.observer.runtime.next_check_at, retry_at)
                self.assertEqual(waits, [15] * 8)  # block on the event for each light-poll slice
                self.assertTrue(self.runner.commands.empty())
                self.assertEqual(self.runner.commands.unfinished_tasks, 0)
                self.assertEqual(self.observer.runtime.last_command.id, '60')
                self.assertIn('paused until ', self.observer.runtime.last_command.text)
                if rate_limited:
                    self.runner.light_poll.assert_not_called()

    def test_stop_sets_wait_event_and_interrupts_without_polling(self):
        entered = threading.Event()
        event_wait = self.runner.wake.wait
        def wait(seconds):
            entered.set()
            return event_wait(seconds)
        with patch.object(self.runner.wake, 'wait', side_effect=wait) as blocked:
            thread = threading.Thread(target=lambda: self.runner.wait(self.runner.stop, 60))
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                self.runner.stop.set()
                thread.join(.2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(blocked.call_count, 1)
                self.assertGreater(blocked.call_args.args[0], 14)
            finally:
                self.runner.stop.set()
                thread.join(1)

    def test_uncertain_posts_are_journaled_before_late_stop(self):
        pr, run = make_pr(), make_run()
        self.runner.gh.pr_snapshots[(pr.repo, pr.number)] = [pr]
        self.runner.gh.runs = [run]
        self.runner.gh.pending[(run.repo, run.run_id)] = [PENDING_PROD]
        decision = Decision('approve', 'reason', 'ai', summary='Ready.',
                            data={'environment_id': PENDING_PROD.env_id})
        failures = [json.JSONDecodeError('invalid response', '', 0), KeyError('id'),
                    GhTimeout(['api'], 1), OSError('connection ended'), None]
        for kind in ('pr', 'deployment'):
            for late_stop in (False, True):
                for failure in failures:
                    with self.subTest(kind=kind, late_stop=late_stop, failure=failure):
                        self.runner.stop.clear()
                        method = 'submit_review' if kind == 'pr' else 'approve_deployment'
                        setattr(self.runner.gh, method, Mock(side_effect=failure, return_value=None))
                        writer = self.runner._write_pr if kind == 'pr' else self.runner._write_gate
                        def after_write(*args):
                            result = writer(*args)
                            # The exception was already caught with stop unset.
                            if late_stop:
                                self.runner.stop.set()
                            return result
                        key = (pr.repo, pr.number, pr.head_sha) if kind == 'pr' else (
                            run.repo, run.run_id, run.attempt, PENDING_PROD.env_id)
                        if kind == 'pr':
                            with patch.object(self.runner, '_write_pr', side_effect=after_write):
                                result = self.runner._settle_pr(key, pr, decision)
                        else:
                            result = self.runner._settle_deploy(key, decision, lambda: after_write(run, decision), 'fp')
                        self.assertEqual(result, 'unknown')
                        records = [json.loads(line) for line in self.journal.path.read_text().splitlines()]
                        self.assertEqual(next(r['event'] for r in reversed(records) if r['event'] != 'notify'), 'unknown')
                        self.assertTrue(self.journal.is_unknown(key))

    def test_definite_post_rejections_stay_errors_even_after_stop(self):
        pr, run = make_pr(), make_run()
        self.runner.gh.pr_snapshots[(pr.repo, pr.number)] = [pr]
        self.runner.gh.runs = [run]
        self.runner.gh.pending[(run.repo, run.run_id)] = [PENDING_PROD]
        decision = Decision('approve', 'reason', 'ai', summary='Ready.',
                            data={'environment_id': PENDING_PROD.env_id})
        for kind in ('pr', 'deployment'):
            for stopped in (False, True):
                with self.subTest(kind=kind, stopped=stopped):
                    self.runner.stop.clear()
                    def reject(*args):
                        if stopped:
                            self.runner.stop.set()
                        raise GhError([], 1, 'HTTP 422')
                    if kind == 'pr':
                        self.runner.gh.submit_review = reject
                        result = self.runner._settle_pr((pr.repo, pr.number, pr.head_sha), pr, decision)
                    else:
                        self.runner.gh.approve_deployment = reject
                        result = self.runner._settle_deploy((run.repo, run.run_id, run.attempt, PENDING_PROD.env_id),
                                                           decision, lambda: self.runner._write_gate(run, decision), 'fp')
                    self.assertEqual(result, 'error')

    def test_real_post_response_decoding_failures_are_unknown(self):
        pr, run = make_pr(), make_run()
        self.runner.gh.pr_snapshots[(pr.repo, pr.number)] = [pr]
        self.runner.gh.runs = [run]
        self.runner.gh.pending[(run.repo, run.run_id)] = [PENDING_PROD]
        decision = Decision('approve', 'reason', 'ai', summary='Ready.',
                            data={'environment_id': PENDING_PROD.env_id})
        for kind, response in (('pr', 'invalid JSON'), ('pr', '{}'), ('deployment', 'invalid JSON')):
            with self.subTest(kind=kind, response=response):
                gh = Gh(runner=Mock(return_value=subprocess.CompletedProcess([], 0, response, '')))
                if kind == 'pr':
                    self.runner.gh.submit_review = gh.submit_review
                    result = self.runner._settle_pr((pr.repo, pr.number, pr.head_sha), pr, decision)
                else:
                    self.runner.gh.approve_deployment = gh.approve_deployment
                    result = self.runner._settle_deploy((run.repo, run.run_id, run.attempt, PENDING_PROD.env_id),
                                                       decision, lambda: self.runner._write_gate(run, decision), 'fp')
                self.assertEqual(result, 'unknown')

    def test_commands_drained_between_items(self):
        self.runner.gh.search_review_requests = Mock(return_value=([('org/repo', 1), ('org/repo', 2)], False))
        observed = []
        def process(repo, number):
            if number == 1:
                self.runner.commands.put(Command('between', 'wake'))
            else:
                observed.append(self.observer.runtime.last_command.id)
            return 'skip'
        self.runner.process_pr = process
        self.runner.tick()
        self.assertEqual(observed, ['between'])

    def test_stop_after_last_pr_skips_deployment_discovery(self):
        self.runner.gh.search_review_requests = Mock(return_value=([('org/repo', 7)], False))
        self.runner.gh.approval_requests = Mock()
        def process(*args):
            self.runner.stop.set()
            return 'applied'
        self.runner.process_pr = process
        self.runner.tick()
        self.runner.gh.approval_requests.assert_not_called()
        self.assertEqual(self.observer.runtime.phase, 'idle')

    def test_stopped_gate_return_raise_and_missing_result(self):
        run = make_run()
        decision = Decision('approve', 'reason', 'ai', summary='Ready.',
                            data={'environment_id': PENDING_PROD.env_id})
        self.runner.gh.runs = [run]
        self.runner.gh.pending[(run.repo, run.run_id)] = [PENDING_PROD]
        for outcome in ('returned', 'raised', 'missing'):
            with self.subTest(outcome=outcome):
                self.runner.stop.clear()
                def submit(*args):
                    self.runner.stop.set()
                    if outcome == 'raised':
                        raise RuntimeError('connection ended')
                    return '[]' if outcome == 'returned' else None
                self.runner.gh.approve_deployment = submit
                key = (run.repo, run.run_id, run.attempt, PENDING_PROD.env_id)
                result = self.runner._settle_deploy(key, decision, lambda: self.runner._write_gate(run, decision), 'fp')
                self.assertEqual(result, 'applied' if outcome == 'returned' else 'unknown')
                record = json.loads(self.journal.path.read_text().splitlines()[-1])
                self.assertEqual(record['event'], result)

    def test_cancelled_model_does_not_settle_or_write(self):
        pr = make_pr()
        self.runner.gh.pr_snapshots[(pr.repo, pr.number)] = [pr]
        entered, released = threading.Event(), threading.Event()
        engine = Mock()
        engine.cancel.side_effect = released.set
        def review(*args):
            entered.set()
            released.wait(2)
            return Decision('approve', 'reason', 'ai', summary='Ready.')
        self.runner.reviewer = Mock(engine=engine, review_pr=review)
        worker = RunnerThread(self.runner, self.observer, lambda: self.runner.process_pr(pr.repo, pr.number),
                              join_timeout=.03)
        worker.thread.start()
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            worker.join()
            self.assertLess(time.monotonic() - started, .3)
            engine.cancel.assert_called_once_with()
            self.assertFalse(worker.thread.is_alive())
            self.assertFalse(self.runner.gh.writes)
            events = [json.loads(line)['event'] for line in self.journal.path.read_text().splitlines()]
            self.assertEqual(events, ['seen'])
        finally:
            released.set()
            worker.join()

    def test_engine_cancel_kills_real_process_and_prompt_uses_stdin(self):
        reviewer = ClaudeReviewer('test', 'Synthetic rules')
        engine = reviewer.engine
        errors = []
        # A synthetic local process exercises Popen + communicate, without invoking a model.
        command = [sys.executable, '-c', 'import sys,time; print(sys.stdin.readline(), flush=True); time.sleep(60)']
        def run():
            try:
                reviewer.review_pr(make_pr())
            except Exception as exc:
                errors.append(exc)
        command += ['--append-system-prompt', 'synthetic system']
        engine.command = Mock(return_value=command)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            deadline = time.monotonic() + 2
            while engine._process is None and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertIsNotNone(engine._process)
            self.assertNotIn('synthetic prompt', engine._process.args)
            engine.cancel()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertIsInstance(errors[0], ReviewerError)
            self.assertIsNone(engine._process)
            engine.cancel()  # idempotent after completion
        finally:
            engine.cancel()
            thread.join(1)

    def test_engine_real_subprocess_receives_prompt_only_on_stdin(self):
        engine = ClaudeEngine('test')
        command = [sys.executable, '-c',
                   'import json,sys; print(json.dumps({"structured_output": {"prompt": sys.stdin.read()}}))',
                   '--append-system-prompt', 'synthetic system']
        with patch.object(engine, 'command', return_value=command):
            result = engine.ask('synthetic system', 'synthetic request', {}, 2, self.root)
        self.assertEqual(result.parsed['prompt'], 'synthetic request')
        self.assertNotIn('synthetic request', command)
        self.assertIsNone(engine._process)

    def test_cancel_is_bounded_when_grandchild_holds_pipes(self):
        for escaped in (False, True):
            with self.subTest(escaped=escaped):
                ready = self.root / f'grandchild-{escaped}'
                grandchild = ('import os,pathlib,signal,sys,time; '
                              'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                              'pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)')
                parent = ('import subprocess,sys,time; '
                          f'subprocess.Popen([sys.executable, "-c", {grandchild!r}, {str(ready)!r}], '
                          f'start_new_session={escaped!r}); time.sleep(30)')
                reviewer = ClaudeReviewer('test', 'Synthetic rules')
                engine = reviewer.engine
                engine.command = Mock(return_value=[sys.executable, '-c', parent,
                                                     '--append-system-prompt', 'synthetic system'])
                errors = []
                def review():
                    try:
                        reviewer.review_pr(make_pr())
                    except Exception as exc:
                        errors.append(exc)
                thread = threading.Thread(target=review)
                thread.start()
                try:
                    deadline = time.monotonic() + 3
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(ready.exists())
                    proc = engine._process
                    self.assertEqual(os.getpgid(proc.pid), proc.pid)
                    started = time.monotonic()
                    engine.cancel()
                    self.assertLess(time.monotonic() - started, 5)
                    thread.join(max(0, 5 - (time.monotonic() - started)))
                    self.assertFalse(thread.is_alive())
                    self.assertIsInstance(errors[0], ReviewerError)
                    self.assertIsNotNone(proc.returncode, str(errors[0]))
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(proc.pid, os.WNOHANG)  # communicate/wait already reaped it
                    self.assertTrue(proc.stdout.closed)
                    self.assertTrue(proc.stderr.closed)
                    self.assertIsNone(engine._process)
                finally:
                    if ready.exists():
                        try:
                            os.kill(int(ready.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    engine.cancel()
                    thread.join(3)

    @unittest.skipUnless(HAS_UI, "Textual screen selection")
    def test_textual_default_and_plain_override(self):
        stdout = Mock(isatty=lambda: True)
        with patch.dict('os.environ', {'TERM': 'xterm', 'LIGHTKEEPER_UI': 'rich'}):
            self.assertIs(cli.live_screen_class(stdout=stdout), TextualApp)
            self.assertIsNone(cli.live_screen_class(True, stdout))

    def test_ui_has_no_github_client_import(self):
        import ast
        ui = Path(cli.__file__).parent / 'ui'
        for path in ui.rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn('github', node.module or '', str(path))
                    self.assertNotIn('Gh', [name.name for name in node.names], str(path))
                elif isinstance(node, ast.Import):
                    self.assertFalse(any('github' in name.name for name in node.names), str(path))

    def test_missing_textual_import_warns_once_and_falls_back_to_plain(self):
        import builtins
        original_import = builtins.__import__
        def import_without_textual(name, *args, **kwargs):
            if name == 'ui.textual_app':
                raise ImportError('Textual not installed')
            return original_import(name, *args, **kwargs)
        stdout = Mock(isatty=lambda: True)
        with patch.dict(os.environ, TERM='xterm'), patch('builtins.__import__', side_effect=import_without_textual), \
             patch('sys.stderr', new_callable=io.StringIO) as output:
            self.assertIsNone(cli.live_screen_class(stdout=stdout))
            self.assertEqual(output.getvalue().splitlines(), ['warning: Textual unavailable; using plain mode'])
            output.seek(0)
            output.truncate()
            self.assertIsNone(cli.live_screen_class(True, stdout))
            self.assertEqual(output.getvalue(), '')


@unittest.skipUnless(HAS_UI, "optional UI dependencies are not installed")
class PilotTests(IsolatedAsyncioTestCase):
    def app(self, screen=None):
        screen = screen or fixture_screen()
        screen = replace(screen, runtime=replace(screen.runtime, gh_login=screen.runtime.login, gh_confirmed=True))
        observer = Observer(Projection(), screen.runtime)
        observer.snapshot = screen
        runner = Mock(cancelled=False, commands=CommandInbox(threading.Event()))
        worker = Mock(runner=runner, poll_join=Mock(return_value=False))
        return TextualApp(observer, worker, opener=Mock())

    async def test_rows_are_literal_text_with_fixture_labels_and_glyphs(self):
        app = self.app()
        app.ascii = False
        async with app.run_test(size=(200, 40)) as pilot:
            await pilot.pause()
            output = contents(app)
            for value in ('gh reviewer ✓', 'WATCHING 7', 'HISTORY 5', '▲', '◆', '✗', '○', '✓',
                          'widget#1', 'widget#100/staging', 'check the retry limit', 'today', 'last check'):
                self.assertIn(value, output)
            for table in app.query(DataTable):
                for key in table.rows:
                    self.assertTrue(all(isinstance(cell, Text) for cell in table.get_row(key)))
            self.assertEqual(app.query_one('#watching', DataTable).row_count, 7)
            self.assertEqual(app.query_one('#history', DataTable).row_count, 5)

    async def test_single_cursor_walks_sections_and_needs_you_wraps(self):
        app = self.app()
        async with app.run_test(size=(140, 30)) as pilot:
            self.assertEqual(app.selected[0], 'alerts')
            await pilot.press('down')
            self.assertEqual((app.selected[0], app.selected[1].item.number), ('watching', 1))
            await pilot.press('up')
            self.assertEqual(app.selected[0], 'alerts')
            await pilot.press('tab')
            self.assertEqual(app.selected[0], 'watching')
            await pilot.press(*(['down'] * 7))
            self.assertEqual(app.selected[0], 'history')
            await pilot.press('up')
            self.assertEqual(app.selected[1].item.kind, 'deployment')
            await pilot.press('tab')
            self.assertEqual(app.selected[0], 'history')
            await pilot.press('tab')
            self.assertEqual(app.selected[0], 'alerts')
            await pilot.press('n')
            self.assertEqual(app.selected[1].item.number, 1)
            await pilot.press('n')
            self.assertEqual(app.selected[1].item.number, 1)
            self.assertEqual(sum(table.show_cursor for table in app.query(DataTable)), 1)

    async def test_open_pr_detail_deployment_url_and_wake(self):
        app = self.app()
        app.observer.snapshot = replace(app.observer.snapshot, log_path='long/path/' * 30)
        async with app.run_test(size=(200, 40)) as pilot:
            await pilot.press('n', 'enter')
            self.assertIn('Retry transient failures', contents(app))
            app.opener.assert_not_called()
            await pilot.press('o')
            await app.workers.wait_for_complete()
            app.opener.assert_called_once_with('https://github.com/example/widget/pull/1')
            await pilot.press('escape', 'tab', 'up', 'enter')
            await app.workers.wait_for_complete()
            app.opener.assert_called_with('https://github.com/example/widget/actions/runs/100')
            await pilot.press('r')
            command = app.worker.runner.commands.get_nowait()
            self.assertEqual(command.kind, 'wake')
            self.assertTrue(command.id)
            await pilot.press('?')
            self.assertIn('Tab section', contents(app))
            await pilot.press('?')
            self.assertIn('Tab section', contents(app))
            await pilot.press('1')
            self.assertIs(app.screen, app.overview)

    async def test_mouse_click_does_not_change_keyboard_selection(self):
        app = self.app()
        async with app.run_test(size=(200, 40)) as pilot:
            await pilot.press('tab')
            table = app.query_one('#watching', DataTable)
            selected = app.selected
            self.assertEqual(table.cursor_row, 0)
            self.assertTrue(await pilot.click(table, offset=(3, 1)))
            self.assertEqual(table.cursor_row, 0)
            self.assertEqual(app.selected, selected)
            await pilot.press('o', 'enter')
            await app.workers.wait_for_complete()
            app.opener.assert_called_once_with(selected[1].item.url)
            self.assertIn('Retry transient failures', contents(app))

    async def test_slow_url_opener_runs_off_app_thread_and_keeps_keys_responsive(self):
        app = self.app()
        calls, handled = [], []
        entered, finished, release = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def opener(url):
            calls.append((url, threading.current_thread().name))
            entered.set()
            release.wait(5)
            finished.set()
            return True
        app.opener = opener
        async with app.run_test(size=(200, 40)) as pilot:
            await pilot.press('tab')
            selected = app.selected
            app_thread = threading.current_thread().name
            await pilot.press('o')
            async with asyncio.timeout(1):
                while not entered.is_set():
                    await asyncio.sleep(.001)
            self.assertFalse(finished.is_set())
            move = app.action_move
            def record_move(delta):
                handled.append(time.monotonic())
                move(delta)
            with patch.object(app, 'action_move', side_effect=record_move):
                started = time.monotonic()
                await pilot.press('down')
            self.assertEqual(len(handled), 1)
            self.assertLess(handled[0] - started, .5)
            self.assertNotEqual(app.selected, selected)
            self.assertFalse(finished.is_set())
            release.set()
            await app.workers.wait_for_complete()
            self.assertEqual(calls[0][0], selected[1].item.url)
            self.assertNotEqual(calls[0][1], app_thread)

    async def test_url_open_failure_is_shown_in_footer(self):
        for opener in (Mock(return_value=False), Mock(side_effect=OSError('synthetic failure'))):
            with self.subTest(opener=opener):
                app = self.app()
                app.opener = opener
                async with app.run_test(size=(200, 40)) as pilot:
                    await pilot.press('tab', 'o')
                    await app.workers.wait_for_complete()
                    self.assertIn('Could not open GitHub URL', app.query_one('#footer', Static).content.plain)

    async def test_selection_survives_new_head_reorder_and_vanished_row(self):
        app = self.app()
        async with app.run_test(size=(200, 40)) as pilot:
            await pilot.press('tab', 'down')
            selected = app.selected[1]
            self.assertEqual(selected.item.number, 2)
            screen = app.observer.snapshot
            updated = replace(selected, item=replace(selected.item, head='new-head'), needs_you='look here', since=0)
            app.observer.snapshot = replace(screen, watching=tuple(updated if r == selected else r for r in screen.watching))
            app.refresh_snapshot()
            await pilot.pause()
            self.assertEqual(app.selected[1].item.identity, selected.item.identity)
            self.assertEqual(app.selected[1].item.head, 'new-head')
            self.assertEqual(app.cursor, 1)  # sorting moved the row, identity kept it selected
            nearest = app.rows[app.cursor + 1]
            app.observer.snapshot = replace(screen, watching=tuple(r for r in screen.watching if r != selected))
            app.refresh_snapshot()
            self.assertEqual(app.selected, nearest)
            # Deleting the final history row chooses its predecessor, then empty is safe.
            app.cursor = len(app.rows) - 1
            app.show_selection()
            previous = app.rows[-2]
            app.observer.snapshot = replace(app.observer.snapshot, history=screen.history[:-1])
            app.refresh_snapshot()
            self.assertEqual(app.selected, previous)
            app.observer.snapshot = replace(screen, watching=(), history=(), alerts=())
            app.refresh_snapshot()
            await pilot.press('up', 'down', 'tab', 'n', 'enter', 'o')
            self.assertIsNone(app.selected)
            app.opener.assert_not_called()

    async def test_poll_only_rebuilds_on_new_reference_and_updates_countdown(self):
        app = self.app()
        with patch('lightkeeper.ui.textual_app.time', Mock(monotonic=Mock(return_value=100))), \
             patch.object(app, 'rebuild_rows', wraps=app.rebuild_rows) as rebuild:
            async with app.run_test(size=(200, 40)) as pilot:
                await pilot.pause()
                self.assertIn('next check in 0:42', contents(app))
                count = rebuild.call_count
                with patch('lightkeeper.ui.textual_app.time', Mock(monotonic=Mock(return_value=110))):
                    app.refresh_snapshot()
                    self.assertIn('next check in 0:32', contents(app))
                    self.assertEqual(rebuild.call_count, count)
                    app.observer.snapshot = replace(app.observer.snapshot)
                    app.refresh_snapshot()
                    self.assertEqual(rebuild.call_count, count + 1)
                app.observer.snapshot = replace(app.observer.snapshot, alerts=())
                await pilot.pause(.3)  # exercise the actual 0.25 s timer
                self.assertEqual(app.query_one('#alerts', DataTable).row_count, 0)

    async def test_input_disables_global_keys_and_refresh_preserves_focus(self):
        app = self.app()
        async with app.run_test(size=(200, 40)) as pilot:
            field = Input()
            await app.mount(field)
            app.cursor = 2
            app.show_selection()
            field.focus()
            await pilot.press('q', 'r', 'o', 'n', '1', '2', '3', '?', 'enter')
            self.assertEqual(field.value, 'qron123?')
            app.worker.request_stop.assert_not_called()
            self.assertTrue(app.worker.runner.commands.empty())
            app.opener.assert_not_called()
            self.assertEqual(app.cursor, 2)
            for key in ('up', 'down', 'tab', 'ctrl+c'):
                field.focus()
                await pilot.press(key)
                self.assertEqual(app.cursor, 2, key)
                app.worker.request_stop.assert_not_called()
            field.focus()
            app.observer.snapshot = replace(app.observer.snapshot)
            app.refresh_snapshot()
            self.assertIs(app.focused, field)

    async def test_poll_updates_timed_cells_in_place_through_pause_expiry(self):
        app = self.app()
        screen = app.observer.snapshot
        paused = replace(screen.alerts[0], key='reviewer_paused', text='reviewer paused')
        waiting = replace(screen.watching[0], since=screen.wall_now - 5)
        reviewing = replace(screen.watching[1], state='reviewing')
        app.observer.snapshot = replace(screen, mono_at=100, alerts=(paused,),
                                        watching=(waiting, reviewing),
                                        runtime=replace(screen.runtime, phase_since=100, reviewer_paused_until=112))
        now = [100]
        app.clock = lambda: now[0]
        app.ascii = False
        async with app.run_test(size=(200, 40)) as pilot:
            await pilot.pause()
            tables = list(app.query(DataTable))
            rows = [tuple((key, id(row)) for key, row in table.rows.items()) for table in tables]
            identities = [app.row_identity(entry) for entry in app.rows]
            table = app.query_one('#watching', DataTable)
            age_column = table.ordered_columns[-1].key
            spinner = table.get_cell('1', '1').plain
            title = table.get_cell('0', '2')
            self.assertEqual(table.get_cell('0', age_column).plain.strip(), '5s')
            self.assertIn('resumes in 0:12', contents(app))
            with patch.object(app, 'rebuild_rows', wraps=app.rebuild_rows) as rebuild, \
                 patch.object(table, 'update_cell', wraps=table.update_cell) as update:
                now[0] = 110.25
                app.refresh_snapshot()
                self.assertEqual(table.get_cell('0', age_column).plain.strip(), '15s')
                self.assertEqual(table.get_cell('1', age_column).plain.strip(), '10s')
                self.assertNotEqual(table.get_cell('1', '1').plain, spinner)
                self.assertIn('resumes in 0:01', contents(app))
                self.assertEqual([(c.args[0], c.args[1]) for c in update.call_args_list],
                                 [('0', age_column), ('1', '1'), ('1', age_column)])
                for instant in (112, 113):
                    now[0] = instant
                    app.refresh_snapshot()
                    self.assertIn('resumes in 0:00', contents(app))
                rebuild.assert_not_called()
            self.assertIs(table.get_cell('0', '2'), title)
            self.assertEqual([tuple((key, id(row)) for key, row in t.rows.items()) for t in tables], rows)
            self.assertEqual([t.row_count for t in tables], [len(r) for r in rows])
            self.assertEqual([app.row_identity(entry) for entry in app.rows], identities)

    async def test_hostile_title_rendered_on_screen_and_ascii_no_color(self):
        app = self.app()
        row = app.observer.snapshot.watching[0]
        hostile = replace(row, item=replace(row.item, title='[@click=app.quit]\x1b[2J', author='[red]author'))
        app.observer.snapshot = replace(app.observer.snapshot, watching=(hostile, *app.observer.snapshot.watching[1:]))
        async with app.run_test(size=(200, 40)) as pilot:
            await pilot.pause()
            # Inspect actual rendered terminal strips as well as cell objects.
            output = '\n'.join(''.join(strip.text for strip in line)
                               for line in app.screen._compositor.render_full_update().strips)
            self.assertIn('[@click=app.quit]', output)
            self.assertIn('[red]author', output)
            self.assertNotIn('\x1b', output)
            app.worker.request_stop.assert_not_called()
        with patch.dict(os.environ, LIGHTKEEPER_ASCII='1', NO_COLOR='1'):
            app = self.app()
            app.ascii = True
            async with app.run_test(size=(140, 30)) as pilot:
                output = contents(app)
                self.assertTrue(app.no_color)
                for value in ('gh reviewer ok', '^', '+', 'x', 'o', 'c', '-', '#', 'approved', 'changes'):
                    self.assertIn(value, output)
                for value in ('✓', '✗', '○', '✎', '◆', '▲'):
                    self.assertNotIn(value, output)

    async def test_narrow_columns_resize_and_scroll(self):
        from lightkeeper.models import CICounts
        app = self.app()
        app.ascii = False
        row = replace(app.observer.snapshot.watching[0], ci=CICounts(4, 1, 1, 1, 1))
        row = replace(row, item=replace(row.item, author='contributor-long-name'))
        rows = tuple(replace(row, item=replace(row.item, number=n)) for n in range(20))
        app.observer.snapshot = replace(app.observer.snapshot, watching=rows)
        async with app.run_test(size=(100, 20)) as pilot:
            table = app.query_one('#watching', DataTable)
            self.assertEqual(len(table.columns), 8)
            self.assertIn('contributor-l…', contents(app))
            self.assertIn('✓1 ○1 ✗1 –1', contents(app))
            await pilot.press('tab', *(['down'] * 19))
            self.assertEqual(app.selected[1].item.number, 19)
            self.assertGreater(table.scroll_y, 0)
            await pilot.resize_terminal(140, 30)
            app.refresh_snapshot()
            self.assertEqual(len(table.columns), 10)
            self.assertEqual(app.selected[1].item.number, 19)

    async def test_render_failure_logs_once_then_recovers(self):
        app = self.app()
        async with app.run_test(size=(200, 40)):
            app.observer.snapshot = replace(app.observer.snapshot)
            with patch.object(app, 'rebuild_rows', side_effect=ValueError('bad frame')):
                with self.assertLogs('lightkeeper', level='ERROR') as logs:
                    app.refresh_snapshot()
                    app.refresh_snapshot()
                self.assertEqual(len(logs.records), 1)
                self.assertIn('render error (see run.log)', contents(app))
            app.refresh_snapshot()
            self.assertNotIn('render error', contents(app))

    async def test_footer_shows_queued_and_last_command_without_markup(self):
        observer = Observer(Projection(), Runtime(repos=('org/repo',)))
        with tempfile.TemporaryDirectory() as tmp:
            runner = cli.Runner(make_settings(), FakeGh(), Journal(Path(tmp) / 'journal.jsonl'), observer=observer)
            worker = Mock(runner=runner, poll_join=Mock(return_value=False))
            app = TextualApp(observer, worker)
            async with app.run_test(size=(200, 40)) as pilot:
                await pilot.press('r')
                app.refresh_snapshot()
                self.assertIn('queued', contents(app).splitlines()[0])
                runner.drain_commands()
                app.refresh_snapshot()
                self.assertIn('checking now', app.query_one('#footer', Static).content.plain)
                self.assertNotIn('queued', contents(app))
                observer('command_done', id='literal', outcome='done', text='[bold]paused until later[/bold]')
                app.refresh_snapshot()
                self.assertIn('[bold]paused until later[/bold]', app.query_one('#footer', Static).content.plain)

    async def test_fixture_literal_title_refresh_and_keys(self):
        screen = fixture_screen()
        row = screen.watching[0]
        screen = replace(screen, runtime=replace(screen.runtime, gh_login=screen.runtime.login, gh_confirmed=True),
                         watching=(replace(row, item=replace(row.item, title='[@click=app.quit]')), *screen.watching[1:]))
        observer = Observer(Projection(), screen.runtime)
        observer.snapshot = screen
        with tempfile.TemporaryDirectory() as tmp:
            runner = cli.Runner(make_settings(), FakeGh(), Journal(Path(tmp) / 'journal.jsonl'), observer=observer)
            def run():
                while not runner.cancelled:
                    runner.wait(runner.stop, 60)
            worker = RunnerThread(runner, observer, run)
            worker.thread.start()
            app = TextualApp(observer, worker)
            try:
                async with app.run_test(size=(200, 40)) as pilot:
                    await pilot.pause()
                    text = contents(app)
                    self.assertIn('reviewer', text)
                    self.assertIn('WATCHING', text)
                    self.assertIn('[@click=app.quit]', text)
                    self.assertNotIsInstance(app.query_one(Static).content, str)
                    await pilot.press('r')
                    await wait_until(lambda: observer.runtime.last_command is not None)
                    self.assertEqual(observer.runtime.last_command.outcome, 'done')
                    await pilot.press('ctrl+c')
                    await wait_until(lambda: not app.is_running and not app.worker.thread.is_alive())
                self.assertFalse(worker.thread.is_alive())
                self.assertEqual(app.return_code, 0)
            finally:
                worker.join()


@unittest.skipUnless(HAS_UI, "optional UI dependencies are not installed")
class MainThreadTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        from tests.helpers import isolate_cli
        isolate_cli(self, temp.name)

    def run_main(self, target, scenario, *, timeout=.02):
        """Real main/locks/runner thread; replace only the terminal driver and external clients."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings()
            gh = FakeGh()
            gh.pr_snapshots[('org/repo', 7)] = [make_pr()]
            lock_paths = [root / 'state' / 'lock', root / 'live-lock']
            reviewer = FakeReviewer([Decision('approve', 'reason', 'ai', summary='Ready.')])
            owner = self
            class HeadlessApp(TextualApp):
                def run(self):
                    self.worker.join_timeout = timeout
                    async def test():
                        async with self.run_test(size=(200, 40)) as pilot:
                            await scenario(self, pilot, lock_paths)
                    asyncio.run(test())
            def loop(args, config, runner, stop):
                owner.assertEqual(threading.current_thread().name, 'runner')
                owner.assertFalse(threading.current_thread().daemon)
                target(runner, gh)
            with patch.object(cli, 'Gh', return_value=gh), patch.object(cli, 'ClaudeReviewer', return_value=reviewer), \
                 patch.object(cli, 'live_screen_class', return_value=HeadlessApp), \
                 patch.object(cli, 'live_lock_path', return_value=lock_paths[1]), \
                 patch.object(cli, 'configure_diagnostics'), patch.object(cli, '_loop', side_effect=loop), \
                 patch('sys.stdout', new_callable=io.StringIO) as output:
                code = cli.main(['--allow-live', '--config-dir', str(settings.config_dir),
                                 '--state-dir', str(root / 'state')])
            self.assertIn('lightkeeper stopped', output.getvalue())
            self.assertIn('needs you:', output.getvalue())
            self.assertIn('alerts:', output.getvalue())
            for path in lock_paths:
                with Lock(path):
                    pass
            journal = root / 'state' / 'journal.jsonl'
            records = [json.loads(line) for line in journal.read_text().splitlines()] if journal.exists() else []
            self.last_log = (root / 'state' / 'run.log').read_text()
            return code, records

    def test_quit_slow_review_journals_then_releases_both_locks(self):
        for outcome in ('returned', 'raised', 'missing'):
            with self.subTest(outcome=outcome):
                entered, release = threading.Event(), threading.Event()
                def target(runner, gh):
                    def submit(*args):
                        entered.set()
                        if not release.wait(5):
                            raise RuntimeError('test timed out')
                        if outcome == 'raised':
                            raise RuntimeError('connection ended after stop')
                        return 42 if outcome == 'returned' else None
                    gh.submit_review = submit
                    runner.process_pr('org/repo', 7)
                async def scenario(app, pilot, paths):
                    try:
                        await wait_until(entered.is_set)
                        self.assertTrue(entered.is_set())
                        await pilot.press('q')
                        await wait_until(lambda: time.monotonic() >= app.worker.deadline)
                        app.refresh_snapshot()  # Beyond the join deadline, writing must still finish.
                        self.assertTrue(app.worker.thread.is_alive())
                        self.assertIn('finishing repo#7', contents(app))
                        for path in paths:
                            with self.assertRaises(JournalError):
                                with Lock(path):
                                    pass
                    finally:
                        release.set()
                    await wait_until(lambda: not app.is_running and not app.worker.thread.is_alive())
                    self.assertFalse(app.worker.thread.is_alive())
                code, records = self.run_main(target, scenario)
                self.assertEqual(code, 0)
                self.assertEqual(records[-1]['event'], 'applied' if outcome == 'returned' else 'unknown')

    def test_crashed_runner_alert_and_exit_code(self):
        def target(runner, gh):
            raise RuntimeError('synthetic crash')
        async def scenario(app, pilot, paths):
            await wait_until(lambda: not app.is_running and not app.worker.thread.is_alive())
            alert = next(a for a in app.observer.snapshot.alerts if a.key == 'runner_crashed')
            self.assertEqual(alert.level, 'error')
            self.assertEqual(app.return_code, 1)
            self.assertFalse(app.worker.thread.is_alive())
        code, _ = self.run_main(target, scenario)
        self.assertEqual(code, 1)
        self.assertIn('runner crashed', self.last_log)
        self.assertIn('synthetic crash', self.last_log)

    def test_quit_during_model_call_cancels_and_joins(self):
        entered, release = threading.Event(), threading.Event()
        engine = Mock()
        engine.cancel.side_effect = release.set
        def target(runner, gh):
            def review(*args):
                entered.set()
                release.wait(2)
                return Decision('approve', 'reason', 'ai', summary='Ready.')
            runner.reviewer = Mock(engine=engine, review_pr=review)
            runner.process_pr('org/repo', 7)
        async def scenario(app, pilot, paths):
            try:
                await wait_until(entered.is_set)
                self.assertTrue(entered.is_set())
                started = time.monotonic()
                await pilot.press('q')
                await wait_until(lambda: not app.is_running and not app.worker.thread.is_alive())
                self.assertFalse(app.worker.thread.is_alive())
                self.assertLess(time.monotonic() - started, .8)
                engine.cancel.assert_called_once_with()
            finally:
                release.set()
        code, records = self.run_main(target, scenario)
        self.assertEqual(code, 0)
        self.assertEqual([r['event'] for r in records], ['seen'])

    def test_signals_request_stop_and_join(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                def target(runner, gh):
                    runner.wait(runner.stop, 60)
                async def scenario(app, pilot, paths):
                    signal.raise_signal(signum)
                    await wait_until(lambda: not app.is_running and not app.worker.thread.is_alive())
                    self.assertTrue(app.worker.runner.cancelled)
                    self.assertFalse(app.worker.thread.is_alive())
                old = signal.getsignal(signum)
                code, _ = self.run_main(target, scenario)
                self.assertEqual(code, 0)
                self.assertIs(signal.getsignal(signum), old)
