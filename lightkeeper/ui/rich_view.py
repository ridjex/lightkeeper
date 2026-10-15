"""Shared literal formatting and static Rich rendering; no live screen."""
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from rich.console import Group
from rich.style import Style
from rich.table import Table
from rich.text import Text

from .idle import DEFAULT_LOOK, lighthouse
from .model import Screen
from .project import label

STATES = {
    "reviewing": ("⠋", "*", "bold cyan", "reviewing"),
    "waiting": ("○", "o", "yellow", "waiting"),
    "approved": ("✓", "+", "green", "approved"),
    "changes": ("✗", "x", "red", "changes"),
    "comment": ("✎", "c", "blue", "comment"),
    "silent": ("–", "-", "dim", "silent"),
    "withheld": ("–", "-", "dim", "withheld:"),
    "yours": ("↩", "<", "dim", "yours"),
    "closed": ("↷", ">", "dim", "closed"),
    "merged": ("↷", ">", "dim", "merged"),
    "unknown": ("?", "?", "magenta", "unknown"),
    "error": ("!", "!", "bold red", "error"),
}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Strip complete escape sequences before stripping remaining control/format characters.
ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")


def clean(value) -> str:
    return "".join(c for c in ESCAPES.sub("", str(value)) if unicodedata.category(c) not in ("Cc", "Cf", "Cs"))


def text(value="", style="") -> Text:
    return Text(clean(value), style=style, no_wrap=True, overflow="ellipsis")


def multiline(value="", style="") -> Text:
    """Preserve line breaks in bodies and detail panes, stripping all other controls."""
    return Text("\n".join(clean(line) for line in str(value).split("\n")), style=style, overflow="fold")


def age(seconds: float) -> str:
    n = max(0, int(seconds))
    if n < 60:
        return f"{n}s"
    if n < 3600:
        return f"{n // 60}m"
    return f"{n // 3600}h{n % 3600 // 60:02}m" if n < 36000 and n % 3600 >= 60 else f"{n // 3600}h"


def countdown(seconds):
    n = max(0, int(seconds))
    return f"{n // 60}:{n % 60:02}"


def glyph(state, mono_now, ascii):
    g, a, style, _ = STATES[state]
    if state == "reviewing":
        g = SPINNER[int(mono_now * 4) % len(SPINNER)]
    return text(a if ascii else g, style)


def word(row):
    return {"approved": "would approve", "changes": "would request changes", "comment": "would comment"}.get(row.state,
            STATES[row.state][3]) if row.dry_run else STATES[row.state][3]


def ci_text(ci, ascii=False):
    if ci is None:
        return text("?", "dim")  # not fetched since restart
    if not ci.total:
        return glyph("silent", 0, ascii)
    result = text()
    for count, state in ((ci.passed, "approved"), (ci.running, "waiting"),
                         (ci.failed, "changes"), (ci.other, "silent")):
        if count:
            if result.plain:
                result.append(" ")
            result.append_text(glyph(state, 0, ascii)).append(str(count), STATES[state][2])
    return result


