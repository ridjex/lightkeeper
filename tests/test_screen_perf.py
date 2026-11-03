"""Synthetic full-screen cost, including Textual composition and terminal serialization."""
import asyncio
from dataclasses import replace
import importlib.util
import os
import time
import unittest
from unittest.mock import Mock, patch

from tests.test_ui import fixture_screen
from lightkeeper.ui.model import Alert

HAS_UI = all(importlib.util.find_spec(name) is not None for name in ("rich", "textual"))
if HAS_UI:
    from lightkeeper.ui.textual_app import TextualApp


def performance_screen():
    screen = fixture_screen()
    watching = tuple(replace(screen.watching[i % len(screen.watching)],
                             item=replace(screen.watching[i % len(screen.watching)].item, number=100 + i),
                             needs_you=None) for i in range(13))
    history = tuple(replace(screen.history[i % len(screen.history)],
                            item=replace(screen.history[i % len(screen.history)].item, number=200 + i))
                    for i in range(200))
    return replace(screen, watching=watching, history=history, mono_at=100,
                   alerts=(Alert("one", "warning", "Synthetic warning", 90),
                           Alert("two", "warning", "Another warning", 90)),
                   runtime=replace(screen.runtime, config_dir="", clock=True, moon=True, events=True))


def performance_app(now):
    worker = Mock(runner=Mock(cancelled=False, commands=Mock(empty=lambda: True)),
                  poll_join=Mock(return_value=False))
    app = TextualApp(Mock(snapshot=performance_screen()), worker, clock=lambda: now[0])
    app.ascii = False
    app.no_color = False
    return app


async def measure_frames(app, pilot, now, *, snapshots=False, frames=60):
    """Drain real queued paints, counting main-thread CPU rather than timer sleeps.

    Headless Textual omits terminal serialization; explicitly include it, without a PTY.
    Profiling can wrap this coroutine; do not use instrumented times as budget results.
    """
    original_display = app._display
    paints = 0

    def display(screen, update):
        nonlocal paints
        if update is not None:
            update.render_segments(app.console)
            paints += 1
        original_display(screen, update)

    app._display = display
    started = time.thread_time()
    try:
        for i in range(frames):
            now[0] = 100 + i / 10
            app._idle_due = app.mono()
            app.animate_idle()
            if snapshots and i % 5 in (0, 2):
                app.refresh_snapshot()  # Four refreshes per simulated second.
            await pilot.pause(.001)
    finally:
        app._display = original_display
    return (time.thread_time() - started) / frames, paints


@unittest.skipUnless(HAS_UI and os.environ.get("LIGHTKEEPER_BENCH"), "set LIGHTKEEPER_BENCH=1 to time frames")
class ScreenBenchTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_frame_budget(self):
        # asyncio debug instruments every message, overwhelming a millisecond-scale budget.
        loop = asyncio.get_running_loop()
        debug = loop.get_debug()
        loop.set_debug(False)
        self.addCleanup(loop.set_debug, debug)
        now = [100.]
        with patch.dict(os.environ, LIGHTKEEPER_SCREEN_TRACE="0"), \
             patch("lightkeeper.ui.idle._sky", return_value=(12., .5)):
            app = performance_app(now)
            async with app.run_test(size=(200, 67)) as pilot:
                app._idle_timer.stop()
                app._refresh_timer.stop()
                await pilot.pause()
                for snapshots in (False, True):
                    samples = []
                    for _ in range(3):
                        elapsed, paints = await measure_frames(app, pilot, now, snapshots=snapshots)
                        self.assertGreaterEqual(paints, 59)  # The initial pose may match the mounted picture.
                        self.assertFalse(app._render_failed)
                        samples.append(elapsed)
                    print(f"screen bench snapshots={snapshots}: "
                          + ", ".join(f"{value * 1000:.3f} ms/frame" for value in samples))
                    if not snapshots:
                        idle_best = min(samples)
                self.assertLess(idle_best, .005)


