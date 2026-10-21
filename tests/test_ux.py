"""Screen layouts, keyboard controls and idle animation with synthetic, offline fixtures."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import os
import unittest

import importlib.util

import re
from pathlib import Path
import tempfile
import threading
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from lightkeeper.commands import CommandInbox
from lightkeeper.github import PRStatus
from lightkeeper.models import Check
from lightkeeper.reviewer import build_pr_prompt
from lightkeeper.settings import load_settings, settings_digest
from lightkeeper.ui.model import Alert, Runtime
from lightkeeper.ui.project import Observer, Projection
from tests.helpers import HEAD, make_pr
from tests.test_ui import NOW, fixture_screen as populated_screen

HAS_UI = all(importlib.util.find_spec(name) is not None for name in ('rich', 'textual'))

if HAS_UI:
    from textual import events
    from rich.style import Style
    from textual.widgets import Checkbox, DataTable, Static
    from textual.containers import VerticalScroll
    from lightkeeper.ui.detail import ConfirmOwnerApprove, ConfirmReopen, fetched_description
    from lightkeeper.ui.idle import (DEFAULT_LOOK, SEA, SEA_STYLES, STONE, TOWER, HEIGHT, HORIZON, Look,
                                           _row, _beam, _disc, _disc_position, _raster, _repository_stars, _event, _clouds,
                                           daytime_styles, event_at, event_start, lighthouse, lunar_phase)
    from lightkeeper.ui.preview import IdlePreview
    from lightkeeper.ui.rich_view import idle_panel
    from lightkeeper.ui.repos import Confirm, RepoForm
    from lightkeeper.ui.textual_app import TextualApp


def fixture_screen():
    # These layout/animation fixtures exercise the optional quiet slot explicitly.
    screen = populated_screen()
    return replace(screen, runtime=replace(screen.runtime, idle='quiet'))


SVG_TEXT = '{http://www.w3.org/2000/svg}text'


class UXDataTests(TestCase):
    def test_closure_preserves_post_and_assessment_after_timeline_eviction(self):
        projection = Projection()
        record = dict(kind='pr', key=['org/repo', 7, HEAD], at='2026-10-10T10:00:00Z')
        projection.feed(dict(record, event='assessment', action='comment', reason='Private assessment.'))
        projection.feed(dict(record, event='applied', action='comment', body='Posted body.',
                             url='https://example.test/review/7'))
        projection.feed(dict(record, event='closed', reason='pull request is merged'))
        for _ in range(25):
            projection.feed(dict(record, event='notify', text='Synthetic notice'))
        detail = projection.screen(Runtime(repos=('org/repo',)), NOW).details[tuple(record['key'])]
        self.assertEqual(detail.latest_private_reason, 'Private assessment.')
        self.assertEqual(detail.latest_post.state, 'applied')
        self.assertEqual(detail.latest_post.review_url, 'https://example.test/review/7')
        self.assertEqual(detail.latest_post.at, NOW)
        self.assertEqual(detail.latest_body, 'Posted body.')
        projection.feed(dict(record, event='assessment', action='nothing', reason=''))
        self.assertEqual(projection.screen(Runtime(), NOW).details[tuple(record['key'])].latest_private_reason, '')

    def test_latest_findings_survive_eviction_and_clear_on_next_assessment(self):
        projection = Projection()
        record = dict(kind='pr', key=['org/repo', 7, HEAD], at='2026-10-10T10:00:00Z')
        finding = dict(severity='important', origin='diff', text='Validate the input.', path='src/parser.py')
        projection.feed(dict(record, event='assessment', action='comment', findings=[finding]))
        first = projection.screen(Runtime(repos=('org/repo',)), NOW)
        for _ in range(25):
            projection.feed(dict(record, event='notify', text='notice'))
        detail = projection.screen(first.runtime, NOW).details[tuple(record['key'])]
        self.assertEqual(detail.latest_findings[0].text, 'Validate the input.')
        with self.assertRaises(FrozenInstanceError):
            detail.latest_findings[0].text = 'mutated'
        projection.feed(dict(record, event='assessment', action='nothing', findings=[], summary=''))
        self.assertEqual(projection.screen(first.runtime, NOW).details[tuple(record['key'])].latest_findings, ())
        self.assertEqual(len(first.details[tuple(record['key'])].latest_findings), 1)

    def test_full_metadata_survives_light_poll_but_not_head_change(self):
        projection = Projection()
        status = PRStatus.from_pr(make_pr(checks=(Check('unit', 'completed', 'failure', 'https://example.test/ci'),)))
        projection.pr_status(status)
        light = replace(status, failed_checks=None, failed_check_links=(), base_ref='', requested_reviewers=None)
        projection.pr_status(light)
        detail = projection.screen(Runtime(), NOW).details[('org/repo', 7, HEAD)]
        self.assertEqual(detail.base_ref, 'main')
        self.assertEqual(detail.requested_reviewers, status.requested_reviewers)
        self.assertEqual(detail.failed_check_links, (('unit', 'https://example.test/ci'),))
        projection.pr_status(replace(light, head='b' * 40))
        moved = projection.screen(Runtime(), NOW).details[('org/repo', 7, 'b' * 40)]
        self.assertEqual(moved.base_ref, '')
        self.assertIsNone(moved.requested_reviewers)
        self.assertEqual(moved.failed_check_links, ())
        projection.pr_status(replace(light, counts=(('success', 1),)))
        self.assertEqual(projection.screen(Runtime(), NOW).details[('org/repo', 7, HEAD)].failed_check_links, ())

    @unittest.skipUnless(HAS_UI, "Textual detail description")
    def test_description_uses_real_section_and_matching_variable_fence(self):
        body = '\n'.join(['First line', '```', '## Description', '```'] + [f'line {i}' for i in range(20)])
        prompt = build_pr_prompt(make_pr(title='title\n## Description\n````\nfalse description', body=body))
        self.assertEqual(fetched_description(prompt), '\n'.join(body.splitlines()[:12]))
        self.assertIsNone(fetched_description('missing section'))
        self.assertIsNone(fetched_description('## Description\nnot fenced'))


@unittest.skipUnless(HAS_UI, "optional UI dependencies are not installed")
class IdleSceneTests(TestCase):
    def test_look_defaults_and_tempo(self):
        self.assertEqual(DEFAULT_LOOK, Look(.8, True, True, True))
        with self.assertRaises(FrozenInstanceError):
            DEFAULT_LOOK.tempo = 1
        for checking in (False, True):
            self.assertEqual(lighthouse(14, 160, 3, checking, look=Look(tempo=.5)),
                             lighthouse(7, 160, 3, checking, look=Look(tempo=1)))

    def test_needs_you_changes_only_beam_colors_deterministically(self):
        for hour in (0, 12, 19, 23):
            for checking in (False, True):
                look = replace(DEFAULT_LOOK, needs_you=True)
                for t in (0, 1, 7):
                    args = (t, 200, 8, checking)
                    rest = (hour, .5, event_start(0) + .3)
                    normal, accents = _raster(*args, DEFAULT_LOOK, *rest)
                    amber, changed = _raster(*args, look, *rest)
                    self.assertEqual(normal, amber)
                    self.assertEqual((amber, changed), _raster(*args, look, *rest))
                    with patch('lightkeeper.ui.idle._beam', return_value=[]):
                        unlit, _ = _raster(*args, DEFAULT_LOOK, *rest)
                    changed_cells = [(x, y) for y in range(HEIGHT) for x in range(200)
                                     if accents[y].get(x) != changed[y].get(x)]
                    self.assertEqual(bool(changed_cells), hour != 12)
                    for x, y in changed_cells:
                        self.assertGreater(normal[y][x], unlit[y][x])
                    frame = lighthouse(*args, look=look, hour=hour, event_time=rest[-1])
                    self.assertEqual(frame, lighthouse(*args, look=look, hour=hour, event_time=rest[-1]))
                    for ascii, no_color in ((True, False), (False, True)):
                        self.assertEqual(lighthouse(*args, ascii, no_color=no_color, look=look, hour=hour),
                                         lighthouse(*args, ascii, no_color=no_color, look=DEFAULT_LOOK, hour=hour))

    def test_lunar_phase_known_new_and_full_moons(self):
        self.assertAlmostEqual(lunar_phase(datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)), 0)
        self.assertAlmostEqual(lunar_phase(datetime(2000, 1, 21, 4, 40, tzinfo=timezone.utc)), .5, delta=.02)

    def test_events_are_deterministic_sparse_and_have_requested_durations(self):
        kinds, showers = set(), 0
        for slot in range(100):
            start = event_start(slot)
            self.assertTrue(90 <= event_start(slot + 1) - start <= 150)
            event = event_at(start)
            self.assertEqual(event, event_at(start))
            kind, _, duration, seed = event
            kinds.add(kind)
            self.assertIsNone(event_at(start + duration))
            if kind == 'meteor':
                self.assertTrue(1.6 <= duration <= 2.0 or duration == 10)
                showers += duration == 10
            elif kind == 'ship':
                self.assertTrue(40 <= duration <= 60)
            elif kind == 'whale':
                self.assertEqual(duration, 10)
            else:
                self.fail(kind)
            kwargs = dict(look=Look(events=True), event_time=start + duration / 2)
            frame = lighthouse(1, 80, 3, False, **kwargs)
            self.assertEqual(frame, lighthouse(1, 80, 3, False, **kwargs))
            if kind != 'meteor' or duration != 10:
                self.assertNotEqual(frame, lighthouse(1, 80, 3, False))
        self.assertEqual(kinds, {'meteor', 'ship', 'whale'})
        self.assertGreater(showers, 0)

    def test_daytime_style_cache_is_bounded(self):
        daytime_styles.cache_clear()
        for hour in range(24):
            self.assertEqual(len(daytime_styles(hour)), 256)
        self.assertLessEqual(daytime_styles.cache_info().currsize, 24)
        self.assertIs(daytime_styles(12), daytime_styles(12))
        self.assertEqual(daytime_styles(0), SEA_STYLES)

    def test_fixed_geometry_small_frames_monochrome_and_extreme_times(self):
        for look in (Look(), DEFAULT_LOOK):
            for width in (0, 1, 3, 59, 80, 160, 200):
                for t in (-5, 1e9):
                    for ascii, no_color in ((False, False), (True, False), (False, True)):
                        picture = lighthouse(t, width, 3, True, ascii, look=look, hour=12,
                                             no_color=no_color, event_time=event_start(0) + .2)
                        self.assertEqual(len(picture), 10)
                        self.assertTrue(all(line.cell_len == width for line in picture))
                        if ascii or no_color:
                            self.assertTrue(all(not line.spans for line in picture))
                        if ascii:
                            self.assertTrue(all(line.plain.isascii() for line in picture))

    def test_repository_stars_are_steady_taller_and_counted(self):
        for repos in (0, 1, 3, 17):
            points = _repository_stars(200, repos)
            self.assertEqual(len(points), min(8, repos))
            self.assertEqual(len(set(points)), min(8, repos))
            for t in (0, 2, 7):
                raster, _ = _raster(t, 200, repos, False, DEFAULT_LOOK, 0, 0, None)
                for x, y in points:
                    self.assertEqual(raster[y + 1][x], 1.)
                    self.assertGreaterEqual(raster[y][x], .4)
                    self.assertLess(raster[y][x], 1.)
            day, _ = _raster(0, 200, repos, False, Look(daytime=True), 12, 0, None)
            for x, y in points:
                self.assertLess(day[y + 1][x], 1.)
            self.assertTrue(lighthouse(0, 200, repos, False)[0].plain.endswith(f' {repos} stars '))
        # Finite sky capacity: retain the exact numeric count when individual stars cannot fit.
        self.assertTrue(lighthouse(0, 80, 300, False)[0].plain.endswith(' 300 stars '))

    def test_checking_changes_only_beam_period_and_lantern(self):
        for checking, period in ((False, 8), (True, 4)):
            first = _beam(.7, 200, checking, False)
            repeat = _beam(.7 + period, 200, checking, False)
            for a, b in zip(first, repeat):
                self.assertAlmostEqual(a[0], b[0])
                self.assertEqual(a[1:], b[1:])
        self.assertNotEqual(_beam(1, 200, False, False), _beam(1, 200, True, False))
        # At t=0 both beams coincide: only the lantern cell differs.
        idle = lighthouse(0, 200, 3, False, hour=0)
        checking = lighthouse(0, 200, 3, True, hour=0)
        self.assertEqual(idle[:2] + idle[3:], checking[:2] + checking[3:])
        self.assertNotEqual(idle[2], checking[2])

    def test_disc_continuous_position_rim_and_horizon_occlusion(self):
        for sun, hour in ((True, 12), (False, 23)):
            cx, first = _disc(80, hour, .5, sun)
            nx, later = _disc(80, hour + .1, .5, sun)
            self.assertLessEqual(abs(nx - cx), 1)
            self.assertNotEqual(first, later)
            self.assertTrue(any(first[p] != later[p] for p in first.keys() & later.keys()))
            # At 200 columns the 12-hour crossing necessarily exceeds 1 column per .1h.
            self.assertAlmostEqual(_disc_position(200, hour + .001, sun)[0] -
                                   _disc_position(200, hour, sun)[0], .0145)
        # The whole night arc stays right of the tower at every width.
        for width in (60, 80, 200):
            for step in range(241):
                _, pixels = _disc(width, (19 + step * .05) % 24, .5, False)
                self.assertTrue(all(x >= 18 for x, y in pixels), (width, step))
        for sun, hour in ((True, 7), (True, 19), (False, 19), (False, 7)):
            cx, pixels = _disc(200, hour, .5, sun)
            self.assertEqual(_disc_position(200, hour, sun)[1], HORIZON)
            above = sum(v for (x, y), v in pixels.items() if y + .5 < HORIZON)
            self.assertAlmostEqual(above / sum(pixels.values()), .5, delta=.12)
        # Water actually removes celestial accents below the local surface.
        _, pixels = _disc(200, 19, .5, False)
        raster, accents = _raster(0, 200, 0, False, Look(moon=True), 19, .5, None)
        self.assertTrue(any(x not in accents[y] for x, y in pixels))
        self.assertTrue(any(x in accents[y] for x, y in pixels))

    def test_moon_rim_and_beam_crosses_the_moon(self):
        from lightkeeper.ui.idle import MOON, _level
        _, pixels = _disc(200, 23, .095, False)
        self.assertTrue(any(0 < value < .2 for value in pixels.values()))
        self.assertTrue(any(value == .2 for value in pixels.values()))
        crossed = False
        for t in [i / 4 for i in range(32)]:
            raster, accents = _raster(t, 200, 3, False, Look(moon=True), 23, .095, None)
            for (x, y), value in pixels.items():
                if raster[y][x] == value:
                    self.assertEqual(accents[y][x], MOON[_level(value)])
                else:  # The beam only adds light, and where it does the pixel is beam, not moon.
                    self.assertGreater(raster[y][x], value)
                    self.assertNotIn(x, accents[y])
                    crossed = True
        self.assertTrue(crossed)

    def test_clouds_pass_in_front_of_the_sun(self):
        from lightkeeper.ui.idle import SUN
        _, pixels = _disc(200, 12, .5, True)
        with patch('lightkeeper.ui.idle._clouds', return_value={xy: 1. for xy in pixels}):
            raster, accents = _raster(1, 200, 0, False, Look(daytime=True), 12, .5, None)
        for (x, y) in pixels:
            self.assertEqual(raster[y][x], .85)
            self.assertNotIn(accents[y][x], SUN)

    def test_day_has_blue_gradient_sun_clouds_and_no_beam(self):
        from lightkeeper.ui.idle import sky_palette
        self.assertEqual(_beam(1, 200, True, True), [])
        for hour in (7, 12, 18):
            raster, accents = _raster(1, 200, 0, True, Look(daytime=True), hour, .5, None)
            self.assertGreater(len(_clouds(1, 200)), 100)
            with patch('lightkeeper.ui.idle._clouds', return_value={}) as clouds:
                clear, clear_accents = _raster(1, 200, 0, True, Look(daytime=True), hour, .5, None)
                clouds.assert_called_once()
            self.assertNotEqual(accents, clear_accents)
            self.assertNotEqual(raster, clear)
        for hour in (0, 6, 19, 23):
            with patch('lightkeeper.ui.idle._clouds') as clouds:
                _raster(1, 200, 0, False, Look(daytime=True), hour, .5, None)
                clouds.assert_not_called()
        for color in sky_palette(12):
            r, g, b = (int(color[i:i+2], 16) for i in (1, 3, 5))
            self.assertLess(r, g)
            self.assertLess(g, b)
        self.assertNotEqual(sky_palette(12)[0], sky_palette(12)[-1])

    def test_star_slots_fixed_and_disc_occludes_them(self):
        slots = _repository_stars(200, 8)
        self.assertEqual(len(set(slots)), 8)
        for n in range(12):
            self.assertEqual(_repository_stars(200, n), slots[:n])
        x, y = slots[1]
        with patch('lightkeeper.ui.idle._disc', return_value=(x, {(x, y + 1): .2})), \
                patch('lightkeeper.ui.idle._beam', return_value=[]):
            raster, _ = _raster(0, 200, 8, False, Look(moon=True), 23, .5, None)
        self.assertEqual(raster[y + 1][x], .2)

    def test_events_cover_water_and_meteor_splash(self):
        def render(kind, age, duration, seed=0, beam=()):
            raster = [[.6] * 80 for _ in range(HEIGHT)]
            accents = [dict.fromkeys(range(80), '#888888') for _ in range(HEIGHT)]
            with patch('lightkeeper.ui.idle.event_at', return_value=(kind, age, duration, seed)):
                _event(raster, accents, 0, [13.] * 80, beam)
            return raster, accents
        night = [(0., 1., 0.)] * 80  # A beam list marks night; zero power lights nothing.
        for kind, duration in (('whale', 10), ('ship', 50)):
            raster, accents = render(kind, duration / 2, duration, beam=night)
            # Row 13 carries the lit top edge; the body below it stays dark.
            dark = [x for x in range(80) if raster[14][x] < .2]
            self.assertGreaterEqual(len(dark), 8)
            self.assertTrue(all(x not in accents[14] for x in dark))
            self.assertGreaterEqual(sum(v >= .7 for v in raster[13]), 6)
        # By day the hull is white; the navigation light flashes .4 s in every 2 s.
        day, _ = render('ship', 25, 50)
        self.assertGreaterEqual(sum(v > .8 for v in day[13]), 8)
        lit, _ = render('ship', 24.2, 50, beam=night)
        unlit, _ = render('ship', 24.6, 50, beam=night)
        self.assertEqual(max(max(row) for row in lit), 1.)
        self.assertLess(max(max(row) for row in unlit), 1.)
        # Half-column movement must not collapse alternating hull columns.
        half, _ = render('whale', 6, 10)
        self.assertEqual(sum(v < .2 for v in half[14]), 14)
        spout, _ = render('whale', 3, 10)
        self.assertTrue(all(spout[y][28] > .9 for y in (8, 9, 10)))
        tail, _ = render('whale', 7, 10)
        self.assertGreaterEqual(sum(v < .2 for v in tail[10]), 4)
        fall, fire = render('meteor', .3, 1.6)
        self.assertTrue(any(color.startswith('#') and color != '#888888'
                            for row in fire for color in row.values()))
        splash, _ = render('meteor', .9, 1.6)
        self.assertEqual(sum(v > .9 for row in splash for v in row), 3)
        self.assertTrue(any(v > .9 for v in splash[13]))
        # Day and night both reach the same event compositor after the sea.
        for day in (False, True):
            with patch('lightkeeper.ui.idle._event', wraps=_event) as draw:
                _raster(0, 80, 0, False, Look(daytime=day, events=True), 12, .5, event_start(0))
                draw.assert_called_once()

    @unittest.skipUnless(os.environ.get('LIGHTKEEPER_BENCH'), 'set LIGHTKEEPER_BENCH=1 to time frames')
    def test_warm_all_on_frame_budget(self):
        from timeit import repeat
        for hour, needs_you in ((7, False), (12, False), (19, False), (23, False), (19, True), (23, True)):
            def frame():
                return lighthouse(1, 200, 8, True, look=Look(daytime=True, moon=True, events=True, needs_you=needs_you),
                                                   hour=hour, event_time=event_start(0) + .3)
            frame()
            self.assertLess(min(repeat(frame, number=50, repeat=3)) / 50, .004)

    def test_glint_under_disc_is_bright_and_follows_surface(self):
        for day, hour in ((False, 22), (True, 12)):
            look = Look(moon=True, daytime=day)
            cx, _ = _disc(200, hour, .5, day)
            for t in (0, 1, 2):
                raster, _ = _raster(t, 200, 0, False, look, hour, .5, None)
                under = max(raster[y][x] for y in range(14, 20)
                            for x in range(round(cx) - 3, round(cx) + 4))
                self.assertGreater(under, .65)
            first, _ = _raster(0, 200, 0, False, look, hour, .5, None)
            later, _ = _raster(1, 200, 0, False, look, hour, .5, None)
            self.assertNotEqual(first[16][round(cx)], later[16][round(cx)])
        # A thin crescent casts a dimmer glint than the full moon; the sun is always full.
        cx, _ = _disc(200, 22, .095, False)
        def peak(phase):
            return max(_raster(0, 200, 0, False, Look(moon=True), 22, phase, None)[0][y][x]
                                         for y in range(14, 20) for x in range(round(cx) - 3, round(cx) + 4))
        self.assertLess(peak(.095), peak(.5))

    def test_tower_and_rock_have_shading_and_door(self):
        from rich.console import Console
        console = Console()
        self.assertEqual(len(TOWER), 4)
        for hour, day in ((0, False), (12, True), (19, False)):
            frame = lighthouse(0, 200, 0, False, look=Look(daytime=True), hour=hour)
            for row in (3, 4):
                left = frame[row].get_style_at_offset(console, 7).color
                right = frame[row].get_style_at_offset(console, 9).color
                self.assertNotEqual(left, right)
            self.assertEqual(frame[5].get_style_at_offset(console, 8).color.name, STONE[1])
            raster, accents = _raster(0, 200, 0, False, Look(daytime=True), hour, 0, None)
            self.assertIn(7, accents[14])
            self.assertGreaterEqual(raster[14][7], (7 if day else 2) / 15)

    def test_raster_pairs_palette_and_run_merging(self):
        self.assertEqual(len(set(SEA)), 16)
        row = _row([1., 1., 0., 0.], [0., 0., 0., 0.], SEA_STYLES)
        self.assertEqual(row.plain, '▀▀  ')
        self.assertEqual([(s.start, s.end) for s in row.spans], [(0, 2), (2, 4)])
        self.assertIs(row.spans[0].style, SEA_STYLES[240])
        for ascii in (False, True):
            mono = _row([1., 1., 0.], [0., 1., 0.], SEA_STYLES, ascii, no_color=True)
            self.assertEqual(mono.plain, ('-# ' if ascii else '░█ '))
            self.assertEqual(mono.spans, [])

    def test_compact_and_monochrome_picture(self):
        screen = replace(fixture_screen(), watching=())
        for ascii, no_color in ((False, True), (True, False), (True, True)):
            for width, free_rows, expected in ((59, 40, 3), (80, 13, 3), (80, 14, 12)):
                panel = idle_panel(screen, 100, width, ascii, free_rows=free_rows, no_color=no_color)
                lines = list(panel.renderables)
                self.assertEqual(len(lines), expected)
                self.assertIn('all quiet', '\n'.join(line.plain for line in lines))
                self.assertTrue(all(line.cell_len <= width for line in lines))
                for line in lines:
                    self.assertNotIn('▀', line.plain)
                    if ascii:
                        self.assertTrue(line.plain.isascii())
                    for span in line.spans:
                        style = span.style if isinstance(span.style, Style) else Style.parse(span.style)
                        self.assertIsNone(style.color)
                        self.assertIsNone(style.bgcolor)


@unittest.skipUnless(HAS_UI, "optional UI dependencies are not installed")
class UXPilots(IsolatedAsyncioTestCase):
    async def test_preview_gene_keys(self):
        app = IdlePreview()
        async with app.run_test(size=(160, 14)) as pilot:
            self.assertEqual(app.look, DEFAULT_LOOK)
            for key in ('[', ']', 'd', ',', '.', 'm', 'e', 'E', 'c', '+', '-'):
                await pilot.press(key)
                await pilot.pause()
                self.assertTrue(app.is_running)
            self.assertEqual(app.look, Look(.8, False, False, True))
            await pilot.press('n')
            self.assertTrue(app.look.needs_you)
            self.assertIn('needs_you', app.query_one('#footer').content.plain)
            await pilot.press('n')
            self.assertFalse(app.look.needs_you)
            self.assertEqual(app.hour_offset, 0)
            self.assertIsNotNone(app.event_slot)
            self.assertFalse(any(binding.key == 't' for binding in app.BINDINGS))
            self.assertTrue(app.checking)
            self.assertEqual(app.repos, 3)
            self.assertEqual(app.query_one('#idle').size.height, 12)
            self.assertEqual(app.query_one('#footer').size.height, 2)
            self.assertIn('Look(', app.query_one('#footer').content.plain)
            for _ in range(20):
                app.action_tempo(-.1)
            self.assertEqual(app.look.tempo, .2)
            for _ in range(20):
                app.action_tempo(.1)
            self.assertEqual(app.look.tempo, 1.5)
            await pilot.press('q')

    async def test_idle_receives_needs_you_and_refreshes_compact_key(self):
        snapshot = fixture_screen()
        snapshot = replace(snapshot, runtime=replace(snapshot.runtime, idle='always'))
        for width in (50, 200):
            app = self.app(snapshot)
            with patch('lightkeeper.ui.textual_app.idle_panel', wraps=idle_panel) as render:
                async with app.run_test(size=(width, 52)) as pilot:
                    await pilot.pause()
                    self.assertTrue(render.call_args.kwargs['look'].needs_you)
                    cleared = replace(snapshot, watching=tuple(replace(row, needs_you=None)
                                                               for row in snapshot.watching))
                    app.observer.snapshot = cleared
                    render.reset_mock()
                    app.refresh_snapshot()
                    self.assertFalse(render.call_args.kwargs['look'].needs_you)
                    if width == 50:
                        self.assertFalse(app._idle_compact_key[-1])

    async def test_quiet_watching_reserves_twelve_rows_empty_and_populated(self):
        snapshot = fixture_screen()
        app = self.app(replace(snapshot, watching=()))
        async with app.run_test(size=(160, 40)) as pilot:
            for rows in ((), snapshot.watching, ()):
                app.observer.snapshot = replace(snapshot, watching=rows)
                app.refresh_snapshot()
                await pilot.pause()
                self.assertEqual(len(rows), 7 if rows else 0)
                self.assertEqual(app.query_one('#watching').row_count, len(rows))
                self.assertEqual(app.query_one('#watching-block').size.height, 12)
                self.assertEqual(app.query_one('#idle').display, not rows)
                self.assertFalse(app.query_one('#idle-above').display)
                self.assertEqual(app._idle_timer._active.is_set(), not rows)
                self.assertEqual(app.query_one('#history').region.y,
                                 app.query_one('#watching-heading').region.y + 14)
                if rows:
                    self.assertEqual(app.query_one('#watching').size.height, 12)
                else:
                    self.assertEqual(app.query_one('#idle').size.height, 12)

    async def test_always_idle_keeps_watching_and_takes_height_from_history(self):
        snapshot = fixture_screen()
        app = self.app(snapshot)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            history_height = app.query_one('#history').size.height
            for mode in ('quiet', 'always', 'quiet'):
                app.observer.snapshot = replace(snapshot, runtime=replace(snapshot.runtime, idle=mode))
                app.refresh_snapshot()
                await pilot.pause()
                above = app.query_one('#idle-above')
                self.assertFalse(app.query_one('#idle').display)
                self.assertEqual(above.display, mode == 'always')
                self.assertEqual(app._idle_visible, mode == 'always')
                self.assertEqual(app._idle_timer._active.is_set(), mode == 'always')
                self.assertEqual(app.query_one('#watching').size.height, 12)
                self.assertEqual(app.query_one('#watching-block').size.height, 12)
                self.assertEqual(app.query_one('#history').size.height,
                                 history_height - (12 if mode == 'always' else 0))
                history = app.query_one('#history')
                self.assertEqual(history.region.bottom, app.query_one('#footer').region.y)
                if mode == 'always':
                    self.assertEqual(above.size.height, 12)
                    self.assertEqual(above.region.bottom, app.query_one('#watching-heading').region.y)
                    self.assertEqual(above.region.y, app.query_one('#alerts').region.bottom)
                    self.assertIn('watching', above.content.renderables[-2].plain)

    async def test_always_idle_hides_when_history_would_lose_its_last_row(self):
        snapshot = fixture_screen()
        app = self.app(replace(snapshot, runtime=replace(snapshot.runtime, idle='always')))
        async with app.run_test(size=(160, 40)) as pilot:
            for height, shown, panel_height in ((40, True, 12), (30, True, 3), (25, True, 3),
                                                (24, False, 0), (20, False, 0)):
                await pilot.resize_terminal(160, height)
                app.refresh_snapshot()
                await pilot.pause()
                panel = app.query_one('#idle-above')
                self.assertEqual(panel.display, shown)
                self.assertFalse(app.query_one('#idle').display)
                self.assertEqual(app._idle_visible, shown)
                self.assertEqual(app._idle_timer._active.is_set(), shown)
                if shown:
                    self.assertEqual(panel.size.height, panel_height)
                self.assertEqual(app.query_one('#watching').size.height, 12)
                history, footer = app.query_one('#history'), app.query_one('#footer')
                self.assertGreaterEqual(history.size.height, 1)
                if height == 20:
                    # Alerts plus the fixed minimum need 22 rows; Overview scrolls.
                    history.focus()
                    history.scroll_visible(animate=False)
                    await pilot.pause()
                self.assertFalse(history.show_horizontal_scrollbar)
                self.assertGreaterEqual(footer.region.y, history.region.y + 1)

    async def test_empty_slots_switch_and_pause_when_too_short(self):
        snapshot = replace(fixture_screen(), watching=(), alerts=())
        app = self.app(snapshot)
        async with app.run_test(size=(160, 40)) as pilot:
            for mode in ('quiet', 'always', 'quiet'):
                app.observer.snapshot = replace(snapshot, runtime=replace(snapshot.runtime, idle=mode))
                app.refresh_snapshot()
                await pilot.pause()
                self.assertEqual(app.query_one('#idle').display, mode == 'quiet')
                self.assertEqual(app.query_one('#idle-above').display, mode == 'always')
                self.assertEqual(app.query_one('#watching-block').size.height, 12)
                self.assertEqual(app.query_one('#watching').row_count, 0)
                self.assertTrue(app._idle_timer._active.is_set())
            for height, shown in ((20, True), (18, True), (16, True), (40, True)):
                await pilot.resize_terminal(160, height)
                app.refresh_snapshot()
                await pilot.pause()
                self.assertEqual(app.query_one('#idle').display, shown)
                self.assertFalse(app.query_one('#idle-above').display)
                self.assertEqual(app._idle_timer._active.is_set(), shown)
                self.assertEqual(app.query_one('#watching-block').size.height, 12)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.shots = Path(os.environ.get('UX_SCREENSHOT_DIR', self.root / 'screenshots'))
        self.shots.mkdir(parents=True, exist_ok=True)
        (self.root / 'settings.toml').write_text(
            '[identity]\nlogin="reviewer"\n[repos."example/widget"]\npost="dry-run"\n')
        (self.root / 'rules.md').write_text('Review carefully.', encoding='utf-8')

    def app(self, snapshot=None, clock=None):
        snapshot = snapshot or fixture_screen()
        snapshot = replace(snapshot, runtime=replace(snapshot.runtime, config_dir=str(self.root)))
        observer = Observer(Projection(), snapshot.runtime)
        observer.snapshot = snapshot
        worker = Mock(runner=Mock(cancelled=False, commands=CommandInbox(threading.Event())),
                      poll_join=Mock(return_value=False))
        # Layout pilots must not interpret screenshot/export time as terminal backpressure.
        app = TextualApp(observer, worker, clock=clock or (lambda: 100), mono=lambda: 100)
        app.theme = 'textual-dark'
        return app

    def screenshot(self, app, name):
        path = app.save_screenshot(name + '.svg', path=str(self.shots))
        nodes = list(ET.parse(path).iter(SVG_TEXT))
        value = ' '.join(''.join(node.itertext()) for node in nodes)
        value = ' '.join(value.split())
        lines = {}
        for node in nodes:
            # Reconstruct terminal rows from SVG coordinates for human inspection, too.
            if node.get('clip-path'):
                line = lines.setdefault(float(node.get('y')), '')
                content = ''.join(node.itertext()).replace('\xa0', ' ').replace('\n', '')
                if content:
                    cell_width = float(node.get('textLength')) / len(content)
                    column = round(float(node.get('x')) / cell_width)
                    lines[float(node.get('y'))] = line.ljust(column) + content
        (self.shots / (name + '.txt')).write_text('\n'.join(line.rstrip() for _, line in sorted(lines.items())) + '\n')
        return value, nodes

    def assert_navigation(self, value, app, current, selector):
        for expected in ('1 Overview', '2 Repositories', '3 Agent', 'L logs', 'q quit', '? help'):
            self.assertIn(expected, value)
        content = app.screen.query_one(selector, Static).content
        label = ('1 Overview', '2 Repositories', '3 Agent')[current - 1]
        start = content.plain.index(label)
        self.assertTrue(content.get_style_at_offset(app.console, start).reverse)
        self.assertTrue(content.get_style_at_offset(app.console, start).bold)

    async def test_screen_settings_reach_dashboard_look(self):
        snapshot = replace(fixture_screen(), watching=(), alerts=())
        app = self.app(snapshot)
        async with app.run_test(size=(100, 30)):
            for clock, moon, events in ((True, True, True), (False, True, False), (True, False, True)):
                (self.root / 'settings.toml').write_text(
                    '[identity]\nlogin="reviewer"\n[screen]\ntempo=1.2\n'
                    f'clock={str(clock).lower()}\nmoon={str(moon).lower()}\nevents={str(events).lower()}\n')
                screen = load_settings(self.root).screen
                app.observer('policy', tempo=screen.tempo, clock=screen.clock,
                             moon=screen.moon, events=screen.events)
                with patch('lightkeeper.ui.textual_app.idle_panel', wraps=idle_panel) as render:
                    app.refresh_idle(app.observer.snapshot, 10, 100, force=True)
                    self.assertEqual(render.call_args.kwargs['look'], Look(1.2, clock, moon, events))

    async def test_idle_timer_render_error_keeps_session_and_logs_once(self):
        app = self.app(replace(fixture_screen(), watching=(), alerts=()))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._idle_timer.pause()
            app._refresh_timer.pause()
            panel = app.query_one('#idle', Static)
            previous = panel.content
            with patch('lightkeeper.ui.rich_view.lighthouse', side_effect=ValueError('broken scene')):
                with self.assertLogs('lightkeeper', level='ERROR') as logs:
                    for tick in range(3):
                        await app._idle_timer._tick(next_timer=100 + tick / 10, count=tick)
                        app.refresh_snapshot()
                self.assertEqual(len(logs.records), 1)
                self.assertTrue(app.is_running)
                self.assertFalse(app._exit)
                self.assertIs(panel.content, previous)
                self.assertIn('render error (see run.log)', app.query_one('#footer').content.plain)
            app.refresh_snapshot()
            self.assertFalse(app._render_failed)
            self.assertNotIn('render error', app.query_one('#footer').content.plain)

    async def test_first_idle_render_reserves_expanded_alert_lines(self):
        snapshot = replace(fixture_screen(), watching=(), alerts=())
        app = self.app(snapshot)
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app._idle_timer.pause()
            app._refresh_timer.pause()
            app.expanded_alerts.update(('unknown_write', 'test'))
            app.observer.snapshot = replace(snapshot, alerts=(
                Alert('unknown_write', 'warning', 'Unknown write', 90),
                Alert('test', 'warning', 'Synthetic alert', 90)))
            panel = app.query_one('#idle', Static)
            with patch('lightkeeper.ui.textual_app.idle_panel', wraps=idle_panel) as render:
                app.refresh_snapshot()
                self.assertEqual(render.call_args.kwargs['free_rows'], 14)
                self.assertEqual(len(panel.content.renderables), 12)
                await pilot.pause()
                first_height = panel.size.height
                app.refresh_snapshot()
                await pilot.pause()
                self.assertEqual(first_height, 12)
                self.assertEqual(panel.size.height, first_height)
                self.assertEqual(app._idle_layout, (100, 14, False))

    async def test_compact_idle_updates_once_per_second(self):
        for size in ((59, 30), (59, 20)):  # Only width makes quiet mode compact now.
            now = [100.]
            app = self.app(replace(fixture_screen(), watching=(), alerts=()), clock=lambda: now[0])
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                app._idle_timer.pause()
                app._refresh_timer.pause()
                panel = app.query_one('#idle', Static)
                with patch.object(panel, 'update', wraps=panel.update) as update:
                    for tick, second in enumerate((101., 101.1, 101.5, 101.9)):
                        now[0] = second
                        await app._idle_timer._tick(next_timer=second, count=tick)
                        app.refresh_snapshot()
                    update.assert_called_once()
                    now[0] = 102.
                    app.animate_idle()
                    self.assertEqual(update.call_count, 2)

    async def test_idle_timer_rate_respects_mount_visibility_and_shutdown(self):
        snapshot = replace(fixture_screen(), watching=(), alerts=())
        app = self.app(snapshot)
        app.on_app_blur(events.AppBlur())
        self.assertEqual(app._idle_interval, 1)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app._idle_timer._interval, 1)
            for hidden_by_screen in (False, True):
                app.on_app_focus(events.AppFocus())
                if hidden_by_screen:
                    app.observer.snapshot = snapshot
                    app.refresh_snapshot()
                    await app.push_screen(Confirm('Synthetic screen'))
                else:
                    app.observer.snapshot = replace(snapshot, watching=fixture_screen().watching[:1])
                    app.refresh_snapshot()
                old_timer = app._idle_timer
                app.post_message(events.AppBlur())
                await pilot.pause()
                self.assertIsNone(old_timer._task)
                self.assertEqual(app._idle_timer._interval, 1)
                self.assertFalse(app._idle_timer._active.is_set())
            old_timer = app._idle_timer
        with patch.object(app, 'set_interval') as start:
            app.on_app_focus(events.AppFocus())
            start.assert_not_called()
        self.assertIs(app._idle_timer, old_timer)
        self.assertIsNone(old_timer._task)

    async def test_form_labels_indicators_hotkeys_and_help_in_dark_svg(self):
        app = self.app()
        async with app.run_test(size=(110, 40)) as pilot:
            await app.push_screen(RepoForm(load_settings(self.root), settings_digest(self.root), 'example/widget'))
            await pilot.pause()
            value, nodes = self.screenshot(app, 'form')
            for label in ('Repository', 'Enabled · watch and review this repository', 'Post', 'Remarks',
                          'Allowed actions', 'Approve (A)', 'Request changes (R)', 'Comment (C)',
                          'Deployments · review deployment gates', 'Save', 'Cancel', 'Remove',
                          'live posts reviews; dry-run only records what would be posted',
                          'blockers only · blockers and important · everything'):
                self.assertIn(label, value)
            self.assertEqual(sum(node.text == '[X]' for node in nodes), 4)
            self.assert_navigation(value, app, 2, '#form-navigation')
            await pilot.press('A', 'R', 'C')
            value, nodes = self.screenshot(app, 'form-actions-off')
            self.assertEqual(sum(node.text == '[X]' for node in nodes), 1)
            for action in ('approve', 'request_changes', 'comment'):
                self.assertFalse(app.screen.query_one('#' + action, Checkbox).value)
            self.assertIn('[ ] Request changes (R)', value)
            await pilot.resize_terminal(80, 24)
            app.screen.query_one('#repo-form').scroll_end(animate=False)
            await pilot.pause()
            value, _ = self.screenshot(app, 'form-small-scrolled')
            self.assertIn('Deployments · review deployment gates', value)
            self.assertIn('Save', value)

    async def test_empty_overview_live_countdown_and_remaining_history_space(self):
        now = [100]
        snapshot = fixture_screen()
        snapshot = replace(snapshot, watching=(), history=(), alerts=(),
                           runtime=replace(snapshot.runtime, next_check_at=119))
        app = self.app(snapshot, clock=lambda: now[0])
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            value, _ = self.screenshot(app, 'overview-empty')
            self.assertIn('all quiet · 1 repository watched · next check in 0:19 · r to check now', value)
            self.assertIn('no reviews yet today', value)
            self.assertFalse(app.query_one('#watching').display)
            self.assertEqual(app.query_one('#idle').size.height, 12)
            with patch.object(app.query_one('#idle'), 'update') as update:
                now[0] += 0.25
                app.refresh_snapshot()
                update.assert_not_called()
            self.assertGreater(app.query_one('#history').size.height, 10)
            self.assertFalse(app.rows)  # Empty messages are not selectable PRs.
            self.assert_navigation(value, app, 1, '#footer')
            now[0] = 101
            app.animate_idle()
            app.refresh_snapshot()
            await pilot.pause()
            value, _ = self.screenshot(app, 'overview-countdown')
            self.assertIn('next check in 0:18', value)
            history = tuple(replace(fixture_screen().history[0], item=replace(
                fixture_screen().history[0].item, number=n), at=NOW - n) for n in range(1, 35))
            app.observer.snapshot = replace(snapshot, history=history)
            app.refresh_snapshot()
            await pilot.pause()
            value, _ = self.screenshot(app, 'overview-history')
            self.assertIn('widget#20', value)
            self.assertNotIn('widget#34', value)
            await pilot.press(*(['down'] * 33))
            await pilot.pause()
            value, _ = self.screenshot(app, 'overview-history-scrolled')
            self.assertIn('widget#34', value)
            await pilot.press(*(['up'] * 33))
            self.assertNotIn('no reviews yet today', value)
            await pilot.press('enter')
            self.assertEqual(app.screen.row.item.number, 1)

    async def test_idle_frames_tips_ascii_resize_and_immediate_removal(self):
        now = [96]
        snapshot = fixture_screen()
        snapshot = replace(snapshot, watching=(), alerts=(), runtime=replace(
            snapshot.runtime, repos=tuple(f'example/repo{n}' for n in range(8)), next_check_at=137))
        with patch.dict(os.environ, {'NO_COLOR': '1', 'LIGHTKEEPER_ASCII': '1'}):
            app = self.app(snapshot, clock=lambda: now[0])
            async with app.run_test(size=(110, 40)) as pilot:
                poses = []
                for second in range(96, 100):
                    now[0] = second
                    app.animate_idle()
                    app.refresh_snapshot()
                    await pilot.pause()
                    value, _ = self.screenshot(app, f'idle-ascii-frame-{second - 96}')
                    self.assertIn('8 repositories watched', value)
                    panel = app.query_one('#idle', Static).content
                    plain = '\n'.join(part.plain for part in panel.renderables)
                    self.assertTrue(plain.isascii())
                    self.assertTrue(all(span.style in ('', 'dim', 'bold')
                                        for part in panel.renderables for span in part.spans))
                    poses.append(plain)
                self.assertEqual(len(set(poses)), 4)
                self.assertTrue(app.no_color)
                now[0] = 108
                app.animate_idle()
                app.refresh_snapshot()
                await pilot.pause()
                value, _ = self.screenshot(app, 'idle-next-tip')
                self.assertIn('Tab moves between sections; o opens GitHub', value)
                for width, height in ((60, 12), (59, 3), (42, 3)):
                    await pilot.resize_terminal(width, 24)
                    app.refresh_snapshot()
                    await pilot.pause()
                    value, _ = self.screenshot(app, f'idle-width-{width}')
                    self.assertEqual(app.query_one('#idle').size.height, height)
                    if width < 60:
                        self.assertIn('r to check now', value)
                app.observer.snapshot = replace(snapshot, watching=fixture_screen().watching[:1])
                app.refresh_snapshot()
                self.assertFalse(app.query_one('#idle').display)
                self.assertFalse(app._idle_timer._active.is_set())
                with patch('lightkeeper.ui.textual_app.idle_panel') as render:
                    app.animate_idle()
                    app.refresh_snapshot()
                    render.assert_not_called()
                await pilot.pause()
                value, _ = self.screenshot(app, 'idle-row-arrives')
                self.assertNotIn('all quiet', value)
                self.assertEqual(app.query_one('#watching').ordered_rows[0].height, 1)
                app.observer.snapshot = snapshot
                app.refresh_snapshot()
                await pilot.pause()
                value, _ = self.screenshot(app, 'idle-row-leaves')
                self.assertIn('all quiet', value)
                self.assertTrue(app._idle_timer._active.is_set())
            self.assertIsNone(app._idle_timer._task)
            self.assertIsNone(app._refresh_timer._task)
        with patch('lightkeeper.ui.textual_app.sys.stdout', encoding='ascii'), patch.dict(
                os.environ, {'LIGHTKEEPER_ASCII': '0'}):
            non_utf8 = self.app(snapshot)
        self.assertTrue(non_utf8.ascii)

    async def test_lighthouse_gallery_focus_visibility_and_height(self):
        snapshot = replace(fixture_screen(), watching=(), history=(), alerts=())
        app = self.app(snapshot, clock=lambda: 3.)
        async with app.run_test(size=(80, 24)) as pilot:
            for width in (80, 120, 200):
                await pilot.resize_terminal(width, 24)
                await pilot.pause()
                value, _ = self.screenshot(app, f'gallery-lighthouse-{width}')
                self.assertIn('all quiet', value)
                self.assertIn('1 ', app.query_one('#idle').content.renderables[0].plain)
                self.assertEqual(app.query_one('#idle').size.height, 12)
                extracted = (self.shots / f'gallery-lighthouse-{width}.txt').read_text()
                for line in app.query_one('#idle').content.renderables[:10]:
                    self.assertIn(line.plain.rstrip(), extracted)
            app.observer.snapshot = replace(snapshot, runtime=replace(snapshot.runtime, phase='checking'))
            app.refresh_snapshot()
            await pilot.pause()
            self.screenshot(app, 'gallery-lighthouse-checking')
            app.ascii = True
            app.no_color = True
            app.animate_idle()
            await pilot.pause()
            self.screenshot(app, 'gallery-lighthouse-ascii-monochrome')
            old_timer = app._idle_timer
            app.post_message(events.AppBlur())
            await pilot.pause()
            self.assertEqual(app._idle_interval, 1)
            self.assertIsNone(old_timer._task)
            blurred_timer = app._idle_timer
            app.post_message(events.AppFocus())
            await pilot.pause()
            self.assertEqual(app._idle_interval, .1)
            self.assertIsNone(blurred_timer._task)
            await app.push_screen(Confirm('Synthetic screen'))
            self.assertFalse(app._idle_timer._active.is_set())
            with patch('lightkeeper.ui.textual_app.idle_panel') as render:
                app.animate_idle()
                render.assert_not_called()
            await pilot.press('escape')
            self.assertTrue(app._idle_timer._active.is_set())
            await pilot.resize_terminal(80, 20)
            app.refresh_snapshot()
            await pilot.pause()
            value, _ = self.screenshot(app, 'gallery-lighthouse-short')
            self.assertIn('1 repository watched', value)
            self.assertEqual(app.query_one('#idle').size.height, 12)
            self.assertIn('no reviews yet today', value)
            self.assertIn('q quit', value)
        self.assertIsNone(app._idle_timer._task)
        self.assertIsNone(app._refresh_timer._task)

    async def test_merged_detail_without_assessment_and_collapsed_timeline(self):
        snapshot = fixture_screen()
        row = next(row for row in snapshot.history if row.state == 'merged')
        key = (row.item.repo, row.item.number, row.item.head)
        event = snapshot.events[key][0]
        snapshot = replace(snapshot, watching=(), alerts=(), history=(row,),
                           events={key: (event, replace(event, at=event.at + 60))})
        app = self.app(snapshot)
        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.press('enter')
            await pilot.pause()
            value, nodes = self.screenshot(app, 'detail-merged')
            for expected in ('example/widget#6', 'Status', 'state ↷ merged', 'no checks',
                             'no assessment on this head', 'nothing posted', 'Timeline', 'closed ×2'):
                self.assertIn(expected, value)
            for absent in ('CI unavailable', 'Assessment', 'private · journal only', 'reviewers', 'base unknown',
                           'draft', 'pushed', ' · · '):
                self.assertNotIn(absent, value)
            self.assertEqual(sum('closed ×2' in ' '.join(''.join(node.itertext()).split()) for node in nodes), 1)
            self.assertIn(datetime.fromtimestamp(event.at + 60).strftime('%Y-%m-%d %H:%M:%S'), value)

    async def test_populated_overview_alerts_size_and_header_repo_count(self):
        snapshot = fixture_screen()
        runtime = replace(snapshot.runtime, repos=tuple(f'example/repo{n}' for n in range(6)) + ('example/widget',),
                          configured_repo_count=8, dry_repos=('example/widget',), live_lock_held=True)
        snapshot = replace(snapshot, runtime=runtime, alerts=(Alert('test', 'warning', 'Synthetic alert', 90),))
        app = self.app(snapshot)
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            value, _ = self.screenshot(app, 'overview-populated')
            self.assertIn('8 repos (6 live)', value)
            self.assertIn('WATCHING 7', value)
            self.assertEqual(app.query_one('#watching').size.height, 12)
            self.assertFalse(app.query_one('#idle').display)
            self.assertEqual(app.query_one('#alerts').size.height, 1)
            self.assertGreater(app.query_one('#history').size.height, 10)
            await pilot.press('enter')
            await pilot.pause()
            value, _ = self.screenshot(app, 'overview-alert-expanded')
            self.assertIn('cause: Synthetic alert', value)
            table = app.query_one('#alerts', DataTable)
            self.assertEqual(table.size.height, sum(row.height for row in table.ordered_rows))

    async def test_navigation_on_repositories_agent_and_confirmations(self):
        app = self.app()
        async with app.run_test(size=(110, 40)) as pilot:
            for key, selector in [('2', '#repos-footer'), ('3', '#agent-footer')]:
                await pilot.press(key)
                await app.workers.wait_for_complete()
                await pilot.pause()
                value, _ = self.screenshot(app, 'screen-' + key)
                self.assert_navigation(value, app, int(key), selector)
            item = fixture_screen().watching[0].item
            for name, modal, current in [
                ('confirm-settings', Confirm('Switch to live posting?'), 2),
                ('confirm-reopen', ConfirmReopen(item, Mock()), 1),
                ('confirm-approve', ConfirmOwnerApprove(item, app.observer.snapshot.runtime, 'comment',
                                                        'Synthetic summary', '', Mock()), 1),
            ]:
                await app.push_screen(modal)
                await pilot.pause()
                value, _ = self.screenshot(app, name)
                self.assert_navigation(value, app, current, '.modal-navigation')
                await pilot.press('escape')

    async def test_fixture_detail_findings_description_and_push_age_svg(self):
        session = self.root / 'session'
        session.mkdir()
        pr = make_pr(base_ref='release', requested_users=('reviewer',), requested_teams=('org/maintainers',),
                     checks=tuple(Check(f'check-{n}', 'completed', 'success') for n in range(7)) + (
                         Check('pytest', 'completed', 'failure', 'https://example.test/check/1'),),
                     body='\n'.join(f'Description line {n}' for n in range(1, 16)))
        (session / 'prompt.md').write_text(build_pr_prompt(pr))
        projection = Projection()
        record = dict(kind='pr', key=['org/repo', 7, HEAD], at='2026-10-10T10:00:00Z', title=pr.title, author=pr.author)
        projection.feed(dict(record, event='assessment', action='comment', summary='Check the input boundary.',
                             why='Not approving yet: [bold]input[/] needs validation.',
                             watch='Check the retry path in src/parser.py.',
                             reason='A private explanation.', data={'session': str(session)}, findings=[
                                 dict(severity='important', origin='diff', text='Validate [bold]input[/].',
                                      path='src/parser.py', url='https://example.test/finding')]))
        projection.feed(dict(record, event='applied', action='comment', summary='Check the input boundary.',
                             body='Rendered [bold]review[/] body.\nSecond body line.', url='https://example.test/review/7'))
        stamp = datetime.fromtimestamp(NOW - 180, timezone.utc).isoformat()
        projection.pr_status(replace(PRStatus.from_pr(pr), head_pushed_at=stamp))
        snapshot = replace(projection.screen(Runtime(repos=('org/repo',), state_dir=str(self.root)), NOW), mono_at=100)
        app = self.app(snapshot)
        async with app.run_test(size=(120, 55)) as pilot:
            await pilot.press('enter')
            await app.workers.wait_for_complete()
            await pilot.pause()
            value, _ = self.screenshot(app, 'detail-fixture')
            for expected in ('base release', 'reviewers reviewer, org/maintainers', 'pushed 3m ago',
                             'Status', 'next step final', '7 passed · 1 failed: pytest (https://example.test/check/1)',
                             'Assessment', 'Not approving yet: [bold]input[/] needs validation.',
                             'Check the retry path in src/parser.py.', 'important · diff · Validate [bold]input[/]. · src/parser.py · https://example.test/finding',
                             'public summary Check the input boundary.', 'private · journal only', 'A private explanation.',
                             'Posted', 'https://example.test/review/7', 'Rendered [bold]review[/] body.',
                             'Second body line.', 'Description', 'Description line 12', 'Timeline', 'time event detail'):
                self.assertIn(expected, value)
            headers = [node for node in ET.parse(self.shots / 'detail-fixture.svg').iter(SVG_TEXT)
                       if node.text in ('Status', 'Assessment', 'Posted', 'Description', 'Timeline')]
            self.assertEqual(len(headers), 6)  # Posted heading plus outcome label.
            svg = (self.shots / 'detail-fixture.svg').read_text()
            styles = dict(re.findall(r'\.([\w-]+) \{([^}]+)\}', svg))
            why_node = next(node for node in ET.parse(self.shots / 'detail-fixture.svg').iter(SVG_TEXT)
                            if 'Not\xa0approving\xa0yet:' in (node.text or ''))
            self.assertIn('font-weight: bold', styles[why_node.get('class')])
            self.assertLess(value.index('Not approving yet:'), value.index('Check the retry path'))
            self.assertLess(value.index('Check the retry path'), value.index('public summary'))
            for node in headers:
                if node.text != 'Posted' or 'underline' in styles[node.get('class')]:
                    self.assertIn('font-weight: bold', styles[node.get('class')])
                    self.assertIn('text-decoration: underline', styles[node.get('class')])
            labels = [node for node in ET.parse(self.shots / 'detail-fixture.svg').iter(SVG_TEXT)
                      if (node.text or '').strip() in ('state', 'why', 'next\xa0step', 'CI', 'reviewers')]
            right_edges = {round(float(node.get('x')) + float(node.get('textLength')), 1) for node in labels}
            self.assertEqual(len(labels), 5)
            self.assertEqual(len(right_edges), 1)
            self.assertNotIn('Description line 13', value)
            self.assert_navigation(value, app, 1, '#detail-footer')
            # Unknown push time must not be replaced with PR updated_at or commit time.
            detail = snapshot.details[('org/repo', 7, HEAD)]
            app.observer.snapshot = replace(snapshot, details={('org/repo', 7, HEAD): replace(detail, head_pushed_at='')})
            app.refresh_snapshot()
            await pilot.pause()
            value, _ = self.screenshot(app, 'detail-push-unknown')
            self.assertNotIn('pushed', value)
            self.assertNotIn('draft', value)
            await pilot.resize_terminal(58, 24)
            await pilot.pause()
            value, _ = self.screenshot(app, 'detail-narrow-top')
            self.assertIn('Status', value)
            app.screen.query_one(VerticalScroll).scroll_end(animate=False)
            await pilot.pause()
            value, _ = self.screenshot(app, 'detail-narrow-scrolled')
            self.assertIn('Timeline', value)
            self.assertIn('Description line 12', value)

    async def test_description_read_is_bounded_in_worker_and_path_confined(self):
        from lightkeeper.ui import detail as module
        import io
        projection = Projection()
        session = self.root / 'session'
        session.mkdir()
        projection.feed(dict(kind='pr', key=['org/repo', 7, HEAD], event='nothing', session=str(session),
                             at='2026-10-10T10:00:00Z'))
        snapshot = projection.screen(Runtime(repos=('org/repo',), state_dir=str(self.root)), NOW)
        app = self.app(snapshot)
        threads, streams = [], []
        original = module.session_path
        def resolve(*args):
            threads.append(threading.get_ident())
            return original(*args)
        def read(*args, **kwargs):
            threads.append(threading.get_ident())
            stream = io.BytesIO(b'## Description\n````\n' + b'x' * (64 * 1024) + b'UNREAD TAIL')
            stream.read = Mock(wraps=stream.read)
            streams.append(stream)
            return stream
        async with app.run_test() as pilot:
            with patch.object(module, 'session_path', side_effect=resolve), patch.object(Path, 'open', side_effect=read):
                await pilot.press('enter')
                await app.workers.wait_for_complete()
            self.assertEqual(len(streams), 1)
            streams[0].read.assert_called_once_with(64 * 1024)
            self.assertTrue(all(thread != threading.get_ident() for thread in threads))
            from tests.test_textual import contents
            self.assertNotIn('UNREAD TAIL', contents(app))
            link = self.root / 'outside'
            link.symlink_to(self.root.parent, target_is_directory=True)
            detail = snapshot.details[('org/repo', 7, HEAD)]
            app.observer.snapshot = replace(snapshot, details={('org/repo', 7, HEAD): replace(detail, latest_session=str(link))})
            app.refresh_snapshot()
            await app.workers.wait_for_complete()
            self.assertFalse(app.screen.query_one('#detail-description').display)


class KeyboardProtocolTests(TestCase):
    def test_main_module_keeps_textual_off_the_kitty_keyboard_protocol(self):
        import lightkeeper.__main__  # noqa: F401
        self.assertEqual(os.environ.get("TEXTUAL_DISABLE_KITTY_KEY"), "1")