def item_text(item, repos, width):
    # Ellipsise only the repository portion: the identifying #number always survives.
    suffix = f"#{item.number}"
    full_label = text(label(item, repos))
    if full_label.cell_len <= width:
        full_label.stylize(Style(bold=True, link=clean(item.url)))
        return full_label
    repo_name, _, environment = label(item, repos).partition(suffix)
    environment = text(environment)
    environment.truncate(max(0, min(width // 3, width - len(suffix))), overflow="ellipsis")
    repo = text(repo_name)
    repo.truncate(max(0, width - len(suffix) - environment.cell_len), overflow="ellipsis")
    repo.append(suffix).append_text(environment)
    repo.stylize(Style(bold=True, link=clean(item.url)))
    return repo


def stats_text(screen, ascii=False, width=None):
    result = text(" today  ", "dim")
    for action, state in (("approve", "approved"), ("request_changes", "changes"), ("comment", "comment")):
        result.append_text(glyph(state, 0, ascii)).append(f" {dict(screen.stats.posted).get(action, 0)} {state}  ")
    if screen.stats.dry_run:
        result.append("dry-run " + ", ".join(f"{n} would {a.replace('_', ' ')}" for a, n in screen.stats.dry_run), "italic")
        result.append("  ")
    needs = f"{'#' if ascii else '◆'} {sum(bool(r.needs_you) for r in screen.watching)} needs you"
    if width is not None and result.cell_len + len(needs) > width:
        result.truncate(max(0, width - len(needs) - 2), overflow="ellipsis")
        result.append("  ")
    result.append(needs, "bold yellow")
    return result


def footer_text(current, hints="", message=""):
    result = multiline()
    if message:
        result.append(clean(message) + "\n")
    result.append("m menu · ", "dim")
    for number, name in enumerate(("Overview", "Repositories", "Agent"), 1):
        if number > 1:
            result.append(" · ", "dim")
        result.append(f"{number} {name}", "bold reverse" if number == current else "dim")
    result.append(" · L logs · q quit · ? help", "dim")
    if hints:
        result.append("\n" + clean(hints), "dim")
    return result


IDLE_TIPS = (
    "Enter on a history row opens its PR",
    "Tab moves between sections; o opens GitHub",
    "2 opens repositories; 3 opens the agent",
    "r checks now; ? shows keyboard help",
)


def idle_panel(screen, now, width, ascii=False, *, free_rows=14, no_color=False,
               look=DEFAULT_LOOK, hour=None, date=None, event_time=None, preview=False):
    """Ten picture rows plus status and tip; three text rows in a tight viewport."""
    deadline = screen.runtime.next_check_at
    check = f"next check in {countdown(deadline - now)}" if deadline is not None else "next check pending"
    separator = " | " if ascii else " · "
    count = len(screen.runtime.repos)
    state = "watching" if screen.watching else "all quiet"
    status = separator.join((state, f"{count} {'repository' if count == 1 else 'repositories'} watched",
                             check, "r to check now"))
    tip = IDLE_TIPS[int(now) // 12 % len(IDLE_TIPS)]
    compact = not preview and (width < 60 or free_rows < 14)  # Picture 10 + status + tip + 2 spare rows.
    lines = ([f"{state} ({count} watched)", separator.join((check, "r to check now")), tip]
             if compact else [status, tip])
    rendered = [] if compact else lighthouse(now, width, count, screen.runtime.phase == "checking",
                                              ascii, no_color=no_color, look=look, hour=hour,
                                              date=date, event_time=event_time)
    for line in lines:
        value = text(line, "dim")
        value.truncate(max(1, width), overflow="crop" if ascii else "ellipsis")
        rendered.append(value)
    return Group(*rendered)


def header_lines(screen: Screen, mono_now: float, width: int, ascii=False, queued=""):
    runtime = screen.runtime
    total = runtime.configured_repo_count if runtime.configured_repo_count is not None else len(runtime.repos)
    live = sum(repo not in runtime.dry_repos for repo in runtime.repos) if runtime.mode == "live" and runtime.live_lock_held else 0
    repo_count = f"{total} repos" + (f" ({live} live)" if live != total else "")
    wide = width >= 140
    elapsed = max(0, mono_now - runtime.phase_since)
    header = text(" lightkeeper", "bold")
    header.append("  ")
    failed = any(a.key == "gh_identity" for a in screen.alerts)
    matches = runtime.gh_confirmed is True and runtime.gh_login == runtime.login
    actual = runtime.gh_login if runtime.gh_confirmed else None
    identity = (f"gh {clean(runtime.gh_login)} {'ok' if ascii else '✓'}" if matches else
                f"gh {'x' if ascii else '✗'} {clean(actual or 'unknown')}")
    unchecked = runtime.gh_confirmed is None and not failed
    unconfirmed = runtime.gh_confirmed is False and not failed
    if unchecked:
        identity = "gh ..." if ascii else "gh …"
    elif unconfirmed:
        identity = f"gh ? {clean(runtime.gh_login or 'unknown')} (unconfirmed)"
    elif not matches:
        identity += f" (expected {clean(runtime.login)})"
    header.append(identity + "  ", "dim" if unchecked or unconfirmed else "green" if matches else "red")
    health = runtime.engine_identity
    mark = ("..." if ascii else "…") if health is None else (("ok" if ascii else "✓") if health.ok else ("x" if ascii else "✗"))
    header.append(f"{clean(runtime.engine_name)} {clean(runtime.engine_profile)} {mark}  ",
                  "dim" if health is None else "green" if health.ok else "red")
    if runtime.updates and runtime.update_version:
        header.append(f"update {clean(runtime.update_version)}  ", "dim")
    header.append(" LIVE " if runtime.mode == "live" else " DRY-RUN ",
                  "reverse green" if runtime.mode == "live" else "bold reverse yellow")
    header.append("   ")
    if runtime.phase == "reviewing":
        header.append_text(glyph("reviewing", mono_now, ascii)).append(" reviewing ")
        if runtime.current_item:
            header.append_text(item_text(runtime.current_item, runtime.repos, 30))
        header.append(f" · {int(elapsed)}s", "red" if elapsed >= 300 else "yellow" if elapsed >= 120 else "cyan")
    elif runtime.phase == "finishing":
        header.append("finishing ")
        if runtime.current_item:
            header.append_text(item_text(runtime.current_item, runtime.repos, 30))
    elif runtime.phase in ("sleeping", "backoff") and runtime.next_check_at is not None:
        remaining = runtime.next_check_at - mono_now
        header.append(("backoff " if runtime.phase == "backoff" else "next check in ") +
                      (countdown(remaining) if remaining >= 0 else "overdue"), "yellow")
    elif runtime.phase == "checking" and runtime.phase_reason:
        header.append(f"checking now ({clean(runtime.phase_reason)})")
    else:
        header.append(runtime.phase + (f" {runtime.progress[0]}/{runtime.progress[1]}" if runtime.phase == "checking" else ""))
    if queued:
        header.append(" · " + clean(queued), "yellow")
    shown_config = str(runtime.config_dir).replace(str(Path.home()), "~", 1)
    right = text(f"settings {shown_config} · {runtime.model}/{runtime.effort} · "
                 f"up {age(mono_now - runtime.started_at)}", "dim")
    def header_line(left, right, required=False):
        table = Table.grid(expand=True)
        table.add_column(no_wrap=True, overflow="ellipsis", ratio=1)
        if (required and width >= 60) or (wide and width - right.cell_len > left.cell_len + 2):
            table.add_column(no_wrap=True, overflow="ellipsis", justify="right", width=right.cell_len)
            table.add_row(left, right)
        else:
            table.add_row(left)
        return table
    last = "last check -"
    if runtime.last_check_at is not None:
        snapshot_mono = screen.mono_at if screen.mono_at is not None else mono_now
        stamp = screen.wall_now - max(0, snapshot_mono - runtime.last_check_at)
        last = f"last check {datetime.fromtimestamp(stamp):%H:%M:%S} {'ok' if runtime.last_check_ok else 'failed'} {runtime.last_check_seconds:.1f}s"
    counts = text(repo_count + (" · " + last if wide else ""), "dim")
    stats_width = width - counts.cell_len - 2 if width >= 60 else width
    return Group(header_line(header, right),
                 header_line(stats_text(screen, ascii, stats_width), counts, required=True))


def row_data(screen, rows, mono_now, width, ascii=False, historical=False):
    runtime = screen.runtime
    wide = width >= 140
    elapsed = max(0, mono_now - runtime.phase_since)
    item_width = min(32, max((text(label(r.item, runtime.repos)).cell_len for r in rows), default=8))
    item_width = min(item_width, max(8, width // 3))
    state_width = max((len(word(r)) + (4 if r.dry_run or getattr(r, "repo_dry", False) else 0) for r in rows), default=9)
    show_author = wide or not historical
    author_width = min(14, max((text(r.item.author).cell_len for r in rows), default=0))
    author_width = max(6, author_width)
    if not historical:
        # Preserve author and CI, then spend remaining cells on the explanation.
        item_width = min(item_width, max(8, width - author_width - state_width - 48))
    widths = [9 if historical else 1, 1, item_width]
    if show_author:
        widths += [author_width]
    if wide:
        widths += [7] + ([] if historical else [min(30, max(0, width - item_width - state_width - author_width - 84))])
    ci_width = max([1] + [ci_text(r.ci, ascii).cell_len for r in rows if r.item.kind == "pr"]) if not historical else 0
    widths += [state_width, None] + ([] if historical else [min(12, ci_width), 6])
    result = []
    for row in rows:
        leading = (datetime.fromtimestamp(row.at).strftime("%H:%M" if datetime.fromtimestamp(row.at).date() ==
                   datetime.fromtimestamp(screen.wall_now).date() else "%a %H:%M") if historical else
                   (("#" if ascii else "◆") if row.needs_you else " "))
        cells = [text(leading, "dim" if historical else "bold yellow"), glyph(row.state, mono_now, ascii),
                 item_text(row.item, runtime.repos, item_width)]
        if show_author:
            cells.append(text(row.item.author))
        if wide:
            cells.append(text(row.item.head[:7], "dim"))
            if not historical:
                cells.append(text(row.item.title or row.item.environment, "dim"))
        why = row.why if historical or row.state == "withheld" else row.needs_you or row.why
        if historical:
            why = (f"({row.change_text or 'new rules'}) " if row.new_rules else "") + why + (f" (×{row.repeats})" if row.repeats > 1 else "")
        shown_word = ("dry " if row.dry_run or getattr(row, "repo_dry", False) else "") + word(row)
        cells.extend([text(shown_word, STATES[row.state][2] + (" italic" if row.dry_run else "")), text(why)])
        if not historical:
            cells.append(ci_text(row.ci, ascii) if row.item.kind == "pr" else text())
            since_snapshot = max(0, mono_now - screen.mono_at) if screen.mono_at is not None else 0
            seconds = elapsed if row.state == "reviewing" else screen.wall_now - row.since + since_snapshot
            cells.append(text(age(seconds), "dim"))
        result.append(cells)
    return widths, result


def rows_table(screen, rows, mono_now, width, ascii=False, historical=False):
    widths, cells = row_data(screen, rows, mono_now, width, ascii, historical)
    table = Table(box=None, show_header=False, expand=True, padding=(0, 1), pad_edge=True)
    for index, w in enumerate(widths):
        right = (historical and index == 0) or (not historical and index == len(widths) - 1)
        table.add_column(width=w, ratio=1 if w is None else None, no_wrap=True, overflow="ellipsis",
                         justify="right" if right else "left")
    for row in cells:
        table.add_row(*row)
    return table


def render(screen: Screen, mono_now: float, width: int, height: int, ascii: bool = False):
    runtime = screen.runtime
    parts = list(header_lines(screen, mono_now, width, ascii).renderables)
    alert_lines = []
    alerts = tuple(a for a in screen.alerts if a.key != "reviewer_paused" or
                   not runtime.reviewer_paused_until or mono_now < runtime.reviewer_paused_until)
    for alert in alerts[:3]:
        message = alert.text
        if alert.key == "reviewer_paused" and runtime.reviewer_paused_until:
            message += f"; resumes in {countdown(runtime.reviewer_paused_until - mono_now)}"
        alert_lines.append(text(f" {'^' if ascii else '▲'} {message}", "red" if alert.level == "error" else "yellow"))
    if len(alerts) > 3:
        alert_lines.append(text(f" +{len(alerts) - 3} more alerts", "yellow"))
    # Reserve header, headings, footer, alerts; then remove blanks, history, ordinary watching rows.
    watching = sorted(screen.watching, key=lambda r: (not bool(r.needs_you), r.state != "reviewing", r.since))
    history = list(screen.history[:10])
    base = 5 + len(alert_lines)
    blanks = 4 if alert_lines else 3
    blanks = max(0, min(blanks, height - base - len(watching) - len(history)))
    while base + blanks + len(watching) + len(history) > height and len(history) > 3:
        history.pop()
    capacity = max(sum(bool(r.needs_you) for r in watching), height - base - blanks - len(history))
    hidden = 0
    if len(watching) > capacity:
        keep = max(sum(bool(r.needs_you) for r in watching), capacity - 1)
        hidden, watching = len(watching) - keep, watching[:keep]
    def gap():
        nonlocal blanks
        if blanks:
            parts.append(text())
            blanks -= 1
    gap()
    parts.extend(alert_lines)
    if alert_lines:
        gap()
    parts.append(text(f" WATCHING {len(screen.watching)}", "bold"))
    parts.append(rows_table(screen, watching, mono_now, width, ascii))
    if hidden:
        parts.append(text(f" +{hidden} more", "dim"))
    gap()
    parts.append(text(f" HISTORY latest {len(history)}", "bold"))
    parts.append(rows_table(screen, history, mono_now, width, ascii, True))
    gap()
    parts.append(text(f" Ctrl-C to stop · log {screen.log_path} · details: lightkeeper --status", "dim"))
    return Group(*parts)


def print_summary(screen, console, ascii=False):
    console.print(text(" lightkeeper stopped"))
    console.print(stats_text(screen, ascii))
    needs = [label(r.item, screen.runtime.repos) for r in screen.watching if r.needs_you]
    console.print(text(" needs you: " + (", ".join(needs) or "none")))
    console.print(text(" alerts: " + ("; ".join(a.text for a in screen.alerts) or "none")))
    console.print(text(f" log {screen.log_path}"))