@unittest.skipUnless(HAS_UI, "optional UI dependencies are not installed")
class ScreenRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_status_does_not_invalidate_widgets(self):
        now = [100.1]
        app = performance_app(now)
        async with app.run_test(size=(200, 67)):
            app._idle_timer.stop()
            app._refresh_timer.stop()
            app.refresh_snapshot()
            from contextlib import ExitStack
            with ExitStack() as stack:
                updates = [stack.enter_context(patch.object(app.query_one(selector), method))
                           for selector, method in (("#header", "update"), ("#footer", "update"),
                                                    ("#alerts", "update_cell"), ("#watching", "update_cell"),
                                                    ("#history", "update_cell"))]
                now[0] = 100.2
                app.refresh_snapshot()
                for update in updates:
                    update.assert_not_called()

    async def test_header_deadline_crossing_and_footer_changes(self):
        now = [142.1]
        app = performance_app(now)
        screen = app.observer.snapshot
        app.observer.snapshot = replace(screen, runtime=replace(screen.runtime, next_check_at=142.25))
        async with app.run_test(size=(200, 67)):
            app._idle_timer.stop()
            app._refresh_timer.stop()
            header = app.query_one("#header")
            with patch.object(header, "update", wraps=header.update) as update:
                now[0] = 142.3
                app.refresh_snapshot()
                update.assert_called_once()
                self.assertIn("overdue", header.content.renderables[0].columns[0]._cells[0].plain)
            footer = app.query_one("#footer")
            with patch.object(footer, "update", wraps=footer.update) as update:
                app.help_visible = True
                app.update_footer()
                self.assertIn("Screen: 10 fps", footer.content.plain)
                app.on_app_blur(None)
                app.update_footer()
                self.assertIn("unfocused", footer.content.plain)
                app.message = "Synthetic message"
                app.update_footer()
                self.assertEqual(update.call_count, 3)
                self.assertIn("Synthetic message", footer.content.plain)

    async def test_changed_ages_and_spinner_refresh(self):
        now = [100.1]
        app = performance_app(now)
        screen = app.observer.snapshot
        watching = (replace(screen.watching[0], state="waiting", since=screen.wall_now - 59.2),
                    replace(screen.watching[1], state="reviewing"))
        app.observer.snapshot = replace(screen, watching=watching,
                                        runtime=replace(screen.runtime, phase="reviewing", phase_since=90.75))
        async with app.run_test(size=(200, 67)):
            app._idle_timer.stop()
            app._refresh_timer.stop()
            table = app.query_one("#watching")
            column = table.ordered_columns[-1].key
            self.assertEqual(table.get_cell("1", column).plain.strip(), "59s")
            self.assertEqual(table.get_cell("0", column).plain.strip(), "9s")
            spinner = table.get_cell("0", "1")
            now[0] = 101.1
            app.refresh_snapshot()
            self.assertEqual(table.get_cell("1", column).plain.strip(), "1m")
            self.assertEqual(table.get_cell("0", column).plain.strip(), "10s")
            self.assertNotEqual(table.get_cell("0", "1"), spinner)
            app.ascii = True
            app.refresh_snapshot()
            with patch.object(app.query_one("#header"), "update") as update:
                now[0] = 101.4  # ASCII has no quarter-second spinner; elapsed still reads 10s.
                app.refresh_snapshot()
                update.assert_not_called()

    async def test_picture_repaint_avoids_layout_and_matches_rich(self):
        from rich.segment import Segment
        from textual.visual import RichVisual, RenderOptions
        now = [100.]
        app = performance_app(now)
        async with app.run_test(size=(200, 67)) as pilot:
            app._idle_timer.stop()
            app._refresh_timer.stop()
            panel = app.query_one("#idle-above")
            with patch.object(panel, "refresh", wraps=panel.refresh) as refresh:
                panel.update(panel.content, layout=False)
                refresh.assert_not_called()
            def cells(segments):
                return [(char, style) for value, style, _ in segments for char in value]
            for ascii, no_color in ((False, False), (True, False), (False, True)):
                app.ascii, app.no_color = ascii, no_color
                with patch.object(panel, "update", wraps=panel.update) as update:
                    now[0] += .1
                    app._idle_due = app.mono()
                    app.animate_idle()
                    update.assert_called_once()
                    self.assertFalse(update.call_args.kwargs["layout"])
                await pilot.pause()
                expected = RichVisual(panel, panel.content).render_strips(
                    panel.size.width, None, panel.visual_style, RenderOptions(get_style=lambda _: panel.visual_style, rules={}))
                for y, line in enumerate(expected):
                    actual = panel.render_line(y)
                    wanted = cells(Segment.adjust_line_length(
                        list(line), panel.size.width, panel.visual_style.rich_style))
                    self.assertEqual(len(cells(actual)), len(wanted))
                    for x, (got, want) in enumerate(zip(cells(actual), wanted)):
                        self.assertEqual(got, want, f"row {y}, column {x}")
            await pilot.resize_terminal(50, 67)
            app.refresh_snapshot()
            await pilot.pause()
            self.assertEqual(panel.size.height, 3)
            await pilot.resize_terminal(200, 67)
            app.refresh_snapshot()
            await pilot.pause()
            self.assertEqual(panel.size.height, 12)


if __name__ == "__main__":
    unittest.main()
