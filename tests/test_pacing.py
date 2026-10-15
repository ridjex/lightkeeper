"""Offline screen pacing and diagnostics with an explicitly advanced monotonic clock."""
from dataclasses import replace
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import tempfile
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
        self.assertEqual(app.screen_rate(), "Screen: 5 fps, terminal behind (1 frames skipped)")
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
        self.refresh.assert_not_called()
        for _ in range(29):
            self.frame(app)
        self.frame(app, 1.01)
        for _ in range(29):
            self.frame(app)
        self.assertEqual(app._idle_interval, 1)
        self.frame(app)
        self.assertEqual(app._idle_interval, .5)

    def test_blur_caps_at_one_fps_and_focus_preserves_adaptive_rate(self):
        app = self.app_instance
        self.frame(app, .11)
        app.on_app_blur(events.AppBlur())
        self.assertEqual(app._idle_interval, 1)
        for _ in range(35):
            self.frame(app)
        self.assertEqual(app._idle_interval, 1)
        app.on_app_focus(events.AppFocus())
        self.assertEqual(app._idle_interval, .2)
        for _ in range(30):
            self.frame(app)
        app.on_app_blur(events.AppBlur())
        app.on_app_focus(events.AppFocus())
        self.assertEqual(app._idle_interval, .1)

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
                self.frame(app, .11)
                refresh.assert_not_called()
                stop.assert_called_once()
                start.assert_called_once_with(.2, app.animate_idle, pause=False)
            app._idle_timer.stop()
            await app.push_screen(AgentPanel())
            self.assertIn("Screen: 5 fps, terminal behind (1 frames skipped)",
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

    async def test_trace_window_timing_append_permissions_and_timer(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, LIGHTKEEPER_SCREEN_TRACE="1"):
            app = self.app(tmp)
            with patch.object(app, "set_interval", wraps=app.set_interval) as start:
                async with app.run_test(size=(202, 40)):
                    self.assertEqual(sum(call.args[0] == 5 for call in start.call_args_list), 1)
                    app._refresh_timer.stop()
                    app._idle_timer.stop()
                    app._trace_timer.stop()
                    # Begin a clean diagnostic window after mounting.
                    app.write_screen_trace()
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
                    path = Path(tmp) / "screen.log"
                    path.chmod(0o644)  # Existing diagnostics are made private too.
                    app.write_screen_trace()
                    record = json.loads(path.read_text(encoding="utf-8").splitlines()[1])
                    self.assertEqual(datetime.fromisoformat(record["utc"]).utcoffset().total_seconds(), 0)
                    self.assertEqual(record["interval"], .2)
                    self.assertEqual((record["rendered"], record["skipped"]), (1, 1))
                    self.assertAlmostEqual(record["max_lateness"], .15)
                    self.assertEqual((record["width"], record["height"]), (202, 40))
                    for name, maximum, mean in (("animate_idle", .02, .01), ("refresh_snapshot", .03, .02)):
                        self.assertEqual(record[name]["calls"], 2)
                        self.assertAlmostEqual(record[name]["max"], maximum)
                        self.assertAlmostEqual(record[name]["mean"], mean)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    app.write_screen_trace()
                    empty = json.loads(path.read_text(encoding="utf-8").splitlines()[2])
                    self.assertEqual((empty["rendered"], empty["skipped"], empty["max_lateness"]), (0, 0, 0))
                    self.assertEqual(empty["animate_idle"], {"calls": 0, "max": 0, "mean": 0})

    async def test_no_trace_file_or_timer_without_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ):
            os.environ.pop("LIGHTKEEPER_SCREEN_TRACE", None)
            app = self.app(tmp)
            with patch.object(app, "set_interval", wraps=app.set_interval) as start:
                async with app.run_test(size=(202, 40)):
                    self.assertIsNone(app._screen_trace)
                    self.assertNotIn(5, [call.args[0] for call in start.call_args_list])
                    app.write_screen_trace()
                    self.assertEqual(list(Path(tmp).iterdir()), [])

    async def test_trace_empty_missing_and_unwritable_directory_are_silent(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, LIGHTKEEPER_SCREEN_TRACE="1"):
            app = self.app()
            async with app.run_test(size=(202, 40)):
                app.write_screen_trace()
                app.observer.snapshot = replace(app.snapshot, runtime=replace(app.snapshot.runtime, state_dir=tmp))
                with patch("lightkeeper.ui.screen_trace.os.open", side_effect=PermissionError):
                    app.write_screen_trace()
                missing = Path(tmp) / "missing"
                app.observer.snapshot = replace(app.snapshot, runtime=replace(app.snapshot.runtime, state_dir=str(missing)))
                app.write_screen_trace()
                self.assertEqual(list(Path(tmp).iterdir()), [])
