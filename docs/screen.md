# The screen

What `lightkeeper` shows while it runs, and the idle picture's settings and preview keys.

`m` opens the menu on every screen: Overview · Repositories · Agent · Check now · Screen settings ·
Edit settings.toml · Edit rules.md · Edit template.md · Check for updates now · Help · Quit. Navigation
entries are refused while a form or confirmation is open. The header shows `update X.Y.Z` in dim text when
GitHub has a newer release (checked once a day through your `gh`; `[screen] updates = false` turns it off).

`lightkeeper` opens the Textual Overview: current activity and today's posted counts (local calendar day),
active alerts, watched PRs and deployment gates, and recent verdicts. Dry-run repository rows carry a
`dry` marker; `◆` marks items needing you. One cursor walks Alerts, Watching and History; tables scroll
when their rows do not fit. Watching uses one line per row; alerts use their rendered row heights.
Watching keeps at least 12 rows of space, whether empty or populated. With
`[screen] idle = "quiet"`, empty Watching fills that space with a full-width lighthouse, repository
count, next-check countdown, check-now hint and a tip rotating every 12 seconds. As soon as watched
rows arrive, the table replaces the picture in the same space. The default `idle = "always"` puts the picture
above the Watching heading, between Alerts and Watching; Watching keeps its own 12-row minimum.
History gets the remaining height. Below 60 columns or 14 free rows, the picture becomes three text
rows; it hides if its slot would leave no room for a History row. The Watching minimum still applies;
if alerts and the fixed blocks together exceed the terminal height, Overview scrolls to keep History
reachable.

Needs-you items appear first in Alerts as bold amber `◆` lines. The header badge `◆ N needs you` uses
bold reverse video when nonzero, Watching and PR detail show `needs you`, and the nighttime lighthouse
beam turns amber (not in ASCII or NO_COLOR modes, where the badge carries the information). `Enter` on an alert opens PR detail; `o` opens GitHub from either place, and `A` in
detail opens owner approval with confirmation. Inspect the change before approving. Alerts clear when
the item no longer needs you; History keeps the actual verdict. ASCII uses `#`; `NO_COLOR` retains
the badge's reverse-video emphasis.

Tempo defaults to 0.8; the local-hour palette and sun/moon position, moon and rare events are on.
The scene starts at 10 fps while visible. A frame is skipped while Textual's writer queue still holds the
previous one (the terminal is behind; without a writer thread, a timer more than one interval late counts
the same), and the rate steps down through 5, 2 and 1 fps; 30 consecutive on-time frames recover one step.
Losing focus caps it at 1 fps without changing the step; focus restores the adaptive rate. It pauses whenever neither slot is visible, including
behind other screens. Fewer frames show the same scene at the same time; tempo still scales only scene clocks.
The Agent screen (`3`) shows `Screen: 10 fps`, or the reduced rate, `terminal behind` and the total
frames skipped; expanded Overview help (`?`) shows it too.
Set `LIGHTKEEPER_SCREEN_TRACE=1` to log a `screen-trace` JSON line to `run.log` every five seconds. Each
window records UTC time, interval, rendered/skipped frames, maximum timer lateness, the deepest writer
backlog, call counts and maximum durations of snapshot refresh and animation, and terminal width/height.
High lateness or backlog with low durations means the terminal is behind; high durations mean the app is.
Intervals, lateness and durations are in seconds. Empty or unwritable state directories are silently skipped.
ASCII/non-UTF-8
and NO_COLOR terminals get a monochrome version of the same picture. The lighthouse watches rolling
sine waves with eight fixed constellation slots: the first N light for N watched repositories,
capped at eight, with an exact "N stars" label. Stars stay put when a disc passes in front of them.
The picture stays ten rows high (20 sub-pixels); the extra sky leaves wave amplitude unchanged.
At default tempo the beam sweeps every eight seconds while idle, continuously every four seconds while checking; only its cadence and lantern
brightness change. History gets the remaining height and scrolls; empty History says "no reviews yet today". Selection follows
the item across snapshot refreshes. Every screen keeps the navigation hints visible and highlights its
current section; repository totals include disabled entries, with an effective live count when fewer
are live (for example, `8 repos (6 live)`).
`lightkeeper --preview-idle` opens the offline lighthouse experiment kit without
GitHub or the journal. It starts with the dashboard's `DEFAULT_LOOK`: tempo 0.8; daytime, moon and
events on. The preview remains the tuning tool. Preview changes last only for
that session and start from defaults independently of saved screen settings. The footer shows enabled
Look fields and the hour, with keys on a second line when needed. One tapered lighthouse has a gallery, shaded sides and a dark door.

