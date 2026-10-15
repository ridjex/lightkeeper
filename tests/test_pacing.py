"""Offline screen pacing and diagnostics with an explicitly advanced monotonic clock."""
from dataclasses import replace
from datetime import datetime
import importlib.util
import json
import os
import unittest
from unittest.mock import Mock, patch

from tests.test_ui import fixture_screen

HAS_UI = all(importlib.util.find_spec(name) is not None for name in ("rich", "textual"))
if HAS_UI:
    from textual import events
    from textual.widgets import Static
    from lightkeeper.ui.agent import AgentPanel
    from lightkeeper.ui.textual_app import TextualApp


class Clock:
    now = 100.

    def __call__(self):
        return self.now


class PacingFixture:
    def setUp(self):
        self.mono = Clock()
        self.enterContext(patch.dict(os.environ, LIGHTKEEPER_SCREEN_TRACE="0"))

    def app(self, state_dir=""):
        screen = fixture_screen()
        screen = replace(screen, alerts=(), watching=(), history=(),
                         runtime=replace(screen.runtime, config_dir="", state_dir=state_dir))
        worker = Mock(runner=Mock(cancelled=False, commands=Mock(empty=lambda: True)),
                      poll_join=Mock(return_value=False))
        return TextualApp(Mock(snapshot=screen), worker, mono=self.mono, clock=self.mono)

    def frame(self, app, late=0):
        self.mono.now = app._idle_due + late
        app.animate_idle()


