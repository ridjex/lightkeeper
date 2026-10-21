"""One ten-row lighthouse: two vertical samples per literal Rich Text cell."""
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from math import cos, floor, sin, sqrt, tau
from time import localtime

from rich.style import Style
from rich.text import Text


@dataclass(frozen=True)
class Look:
    tempo: float = .8
    daytime: bool = False
    moon: bool = False
    events: bool = False
    needs_you: bool = False


DEFAULT_LOOK = Look(tempo=.8, daytime=True, moon=True, events=True)
# One four-row tower: lantern, gallery, tapered shaft, foot with a door.
TOWER = ('  ✦  ', ' ━█━ ', ' ▐█▌ ', '▟█ █▙')
ASCII_TOWER = ('  *  ', ' -#- ', ' /#\\ ', '/# _\\')
HEIGHT = 20
HORIZON = 12.8


def _palette(*anchors):
    """Sixteen interpolated shades."""
    rgb = [tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)) for color in anchors]
    colors = []
    for level in range(16):
        position = level * (len(rgb) - 1) / 15
        start = min(int(position), len(rgb) - 2)
        colors.append('#%02x%02x%02x' % tuple(
            round(a + (b - a) * (position - start))
            for a, b in zip(rgb[start], rgb[start + 1])))
    return tuple(colors)


SEA = _palette('#1e1e22', '#254f79', '#39a6ba', '#d7f6e9')
STONE = _palette('#1e1e22', '#b7bac4')
MOON = _palette('#1e1e22', '#f0f0df')
SUN = _palette('#30353d', '#ffc66d', '#fff1ad')
FIRE = _palette('#8c251e', '#f35c28', '#ffe291')
# Amber light calls the owner to an unresolved needs-you item, retaining the beam's shading.
AMBER_BEAM = _palette(SEA[0], FIRE[12], FIRE[-1])


def _styles(palette):
    return tuple(Style(color=palette[top] if top != bottom else None, bgcolor=palette[bottom])
                 for top in range(16) for bottom in range(16))


SEA_STYLES = _styles(SEA)


@lru_cache(maxsize=24)
def daytime_palette(hour):
    night = ('#1e1e22', '#254f79', '#39a6ba', '#d7f6e9')
    warm = ('#252735', '#34496c', '#d19659', '#fff0cb')
    day = ('#163454', '#205b93', '#4c95c8', '#daeefa')
    if 9 <= hour < 18:
        return _palette(*day)
    # Blend warm dawn/dusk into the night and blue daytime palettes.
    anchors = ((0, night), (5, night), (7, warm), (10, day),
               (16, day), (19, warm), (21, night), (24, night))
    for (start, first), (end, last) in zip(anchors, anchors[1:]):
        if start <= hour < end:
            weight = (hour - start) / (end - start)
            colors = ['#%02x%02x%02x' % tuple(round(int(a[i:i + 2], 16) * (1 - weight)
                                                     + int(b[i:i + 2], 16) * weight)
                                             for i in (1, 3, 5)) for a, b in zip(first, last)]
            return _palette(*colors)
    raise ValueError('hour must be in 0..23')


@lru_cache(maxsize=24)
def daytime_styles(hour):
    return _styles(daytime_palette(hour))


def lunar_phase(date):
    """Fraction of a synodic month since the 2000-01-06 18:14 UTC new moon."""
    epoch = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    return ((date - epoch).total_seconds() / 86400 / 29.53) % 1


def _sky(hour, date):
    if date is None:
        date = datetime.now(timezone.utc)
        if hour is None:
            local = localtime()
            hour = local.tm_hour + local.tm_min / 60 + local.tm_sec / 3600
    if hour is None:
        hour = date.hour + date.minute / 60 + date.second / 3600
    return hour % 24, lunar_phase(date)


def _disc_position(width, hour, sun):
    progress = (hour - 7) / 12 if sun else ((hour - 19) % 24) / 12
    cx = 22 + max(0, width - 26) * progress  # The arc starts right of the tower.
    cy = HORIZON - 9.8 * sqrt(max(0., 1 - (2 * progress - 1) ** 2))
    return cx, cy