| Setting | Default | Behaviour |
|---|---|---|
| `[screen] idle` | `"always"` | `"quiet"`: picture inside empty Watching; `"always"`: picture above Watching, which keeps its own 12 rows. |
| `[screen] tempo` | `0.8` | Number from `0.2` through `1.5`; scales waves, beam, clouds and event clocks. |
| `[screen] clock` | `true` | Boolean; use the local hour for the palette and sun/moon position (`Look.daytime`). |
| `[screen] moon` | `true` | Boolean; show the date-derived moon at night (`Look.moon`). |
| `[screen] events` | `true` | Boolean; show rare whale, ship and meteor events (`Look.events`). |
| `[screen] updates` | `true` | Boolean; once a day ask GitHub for a newer release and show `update X.Y.Z` in the header. |

All six settings default when absent and reload between ticks; the switches accept only TOML booleans.
The menu (`m`) opens a Screen settings form that edits exactly these keys and applies them at once.

| Preview key | Experiment |
|---|---|
| `c` | Toggle checking: faster beam and bright lantern |
| `+` / `-`, `q` | Repository count; quit |
| `[` / `]` | Tempo −/+ 0.1, clamped to 0.2–1.5 |
| `d`, `,` / `.` | Local-hour palette; scrub −/+ one hour, wrapping at midnight |
| `m` | Date-derived lunar disc at night; the daytime sun appears automatically with `d` |
| `e`, `E` | Rare deterministic events; enable events and force the next slot now |
| `n` | Toggle needs-you amber beam (use `d` or scrub to night to see the beam) |

Dawn and dusk retain warm palettes. Day (07:00–19:00) has a blue sky gradient, deeper at the top,
lighter toward the horizon, blue sea, pale foam and a warm sun; the beam is off. Four broad, soft
clouds drift across the daytime sky in nine minutes at default tempo, tinted at dawn/dusk.
Hourly palettes are lazily cached (at most 24). The optional moon has a dim grey dark side and
uses a 29.53-day synodic approximation, not an ephemeris. Sun (07:00–19:00) and moon (19:00–07:00)
follow continuous half-ellipses with sampled rims, rising and setting behind the water.
Moon-only mode uses the same night arc and hides the moon during 07:00–19:00.
The beam cannot illuminate the moon. Both discs cast a bright, slope-dependent broken reflection.
Tower and rock have a lighter disc-facing side; the body is lighter by day. Checking brightens the lantern.
Events draw over water by day and night: a 14–18-column whale back with a two-second spout and
separate two-second tail fin over ten seconds; a ten-column ship crossing in 40–60 seconds;
a warm red-orange meteor falling in 0.6–1.0 seconds followed by a one-second surface splash
(occasionally 3–6 meteors over ten seconds). Deterministic slots start 90–150 seconds apart.
These durations and the beam periods are at default tempo; tempo scales all animation clocks.
Everything remains a two-dimensional big-pixel picture made from literal Rich Text.

| Key | Action |
|---|---|
| `m` | Menu: every screen, action and setting |
| `↑` / `↓`, `Tab` | Previous/next row; next section |
| `n` | Next item needing you |
| `Enter` | Open PR detail; deployment opens its run; needs-you alert opens its item; other alerts expand/collapse |
| `o`, `r` | Open the selected item on GitHub; check now |
| `?`, `q` | Toggle key help; quit |
| `t`, `c` (alert) | Run its named check through the Runner; copy a fix command when available |
| `o`, `s`, `p` (PR detail) | GitHub; session folder; toggle read-only prompt/raw answer |
| `R`, `Esc` (PR detail) | Confirm re-review of this head; return to Overview |
| `A` (PR detail) | Confirm your own approval, with an optional note (live only) |
| `1` / `2` / `3` | Overview / Repositories / Agent |
| `L` | Open the state/logs folder |
| `Space`, `Enter`, `a`, `n` (Repositories) | Pause/resume; edit; add; edit repository notes |
| `t`, `c` (Agent) | Probe engine; copy its auth fix command |
| `e`, `r`, `T` (Agent) | Open settings, rules, template in the built-in editor |
| `Ctrl-S`, `Esc` (editor) | Validate and save; close (confirm discarding unsaved changes) |
| `Ctrl-E` (editor) | Open a clean buffer in `$VISUAL` / `$EDITOR` / `vi` |