@unittest.skipUnless(HAS_UI, "optional UI dependencies are not installed")
class PacingTests(PacingFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app_instance = self.app()
        self.app_instance._idle_visible = True
        self.app_instance.overview = Mock(scrollbar_size_vertical=0)
        self.refresh = self.enterContext(patch.object(self.app_instance, "refresh_idle"))

    def test_on_time_sequence_keeps_ten_fps_and_scene_clock(self):
        app = self.app_instance
        for _ in range(90):
            self.frame(app)
        self.assertEqual(app._idle_interval, .1)
        self.assertEqual(app._idle_skipped, 0)
        self.assertEqual(self.refresh.call_count, 90)
        self.assertEqual(self.refresh.call_args.args[1], self.mono.now)
        self.assertEqual(app.screen_rate(), "Screen: 10 fps")

    def test_one_late_frame_skips_then_thirty_on_time_recover(self):
        app = self.app_instance
        tempo = app.snapshot.runtime.tempo
        self.frame(app, .11)
        self.refresh.assert_not_called()
        self.assertEqual(app._idle_interval, .2)
        self.assertEqual(app._idle_skipped, 1)
        self.assertEqual(app.screen_rate(), "Screen: 5 fps, terminal behind (1 frame skipped)")
        for _ in range(29):
            self.frame(app)
            self.assertEqual(app._idle_interval, .2)
        self.frame(app)
        self.assertEqual(app._idle_interval, .1)
        self.assertEqual(self.refresh.call_count, 30)
        self.assertEqual(app.snapshot.runtime.tempo, tempo)
        self.assertNotIn("terminal behind", app.screen_rate())

    def test_ladder_saturates_and_late_frame_resets_recovery(self):
        app = self.app_instance
        for interval in (.2, .5, 1, 1):
            self.frame(app, app._idle_interval + .01)
            self.assertEqual(app._idle_interval, interval)
            self.assertAlmostEqual(app._idle_due, self.mono.now + interval)
            self.frame(app)    # the re-armed timer's first tick is on time by definition and draws
        self.assertEqual(self.refresh.call_count, 4)
        for _ in range(28):          # 29 on-time frames since the last skip
            self.frame(app)
        self.assertEqual(app._idle_interval, 1)
        self.frame(app, 1.01)        # a late frame resets the recovery count
        self.frame(app)              # the re-armed timer's first tick
        for _ in range(28):
            self.frame(app)
        self.assertEqual(app._idle_interval, 1)
        self.frame(app)              # the 30th on-time frame steps down
        self.assertEqual(app._idle_interval, .5)

    def test_blur_caps_at_one_fps_and_focus_preserves_adaptive_rate(self):
        app = self.app_instance
        self.frame(app, .11)
        app.on_app_blur(events.AppBlur())
        self.assertEqual(app._idle_interval, 1)
        for _ in range(35):
            self.frame(app)
        self.frame(app, 1.5)      # late while blurred: skipped, but the step is not billed after focus
        self.assertEqual((app._idle_interval, app._idle_step, app._idle_skipped), (1, 1, 2))
        app.on_app_focus(events.AppFocus())
        self.assertEqual(app._idle_interval, .2)
        for _ in range(30):
            self.frame(app)
        app.on_app_blur(events.AppBlur())
        app.on_app_focus(events.AppFocus())
        self.assertEqual(app._idle_interval, .1)

    def test_writer_backlog_is_the_primary_signal(self):
        app = self.app_instance
        queue = Mock(qsize=Mock(return_value=4))
        app._driver = Mock(_writer_thread=Mock(_queue=queue), _size=(202, 40))
        self.frame(app)               # on time, but the previous frame is still queued
        self.refresh.assert_not_called()
        self.assertEqual((app._idle_interval, app._idle_skipped), (.2, 1))
        queue.qsize.return_value = 3  # one frame's own three writes are not a backlog
        self.frame(app)
        self.refresh.assert_called_once()
        queue.qsize.return_value = 0
        self.frame(app, .5)           # a whole interval late still counts: the loop itself was held up
        self.assertEqual((self.refresh.call_count, app._idle_skipped), (1, 2))

    def test_rearmed_timer_is_not_judged_by_the_old_grid(self):
        app = self.app_instance
        self.frame(app, .11)          # skipped and re-armed
        self.assertTrue(app._idle_rearmed)
        self.frame(app, 5)            # the new timer's first tick is on time by definition
        self.refresh.assert_called_once()
        self.assertFalse(app._idle_rearmed)
        self.assertEqual(app._idle_due, self.mono.now + .2)
        self.frame(app, .3)           # from now on lateness counts again
        self.assertEqual(app._idle_skipped, 2)

    def test_hidden_calls_do_not_skip_or_recover(self):
        app = self.app_instance
        app._idle_visible = False
        self.frame(app, 100)
        self.refresh.assert_not_called()
        self.assertEqual(app._idle_skipped, 0)
        self.assertEqual(app._idle_on_time, 0)


@unittest.skipUnless(HAS_UI, "optional UI dependencies are not installed")
class PacingPilots(PacingFixture, unittest.IsolatedAsyncioTestCase):
    async def test_timer_replacement_agent_line_and_visibility_deadline(self):
        app = self.app()
        async with app.run_test(size=(202, 40)):
            app._refresh_timer.stop()
            old_timer = app._idle_timer
            with patch.object(old_timer, "stop", wraps=old_timer.stop) as stop, \
                 patch.object(app, "set_interval", wraps=app.set_interval) as start, \
                 patch.object(app, "refresh_idle", wraps=app.refresh_idle) as refresh:
                self.frame(app)          # mounting re-armed the timer; its first tick draws
                refresh.assert_called_once()
                self.frame(app, .11)
                refresh.assert_called_once()
                stop.assert_called_once()
                start.assert_called_once_with(.2, app.animate_idle, pause=False)
            app._idle_timer.stop()
            await app.push_screen(AgentPanel())
            self.assertIn("Screen: 5 fps, terminal behind (1 frame skipped)",
                          app.screen.query_one("#agent-body", Static).content.plain)
            self.assertFalse(app._idle_visible)
            self.mono.now += 100
            await app.pop_screen()
            self.assertTrue(app._idle_visible)
            self.assertAlmostEqual(app._idle_due, self.mono.now + .2)
            with patch.object(app, "refresh_idle", wraps=app.refresh_idle) as refresh:
                self.frame(app)
                refresh.assert_called_once()
            self.assertEqual(app._idle_skipped, 1)

    async def test_trace_window_goes_to_the_logger_and_resets(self):
        with patch.dict(os.environ, LIGHTKEEPER_SCREEN_TRACE="1"):
            app = self.app()
            with patch.object(app, "set_interval", wraps=app.set_interval) as start:
                async with app.run_test(size=(202, 40)):
                    self.assertEqual(sum(call.args[0] == 5 for call in start.call_args_list), 1)
                    app._refresh_timer.stop()
                    app._idle_timer.stop()
                    app._trace_timer.stop()
                    with self.assertLogs("lightkeeper", "INFO"):
                        app.write_screen_trace()   # begin a clean window after mounting
                    refresh = app.refresh_idle
                    def draw(*args, **kwargs):
                        refresh(*args, **kwargs)
                        self.mono.now += .02
                    with patch.object(app, "refresh_idle", side_effect=draw):
                        self.frame(app)
                    self.frame(app, .15)
                    app._idle_timer.stop()
                    for elapsed in (.01, .03):
                        def snapshot():
                            self.mono.now += elapsed
                        with patch.object(app, "_refresh_snapshot", side_effect=snapshot):
                            app.refresh_snapshot()
                    with self.assertLogs("lightkeeper", "INFO") as logs:
                        app.write_screen_trace()
                        app.write_screen_trace()
                    first, empty = (json.loads(line.split("screen-trace ", 1)[1]) for line in logs.output)
                    self.assertEqual(datetime.fromisoformat(first["utc"]).utcoffset().total_seconds(), 0)
                    self.assertEqual(first["interval"], .2)
                    self.assertEqual((first["rendered"], first["skipped"], first["backlog"]), (1, 1, 0))
                    self.assertAlmostEqual(first["max_lateness"], .15)
                    self.assertEqual((first["width"], first["height"]), (202, 40))
                    for name, maximum in (("animate_idle", .02), ("refresh_snapshot", .03)):
                        self.assertEqual(first[name]["calls"], 2)
                        self.assertAlmostEqual(first[name]["max"], maximum)
                    self.assertEqual((empty["rendered"], empty["skipped"], empty["max_lateness"]), (0, 0, 0))
                    self.assertEqual(empty["animate_idle"], {"calls": 0, "max": 0})

    async def test_no_trace_timer_or_lines_without_opt_in(self):
        with patch.dict(os.environ):
            os.environ.pop("LIGHTKEEPER_SCREEN_TRACE", None)
            app = self.app()
            with patch.object(app, "set_interval", wraps=app.set_interval) as start:
                async with app.run_test(size=(202, 40)):
                    self.assertIsNone(app._screen_trace)
                    self.assertNotIn(5, [call.args[0] for call in start.call_args_list])
                    with self.assertNoLogs("lightkeeper"):
                        app.write_screen_trace()