def _disc(width, hour, phase, sun):
    """An ellipse across the sky, with continuous x and area-sampled disc rims."""
    if (sun and not 7 <= hour <= 19) or (not sun and 7 < hour < 19):
        return width / 2, {}
    cx, cy = _disc_position(width, hour, sun)
    terminator = cos(tau * phase)
    pixels = {}
    for y in range(max(0, int(cy - 3)), min(HEIGHT, int(cy + 3) + 1)):
        for x in range(max(0, int(cx - 4)), min(width, int(cx + 4) + 1)):
            covered = lit = 0
            for sy in range(4):
                v = (y + (sy + .5) / 4 - cy) / 3
                if abs(v) > 1:
                    continue
                edge = sqrt(1 - v * v)
                for sx in range(8):
                    u = (x + (sx + .5) / 8 - cx) / 4
                    if abs(u) <= edge:
                        covered += 1
                        lit += sun or (u >= terminator * edge if phase < .5
                                       else u <= -terminator * edge)
            if covered:
                pixels[x, y] = (.2 * covered + .8 * lit) / 32
    return cx, pixels


def _event_hash(slot):
    value = (slot * 1664525 + 1013904223) & 0xffffffff
    return (value ^ (value >> 16)) * 2246822519 & 0xffffffff


def event_start(slot):
    return slot * 120 + _event_hash(slot) % 31