PR detail scrolls through clearly labelled Status, Assessment, Posted, Description and Timeline sections.
A bold PR title sits above known author/head/base metadata; draft and push age appear only when applicable
or known, never inferred from PR update time. Status aligns state, reason, next step, CI counts and failed
check URLs, known reviewers and any recorded merged/closed time. Closed/merged PRs without fetched checks
say "no checks". Assessment shows `why` in bold, then `watch` when present, followed by the existing
public summary and severity/origin bullets, separated from a dim
"private · journal only" box; absent assessments say "no assessment on this head". Posted shows the
outcome, recorded time, review URL and literal body (or dry-run/uncertain attempt); withheld and
nothing-posted outcomes omit the body. Timeline aligns time, event and summary/reason, collapsing
identical events with ×N. All fetched text stays literal, including markup-looking brackets.
When a session prompt is available, Description shows its first 12 description lines as fetched.
This is a bounded 64 KiB worker read of `prompt.md` within the state directory;
it makes no extra GitHub or model requests. `p` still opens the prompt/raw answer and `s` the session folder.

Owner approve is available only from PR detail. The confirmation shows the captured head, model verdict,
public summary, effective mode and identity, and an exact body preview. GitHub receives your note first
(HTML comment delimiters, fence lines and marker-like lines neutralised), then a blank line and
`Approved by @<login> from the lightkeeper screen.` With no note, only that sentence is posted.
It includes no model private reason or automation trace and counts as your manual review: history says
`approved by you`, today's approved count includes it, and automation leaves later pushes to you.

The Runner reloads settings and refuses invalid settings or a changed login, a disabled repository,
disallowed approve, global/repository dry-run, missing live lock, mismatched/unavailable `gh` identity,
an unknown write on this head, an unavailable/closed PR, a moved head, your own PR, or your pending review.
An approval already on this head is reported as `already approved by you` without another POST.
You can override the approval floor (bot author, failed check, missing patch) with a non-empty note;
both the confirmation and Runner require it. A timeout stays `unknown` until GitHub shows your manual
approval; the tool does not retry that write. Quitting during the POST waits for its journaled outcome.

`lightkeeper --plain` prints transition lines instead; redirected output and `TERM=dumb`
also use plain mode. Missing Textual falls back to plain with one warning. `--once` and `--dry-run` work in either mode.
`LIGHTKEEPER_ASCII=1` selects ASCII glyphs; non-UTF-8 output does so automatically. `NO_COLOR`
removes colour without removing state words. All modes keep `run.log` beside the journal (0600,
5 MB with three rotated backups). `lightkeeper --status` prints watched, deferred, unknown, and recent notifications.

The runner works in a separate thread. `q`, Ctrl-C or SIGTERM waits for an active GitHub write to finish
and journal its outcome (the screen says `finishing <item>`); otherwise, after 20 seconds it kills the
active engine process and joins the runner before releasing either lock. Model results arriving after
stop stay unassessed. Exit restores the terminal and prints the stop summary.

Repositories shows enabled state, effective posting mode, remarks, allowed `A R C` actions (dim when
disabled), deployment review and the last access check. `Space` pauses/resumes without confirmation.
`Enter` opens all repository fields; `a` starts with enabled, dry-run, important remarks, all actions and
no deployments. The form places each label above its control, with explicit `[X]` / `[ ]` checkboxes
for Enabled, Deployments, and Approve / Request changes / Comment (`A` / `R` / `C`). Post and Remarks
selects have plain-language help beneath them; Save / Cancel / Remove remain available.
Switching to live asks for confirmation; without the live lock the screen says
"restart required for live" and effective mode stays dry-run. New repositories are saved only after a
successful Runner access check; inaccessible and archived repositories are refused. Remove asks and
keeps journal history. Remarks and notes saves report the number of watching items to re-evaluate.
The Runner reloads settings next tick and rechecks authority before every write.

