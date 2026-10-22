# Decisions

Why things are the way they are. Newest first. Entries are short on purpose; the reasoning behind a change
belongs in its commit and pull request.

- 2026-10-23 · **One check per name, latest run wins:** a superseded run is not a verdict; use startedAt, then completedAt, then list order.

- 2026-10-21 · **Missing declared checks wait; failed lookups use all checks.** Branch declarations cover checks that have not reported, while a once-per-head warning and the old all-checks gate keep lookup failures from blocking owner and recovery paths.

- 2026-10-21 · **Wait for required checks only**, because GitHub's own readiness is the owner's declared policy; all three approval floors stay unchanged, including failed optional checks.

- 2026-10-21 · **Needs-you is an alert, not a counter.** An item waiting for a human is the one thing the screen exists to show; a small counter and a row mark were overlooked for hours, an alert line, a reverse badge and an amber beam are not.

- 2026-10-15 · **Pace idle frames by timer lateness instead of a fixed lower rate** so fast terminals keep smooth motion while busy terminals skip frames and recover gradually; scene clocks never slow down.
- 2026-10-15 · **Identity lookup failures retry and alert only after three failed checks** to avoid transient network noise while still blocking writes; a confirmed account mismatch stops and alerts at once.
- 2026-10-15 · **Disclosure is on by default.** A public tool used by strangers in any jurisdiction should label automated comments unless its user decides otherwise; the first-person posting stays, the label is one line.
- 2026-10-15 · **Template files that reach the model contain only what the model should read.** Notes to the person and alternatives to choose from live in README and the `--init` output; a chooser left in the file would hand the model two contradicting postures.
- 2026-10-15 · **The settings template writes only what the user must decide** (login, repository, post); everything else is commented out so an omitted key follows the installed version's default and a release can change defaults without touching anyone's file.
- 2026-10-15 · **The built-in editor is the default** so editing configuration needs no terminal-editor knowledge; Ctrl-E keeps the external editor available.
- 2026-10-15 · **App.suspend runs only on the main thread** so Textual cannot render over the external editor while the Runner continues independently.
- 2026-10-15 · **The prompt frame was edited once more before freezing** (grammar, no former-name clause) and the system prompt now travels in a private file, review bodies as JSON on stdin; neither appears in process arguments. Existing journals re-open their settled outcomes once; approved heads stay protected.

- 2026-10-14 · **Menu, screen settings and the update reminder.** `m` opens a menu with every screen, action and setting on every screen; a Screen settings form patches only the `[screen]` table and applies at once; the Agent template key moved to `T`. Once a day the runner asks GitHub for the latest release through the user's `gh` and shows `update X.Y.Z` in the header; `[screen] updates = false` turns it off. The idle picture defaults to `always`. Audit fixes before release: session paths built only from validated parts and checked to stay under the state root, login validated as a GitHub login, Textual ≥ 8.2.7 and Rich ≥ 14.2, a failed journal append rolled back to its start, a stop on journals written by a newer version, umask 0077, control sequences stripped from plain output, migration resume trusting unchanged inputs, preview only in a terminal, every `gh` call pinned to github.com.
- 2026-10-14 · **Renamed to lightkeeper**, the lighthouse keeper who watches while you are away. Reviews posted
  under the former name `approval-delegate` stay recognised; the frozen legacy frame for old journals is unchanged.
- 2026-10-14 · **Enabled repositories require non-blank rules at every load**, so a lost `rules.md` never lets
  the built-in frame review alone. Why: two independent reviews found the model would otherwise run with no rules.
- 2026-10-14 · **Journal tail repair is per record and never on read-only replay.** A torn last line is
  truncated; a valid last record without its newline gets one before the next append.
- 2026-10-14 · **Every boundary is UTF-8**, whatever the locale: CLI pipes, persisted files, log streams.
- 2026-10-14 · **Legacy `policy.toml` migrates only with an explicit `--policy`**; an unfinished migration
  resumes from the source recorded in `migration.json`. Why: an installed command must not depend on its cwd.
- 2026-10-14 · **Textual runs without the kitty keyboard protocol.** Every binding has a plain encoding and
  some terminals keep the protocol on after a long run (Backspace then types `7u`).
- 2026-10-14 · **The idle picture is a deterministic big-pixel scene**: tempo 0.8, local-hour palette, real
  lunar phase, eight fixed repository stars, rare whale, ship and meteor events from a time-slot hash, a 4 ms
  frame budget at 200 columns. Depth order is distance: stars, disc, clouds, beam, events, rock and tower.
  Switches live in `[screen]`; `idle = "always"` is the default because the picture is part of the product.
- 2026-10-14 · **Comment shape: one thought, two short paragraphs at most.** `why` states the verdict and its
  essential reason, `watch` the single remaining concern; no headers, lists, tool names or @mentions. The
  hidden HTML trace at the end is the provenance; the visible disclosure is a setting.
- 2026-10-13 · **Owner approval is a Runner command**, confirmed from PR detail with the captured head; it is
  the user's manual review, never carries the model's reason or the trace, and ends automation on that PR.
- 2026-10-13 · **The screen only reads; the Runner writes.** Panels read snapshots and session files; form
  saves patch one table atomically under a lock; editors receive private copies validated before install.
- 2026-10-13 · **Textual is the only live screen; `--plain` is the fallback.** The Rich live screen was removed.
- 2026-10-13 · **Judgement hash per repository.** Finality depends only on what the model sees (frame, rules,
  that repository's notes, remarks level, answer schema). Engine, model, profile, timeout, template and the
  repository list never re-open settled outcomes.
- 2026-10-13 · **Pickiness and authority are two controls**: `remarks` (what to report) and `allowed` (what may
  be posted). A disallowed action is journaled as withheld, never replaced by another action.
- 2026-10-13 · **Engine adapter above the CLI** with typed failures (auth, quota, transport, input) and a probe;
  auth and quota failures pause with one notification per streak.
- 2026-10-13 · **Settings live in `~/.config/lightkeeper/`**, one strict TOML plus Markdown files, reloaded
  between ticks and before writes; a bad reload keeps the last good settings and defers writes.
- 2026-10-12 · **Hangs are diagnosable**: `kill -USR1` dumps every thread stack to `hang.log`; the screen
  never blocks on the runner.
- 2026-10-09 · **The model proposes, the script posts.** Approve means "may go to production as is", so the
  rules carry explicit exclusions; a pull request stays watched until closed; a dismissed automated review is a
  veto; your own review ends automation on that PR.
- 2026-10-09 · **The model gets no tools and no MCP servers**, only complete context; an incomplete diff or an
  over-sized context is never approved. Launch from an empty directory with a scrubbed environment; prompts on stdin.
- 2026-09-17 · **Approvals are bound to the head SHA** and re-checked against a fingerprint right before writing.
- 2026-09-17 · **Local foreground process on the user's own `gh` login; no PAT, no server, no Actions cron.**
  Why: approvals must come from the user's account, and nothing should be readable by other workflows.
- 2026-09-17 · **Discovery: pull requests by search, deployments by notifications.** Review-request
  notifications are overwritten by later events; deployment requests are their own threads.
- 2026-09-17 · **Universal, not smart.** The code owns eligibility and writes; a model decides every request by
  a human-written rules file. No path globs, author lists or size ladders.