def event_at(t):
    """Starts 90–150 scene-seconds apart, without mutable/random state."""
    slot = int(t // 120)
    seed = _event_hash(slot)
    kind = ('meteor', 'ship', 'whale')[(seed >> 8) % 3]
    duration = {'meteor': 10. if seed % 4 == 0 else .6 + seed % 5 / 10 + 1,
                'ship': 40. + seed % 21, 'whale': 10.}[kind]
    elapsed = t - event_start(slot)
    return (kind, elapsed, duration, seed) if event_start(slot) <= t < event_start(slot) + duration else None


def _event(raster, accents, t, surfaces, beam):
    event = event_at(t)
    if event is None or not raster[0]:
        return
    kind, elapsed, duration, seed = event
    width = len(raster[0])

    def surface(x):
        return surfaces[max(0, min(width - 1, round(x)))]

    def dot(x, y, brightness, warm=False):
        x, y = floor(x + .5), floor(y + .5)
        if 0 <= x < width and 0 <= y < HEIGHT:
            raster[y][x] = brightness
            if warm:
                accents[y][x] = FIRE[_level(brightness)]
            else:
                accents[y].pop(x, None)

    if kind == 'meteor':
        count = 3 + seed % 4 if duration == 10 else 1
        life = .6 + seed % 5 / 10
        for i in range(count):
            age = elapsed - i * (10 - life - 1) / max(1, count - 1)
            hit_x = width * (.3 + ((seed >> (i + 2)) % 55) / 100)
            hit_y = surface(hit_x)
            if 0 <= age < life:
                p = age / life
                x, y = hit_x - 12 * (1 - p), hit_y * p
                for tail in range(5):
                    dot(x - tail * .8, y - tail, .98 - tail * .1, True)
            elif life <= age < life + 1:
                for dx, dy in ((-1, 0), (0, -1), (1, 0)):
                    dot(hit_x + dx, surface(hit_x + dx) + dy, .98)
    elif kind == 'ship':
        x = -10 + (width + 20) * elapsed / duration
        y = round(surface(x + 4))
        illumination = 0.
        if beam:
            beam_y, beam_width, power = beam[max(0, min(width - 1, round(x + 4)))]
            illumination = power * max(0., 1 - abs(y - beam_y) / beam_width)
        # A white hull by day; at night a grey hull with a lit top edge, and a
        # navigation light that flashes for .4 s every two seconds.
        night = bool(beam)
        for dy, inset in ((0, 0), (1, 1)):
            for dx in range(inset, 10 - inset):
                dot(x + dx, y + dy, (.7 if dy == 0 else .16 + .1 * illumination) if night else .85)
        for dy in range(1, 4):
            dot(x + 5, y - dy, max(.5, .18 + .4 * illumination) if night else .3)
        flash = elapsed % 2 < .4
        dot(x + 3, y - 1, 1. if flash else (.4 if night else .6), True)
    else:
        x = int(width * (.3 + seed % 35 / 100)) + elapsed * .25
        breadth = 14 + seed % 5
        rise = min(1., elapsed / 2, (duration - elapsed) / 2)
        for dx in range(breadth):
            arc = sin((dx + .5) / breadth * tau / 2)
            y = surface(x + dx)
            count = round(rise * (1 + 2 * arc))
            for dy in range(count):
                dot(x + dx, y + 1 - dy, .7 if beam and dy == count - 1 else .06)
        if 2 <= elapsed < 4:
            y = surface(x + 3) - 2
            for dy in range(1, 4):
                dot(x + 3, y - dy, .98)
            dot(x + 2, y - 3, .85)
            dot(x + 4, y - 3, .85)
        if 6 <= elapsed < 8:
            y = surface(x + breadth - 2)
            for dx, dy in ((0, -1), (0, -2), (-1, -3), (1, -3), (-2, -3), (2, -3)):
                dot(x + breadth - 2 + dx, y + dy, .7 if beam and dy == -3 else .06)


def _level(value):
    return 0 if value <= 0 else 15 if value >= 1 else int(value * 15)


@lru_cache(maxsize=4096)
def _accent_style(top, bottom):
    return Style(color=top if top != bottom else None, bgcolor=bottom)


def _row(top, bottom, styles, ascii=False, no_color=False, marks=(), *,
         palette=SEA, accents_top=None, accents_bottom=None):
    """Quantize and merge samples; accents supply neutral stone and celestial colours."""
    pairs = [(_level(a), _level(b)) for a, b in zip(top, bottom)]
    if ascii or no_color:
        ramp = ' .:-=+*#' if ascii else ' ·:░▒▓█'
        chars = [ramp[(a + b) * (len(ramp) - 1) // 30] for a, b in pairs]
    else:
        chars = [' ' if a == b else '▀' for a, b in pairs]
    cell_styles = [styles[a * 16 + b] for a, b in pairs]
    if accents_top or accents_bottom:
        accents_top, accents_bottom = accents_top or {}, accents_bottom or {}
        for x in accents_top.keys() | accents_bottom.keys():
            a, b = pairs[x]
            top_color = accents_top.get(x, palette[a])
            bottom_color = accents_bottom.get(x, palette[b])
            cell_styles[x] = _accent_style(top_color, bottom_color)
            if not (ascii or no_color):
                chars[x] = ' ' if top_color == bottom_color else '▀'
    for x, label, level, color in marks:
        for offset, char in enumerate(label):
            pos = x + offset
            if 0 <= pos < len(chars):
                chars[pos] = char
                bg = cell_styles[pos].bgcolor
                cell_styles[pos] = _accent_style(color or palette[level], bg.name)
    result = Text(''.join(chars), no_wrap=True, overflow='crop')
    if not (ascii or no_color):
        start = 0
        for end in range(1, len(pairs) + 1):
            if end == len(pairs) or cell_styles[end] != cell_styles[start]:
                result.stylize(cell_styles[start], start, end)
                start = end
    return result


def _repository_stars(width, repos):
    """Eight fixed slots, lit in order; the disc covers them, never relocates them."""
    slots = ((.19, 2), (.48, 6), (.76, 2), (.32, 5),
             (.89, 6), (.60, 1), (.40, 0), (.68, 7))
    return [(round((width - 1) * x), y) for x, y in slots[:min(8, max(0, repos))]]


@lru_cache(maxsize=24)
def sky_palette(hour):
    if 9 <= hour < 18:
        return _palette('#163d73', '#649fd1')
    return _palette('#283e72', '#a16c88', '#e7ad84')


def _clouds(t, width):
    """Four broad ellipses; one screen width per nine minutes at default tempo."""
    pixels = {}
    for fraction, cy, breadth, height in ((.12, 3, 24, 4), (.39, 6, 18, 3),
                                         (.66, 2, 20, 3), (.87, 8, 14, 2)):
        cx = (fraction * (width + 24) + t * width / 540) % (width + 24) - 12
        for y in range(max(2, int(cy - height / 2)), int(cy + height / 2) + 1):  # Clear of the label row.
            v = (y + .5 - cy) / (height / 2)
            if abs(v) >= 1:
                continue
            half = breadth / 2 * sqrt(1 - v * v)
            for x in range(max(0, int(cx - half)), min(width, int(cx + half) + 1)):
                pixels[x, y] = min(1., max(0., half - abs(x + .5 - cx)))
    return pixels


def _beam(t, width, checking, day):
    """One continuous sweep per 4/8 scene-seconds; daylight has no beam."""
    if day:
        return []
    slope = .055 + .13 * sin(t * tau / (4 if checking else 8))
    return [(5 + (x - 8) * slope, .55 + max(0, x - 8) * .05,
             .72 if x > 9 else 0.) for x in range(width)]


def _raster(t, width, repos, checking, look, hour, phase, event_time):
    """Sky → stars → disc → clouds → water and beam → events → rock; glyphs finish the tower."""
    wave_t, scene_t = t * look.tempo, t * look.tempo / DEFAULT_LOOK.tempo
    day = look.daytime and 7 <= hour < 19
    cx, disc = _disc(width, hour, phase, day) if day or look.moon else (width / 2, {})
    beam = _beam(scene_t, width, checking, day)
    lit = 1. if day else max(.15, (1 - cos(tau * phase)) / 2)
    raster = [[0.] * width for _ in range(HEIGHT)]
    accents = [{} for _ in range(HEIGHT)]
    if day:
        colors = sky_palette(int(hour))
        for y in range(HEIGHT):
            level = min(15, round(y * 15 / HORIZON))
            raster[y] = [.25 + level / 60] * width
            accents[y] = dict.fromkeys(range(width), colors[level])
    else:
        slots = _repository_stars(width, 8)
        reserved = {(x, y + dy) for x, y in slots for dy in (0, 1)}
        for i, x in enumerate(range(13, width, 17)):
            y = (i * 7 + 1) % 10
            if (x, y) in reserved:
                continue
            raster[y][x] += (.65 if i % 4 == 0 else .32) + .22 * sin(wave_t * tau / (3 + i % 7) + i * 2.3)
        for x, y in slots[:max(0, repos)]:
            if 0 <= x < width:
                raster[y + 1][x] = 1.
                raster[y][x] = max(raster[y][x], .4)
    orb = SUN if day else MOON
    for (x, y), value in disc.items():
        raster[y][x] = value
        accents[y][x] = orb[_level(value)]
    if day:  # Clouds pass in front of the sun.
        clouds = _palette(colors[5], '#dbeafa' if 9 <= hour < 18 else '#f5c7a4')
        for (x, y), rim in _clouds(scene_t, width).items():
            raster[y][x] = .5 + .35 * rim
            accents[y][x] = clouds[round(7 + 7 * rim)]
    surfaces, slopes = [], []
    row_shimmer = [.5 + .5 * sin(y * 2.1 - wave_t * 1.3) for y in range(HEIGHT)]
    row_slope = [.16 * cos(y * 1.7 - wave_t * 1.3) for y in range(HEIGHT)]
    for x in range(width):
        u = x * 160 / max(1, width)
        a, b, c = u * .075 - wave_t * .7, u * .18 - wave_t * 1.1, u * .32 + wave_t * .6
        surfaces.append(HORIZON + 1.65 * sin(a) + .85 * sin(b) + .4 * sin(c))
        slopes.append(1.65 * .075 * cos(a) + .85 * .18 * cos(b) + .4 * .32 * cos(c))
    for x in range(width):
        surface = surfaces[x]
        foam = .18 + max(0., 13 - surface) * .16
        beam_y, beam_width, power = beam[x] if beam else (0., 1., 0.)
        reflection = max(0., 1 - abs(surface - beam_y) / (beam_width + 1)) * power
        beam_power = power * .66 / (1 + max(0, x - 8) * .005)
        depth_scale = 1 / max(1, HEIGHT - surface)
        moon_dx = ((x - cx) * 160 / max(1, width)) ** 2
        column_shimmer = .5 + .5 * sin(x * 160 / max(1, width) * .7 + wave_t * 1.7)
        for y in range(max(0, int(surface)) if day else 0, HEIGHT):
            depth = y + .5 - surface
            if depth < -.5:
                glow = beam_power * max(0., 1 - abs(y + .5 - beam_y) / beam_width)
                if glow and raster[y][x] < 1:
                    raster[y][x] += glow
                    accents[y].pop(x, None)  # The beam is near: it crosses the moon, never passes behind it.
                    if look.needs_you:
                        accents[y][x] = AMBER_BEAM[_level(raster[y][x])]
            else:
                shimmer = .22 + .78 * row_shimmer[y] * column_shimmer
                glint = max(0., 1 - abs(slopes[x] + row_slope[y]) / .22) ** 2
                path = lit * 1.7 * (.08 + .92 * glint) / (1 + moon_dx / (1.2 + max(0., depth) * .32) ** 2) if disc else 0.
                coverage = min(1., depth + .5)
                raster[y][x] = raster[y][x] * (1 - coverage) + ((.35 if day else .12) + .23 * (1 - depth * depth_scale)
                                + foam * max(0., 1 - abs(depth - .25))
                                + reflection * .42 * shimmer + path) * coverage
                accents[y].pop(x, None)
    if look.events:
        _event(raster, accents, scene_t if event_time is None else event_time, surfaces, beam)
    for x in range(min(width, 17)):
        rock = 11 + abs(x - 8) * .55
        level = (7 if day else 2) + (1 if (x >= 8) == (cx >= 8) else 0)
        for y in range(11, 16):
            if rock <= y + .5:
                raster[y][x] = level / 15
                accents[y][x] = STONE[level]
    return raster, accents


def lighthouse(t, width, repos, checking, ascii=False, *, no_color=False, look=DEFAULT_LOOK,
               hour=None, date=None, event_time=None) -> list[Text]:
    width = max(0, width)
    hour, phase = _sky(hour, date) if look.daytime or look.moon else (0., 0.)
    day = look.daytime and 7 <= hour < 19
    palette = daytime_palette(int(hour)) if look.daytime else SEA
    styles = daytime_styles(int(hour)) if look.daytime else SEA_STYLES
    raster, accents = _raster(t, width, repos, checking, look, hour, phase, event_time)
    cx, _ = _disc_position(width, hour, day) if day or look.moon else (width / 2, 0)
    result = []
    figure = ASCII_TOWER if ascii else TOWER
    for y in range(10):
        marks = []
        if y == 0:
            label = f' {repos} stars '
            if width - len(label) > 10:
                marks.append((width - len(label), label, 15, None))
        if 2 <= y < 6:
            row = y - 2
            if row == 0:
                marks.append((8, figure[row][2], 15 if checking else 6, None))
            else:
                for i, char in enumerate(figure[row]):
                    level = (7 if day else 2) + (1 if (i >= 2) == (cx >= 8) else 0)
                    if row == 3 and i == 2:
                        marks.append((6 + i, '_' if ascii else '▗', 1, STONE[1]))
                    elif char != ' ':
                        marks.append((6 + i, char, level, STONE[level]))
        result.append(_row(raster[y * 2], raster[y * 2 + 1], styles, ascii, no_color, marks,
                           palette=palette, accents_top=accents[y * 2], accents_bottom=accents[y * 2 + 1]))
    return result