Agent is a compact read-only panel with the lightkeeper version (and `update X.Y.Z available: brew upgrade lightkeeper or pipx upgrade lightkeeper` when GitHub has a newer release), engine/model/effort/profile, auth status and fix command,
actual/expected GitHub login, config/state directories, notifications and uptime.
The menu's file entries, Agent's `e` / `r` / `T` and Repositories' `n` open a built-in plain-text editor.
Markdown wraps; TOML scrolls horizontally. Tab does not indent; there are no line numbers or syntax colours.
`Ctrl-S` validates and saves, then closes and wakes the Runner. Invalid text stays in the editor with an
error message. `Esc` closes unchanged text immediately and asks before discarding unsaved changes.
While the editor is open, `m`, `q`, `?` and `1`–`3` type text instead of navigating or quitting;
Ctrl-C quits the app only when nothing is unsaved. Missing repository notes open empty at `repos/<org>__<name>.md`.

`Ctrl-E` opens the file in `$VISUAL`, then `$EDITOR`, then `vi` (arguments are supported).
Unsaved text refuses this action with "save or close first". The external editor receives a private
temporary copy, never the live file. Only the Textual main loop blocks while the terminal is suspended;
the Runner continues. Textual redraws the screen after the editor exits. Exit 0 must pass validation.
Both editors require valid settings, non-empty UTF-8 rules/notes, and valid template placeholders.
Only then is the copy atomically installed with mode 0600. A concurrent disk edit is refused with
"file changed on disk; reopen the editor". External-editor failure keeps the original and a private
rejected copy (0600), whose path appears in the error; built-in failures keep the buffer on screen.

Form saves reject stale settings, validate a private candidate and atomically replace `settings.toml`
with mode 0600. Each save patches one repository, one agent/comment field, or the `[screen]` table through the settings API.
The profile string is preserved as typed; relative paths resolve against the config directory only for
the engine and Agent label. TOML comments are discarded by form saves, which add this first line:
`# written by lightkeeper; comments are not kept`.
Editor saves preserve the file's text and comments. Unchanged forms skip the write; symlinked
settings and notes cannot be replaced. Both app write paths hold an advisory `fcntl.flock` on
`<config>/.write-lock` (0600) around the final comparison and replacement. The digest/content check
covers changes up to that comparison. An external editor saving in that same instant does not take
this lock and is not protected: last-writer-wins.

Screen reads have no filesystem side effects. Permissions (0600 files, 0700 directories) are enforced
at migration, startup and app writes. Panels cache settings by digest and mtime, without reloading
notes on each poll. Disk reads, serialization, validation, fsync and replacement use one serialized
worker queue; the external process alone hops to the main thread for suspension. Completion and errors
return to the footer or editor message line. Pending repository checks are shared and
forms stop waiting after 30 seconds with `check result lost; retry`. `?` opens help outside the editor.

PR detail shows the latest body and its outcome, the last 20 journal events for that revision, and a private
reason labelled "private, journal only". Session files load in a worker and tolerate missing files; session
paths must resolve under the state directory. All text is literal and all action keys stop while an Input
has focus; editor text also takes precedence over app navigation. Re-review clears only the contextual
settlement and cached assessment; approvals, unknown writes, closed PRs, active reviews, dismissals and
owner takeovers are protected. The Runner rechecks the confirmed
head. An existing automated action on that head reports "already" without posting twice.

Expanded alerts show cause, scope, age, last attempt/result and their named check. Engine probes preserve
quota/transport pauses. Repository checks cache access/default-branch results and release that repository
from quarantine on success. Unknown writes list affected items and are reconciled on the next tick; there
is no acknowledge action. The UI reads snapshots and session files; Runner commands own recovery actions.

If the screen looks stuck: from another terminal run `kill -USR1 <pid>`; use the latest `pid=` banner
in `run.log`; all thread stacks land in `hang.log` next to it. Shutdown still waits for any active write to finish.
