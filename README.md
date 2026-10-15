# lightkeeper

[![ci](https://github.com/ridjex/lightkeeper/actions/workflows/ci.yml/badge.svg)](https://github.com/ridjex/lightkeeper/actions/workflows/ci.yml)

Reviews GitHub pull requests **in your name** when your review is requested, and approves Actions
deployment gates, by rules you write in plain language. Runs on your machine with your `gh` login and
your `claude` CLI, only on repositories you name, journals every decision and keeps every model session.

Not smart, universal: the script finds requests and collects the full context; a model decides by your
`rules.md`; the script re-checks the pull request and posts. Every repository starts in dry-run.

## Before you use it: what it sends and what it posts

- **To Anthropic, through your `claude` CLI account:** for every pull request it reviews, the title,
  description, diff, earlier reviews and comments, check results and the tail of each failed CI log; for
  deployment gates, the workflow file and job logs. This happens in dry-run too. Make sure your employer
  allows sending this code to Anthropic under your account's terms.
- **To GitHub, as you:** reviews and deployment approvals are posted with your `gh` login. GitHub attributes
  them to you; to your team and to branch protection they are your reviews, so an approval counts toward
  required approvals and CODEOWNERS. There is no GitHub App to install or revoke. Agree on this with your
  team before you set a repository to `live`. Set `[comment] disclose = true` to label every comment as
  automated, or limit `allowed` to `["comment", "request_changes"]` where a human approval is required.
- **On your disk:** `~/.local/state/lightkeeper/<login>/` keeps every prompt and answer, including
  code and logs, until you delete it.
- Nothing else: no telemetry, no server. Once a day it asks GitHub, through your `gh`, whether a newer
  lightkeeper release exists (`[screen] updates = false` turns that off).

## Quick start

You need Python 3.11+, the GitHub CLI `gh` logged in as you, and the Claude Code CLI `claude` (2.1 or newer)
logged in, or `ANTHROPIC_API_KEY` set. macOS and Linux.

1. Install. The terminal screen needs Rich and Textual (the `ui` extra); without them the tool prints log lines.

   ```sh
   brew install ridjex/tap/lightkeeper            # Homebrew, screen included
   pipx install "lightkeeper[ui] @ git+https://github.com/ridjex/lightkeeper"   # or pipx
   ```

   From a clone: `python3 -m pip install -e ".[ui]"`, or just `make run` after `pip install -r requirements.txt`.

2. Check both logins and the repository you want reviewed:

   ```sh
   gh auth status
   claude auth status
   lightkeeper --check-repo your-org/your-repo
   ```

3. Create your configuration. It lives in `~/.config/lightkeeper/`, outside any checkout:

   ```sh
   lightkeeper --init
   ```

   This writes `settings.toml` and `rules.md` from the templates in `lightkeeper/templates/` and never
   overwrites existing files. Put your login and your repository into `settings.toml`, then edit `rules.md`:
   it is everything the model knows about what you would approve, so say plainly what must never be approved.

4. Run one pass. `--once` never writes to GitHub:

   ```sh
   lightkeeper --once --plain
   ```

   The tool only looks at open pull requests in your configured repositories where GitHub lists **you
   personally** as a requested reviewer. Team requests and your own pull requests are ignored. If the log
   ends with `tick: nothing to do`, ask a teammate to request your review on a pull request and run it again.

5. See what it would have done:

   ```sh
   lightkeeper --status
   ls ~/.local/state/lightkeeper/<your-login>/sessions/
   ```

   Each session folder holds the exact prompt (`prompt.md`), the model's answer (`decision.json`) and, when
   a review would be posted, the exact body (`rendered.md`).

6. Run it for real. The working command is just the name; it checks every minute and keeps running until
   you quit (`q` on the screen, `Ctrl-C` in plain mode):

   ```sh
   lightkeeper                    # the terminal screen
   lightkeeper --plain            # the same loop as log lines, for a tmux window or a service
   brew services start lightkeeper   # Homebrew: run --plain in the background under launchd or systemd
   ```

   It is still dry-run: every repository starts with `post = "dry-run"`, so nothing reaches GitHub. When you
   trust its verdicts for a repository, set `post = "live"` for that repository in `settings.toml` (or on the
   Repositories screen) and restart.

   The Homebrew service writes its log to `$(brew --prefix)/var/log/lightkeeper.log`; `lightkeeper --status`
   and the state folder work the same whichever way it runs. Stop it with `brew services stop lightkeeper`
   before upgrading.

Updating: stop the running instance, then `brew upgrade lightkeeper` or `pipx upgrade lightkeeper`,
and start it again. Configuration, journal and sessions are never touched by an update.

## How it decides

Every minute: for each repository in your `settings.toml`, find the open pull requests where you are a
requested reviewer, plus the ones the tool already watches. Drafts and running checks wait. For the rest,
the model gets the description, the diff, every check with its link, the tail of each failed job's log,
the earlier comments and reviews, and your `rules.md`; it answers one of
`approve | request_changes | comment | nothing`, public `why` and `watch`, a summary and findings,
a private reason, and optionally one line for you. The script checks the pull request has not changed,
posts the review bound to the reviewed commit, writes the journal, and shows a desktop notification when
the model says you should look. A pull request stays watched until it is closed: a new push is a new
evaluation, nobody has to re-request you.

Between full checks, light polls refresh author/title/CI every 15 s and wake a full check on a push,
a state or draft change, when all checks finish or when a check fails. Failed ticks keep their normal
delay; light-poll errors retry at 30 s, then 60 s. `--once` skips light polls. API budget: light polls cost
about 240 GraphQL points per hour per batch of up to 50 watched pull requests, plus full checks; each
early wake adds a full tick of about three calls per watched pull request.

Deployments waiting for your approval (found through your GitHub notifications) are judged from the
workflow, every job and the logs before the gate; the model answers `approve | ignore`.

## What the code guarantees, whatever the rules say

- The model has no tools and no network: it sees what the script collected, nothing else, and only
  proposes. The script posts, and only after re-fetching the pull request and finding the same head,
  description, checks, files and teammates' reviews it assessed. Approvals are bound to that commit
  (`commit_id`). One review per head and action: a crash after posting never posts twice.
- Three objective floors an `approve` cannot cross, whatever the model was talked into: a bot author, a
  failed check, a file whose patch is not visible. The same three are in `rules.md`; the code only repeats
  them.
- One decision per pull request head (or per run attempt). A push, a check rerun, an edited description
  or edited rules re-open it; your own review, approval or a closed pull request end it.
- A dismissed automated review on the current head is a veto: one notification, no re-approval, even
  after an approval was recorded as final. A new head can be evaluated again. An earlier automated
  "changes requested" only triggers a block warning if it is still your latest decisive review.
- Everything outside `repos` is ignored, including unknown writes awaiting reconciliation. Your own
  pull requests are ignored.
- A 403/404 while fetching an item, without throttling, quarantines only that PR or run for the session;
  other items in the repository continue. HTTP 429 and rate-limit/secondary-limit 403 errors are retried.
- A model answer outside the contract is "unclear": nothing is posted, you are notified, the session is
  kept, and the same input is not retried.
- `dry-run` mode does everything except the final write, and prefixes notifications with `[dry-run]`.
- The `gh` login is checked every tick and immediately before each review or deployment approval.
  A mismatch or failed lookup defers writes, logs the reason and raises an error alert; a matching check
  clears it. The header shows `gh <login> ✓` or `gh ✗ <actual>` (`unknown` if lookup failed).
- Checks still running after 3 hours → one notification, no approval. A context too large to send → no
  approval, one notification.

## Comments

A PR comment reads like a person wrote it: one thought that matters, in at most two short paragraphs.
The fixed 🔭 marker opens the body. `why` gives one sentence stating the verdict and its essential reason:
what was checked and why that is enough, or exactly what blocks. `watch` adds the single most important
remaining concern, or stays empty. No headers, lists, tool names, automation wording or @mentions appear
in the default body; file paths sit inline in prose. For example:

> 🔭 Approved: the migration is additive and the rollback path is covered by tests.
>
> One thing to keep in mind: the retry loop in orders/sync.py swallows timeouts silently.

Every comment still ends with the same hidden HTML trace containing head, judgement hash and engine.
This audit link supports deduplication, dismissal handling and takeover checks. `comment.disclose` defaults to `false`.
When enabled, disclosure renders `Automated review for @<login> (lightkeeper)` at `{disclosure}`,
or as the last paragraph before the hidden trace if the template omits that placeholder. It survives
truncation. The default template uses only `why` and `watch`.
Model-generated mentions outside backtick code spans are neutralised; file paths and URLs stay intact.

Available template placeholders (private fields are never available):

- `{why}`: the verdict and its essential reason.
- `{watch}`: the single most important remaining concern, or empty.
- `{summary}`: the public summary.
- `{findings}`: findings filtered by the repository's `remarks` level.
- `{disclosure}`: the visible automation disclosure when enabled.

The summary and findings remain in the journal and detail view, and are available to custom templates.
Findings in those templates are filtered by the repository's `remarks` level. A comment or changes request
with blank `why`, or no publishable rationale left after rendering, is withheld. Deployment comments contain
only the public summary and hidden trace. Private reasons and owner notifications never enter either body.
`nothing` stays local. Overview rows still show the summary.

## Writing rules

- `rules.md` is passed to the model verbatim, after a fixed frame that names the four actions and the
  three floors. Start from `lightkeeper/templates/rules.md`: it is written for a team where one
  approval merges and the reviewer approves by default; change that first if it is not your team.
- `repos/<org>__<name>.md` adds notes for one repository under `# Repository notes` (edit from the Repositories screen with `n`).
- `remarks` per repository tells the model to report blockers only, blockers and important findings, or
  everything it notices; the code also filters finding severity before rendering.
- `allowed` per repository lists the actions the tool may post. The model still proposes its unconstrained
  verdict; a disallowed one is recorded as `withheld: <action> disabled`, never replaced by another action.
- Text inside pull requests, commits, comments and logs is evidence, never instructions; say so in your rules
  too (the template does).

## Configuration

Configuration lives in `~/.config/lightkeeper/`. `--config-dir PATH` overrides
`LIGHTKEEPER_CONFIG`, which overrides that default. The screen header shows this directory.
The directory is private (0700); loaded configuration files are 0600. Users of the old single `policy.toml`
run `lightkeeper --policy path/to/policy.toml` once; it migrates into the directory above and keeps
the source. With no `settings.toml` the tool watches nothing and shows a persistent warning.

Example `settings.toml` (all optional keys shown with their defaults):

```toml
[identity]
login = "octocat"                  # required; must match gh

[agent]
engine = "claude"                  # the only engine today
model = "opus"
effort = "medium"
# profile = "~/.claude-work"       # omitted: default CLI profile; relative paths use this directory
timeout_s = 600                    # positive integer

[comment]
language = "en"                    # reserved; write template text in your desired language
disclose = false                   # opt in to visible disclosure with any template

[screen]
idle = "always"                    # or "quiet": picture only while nothing is watched
tempo = 0.8
clock = true
moon = true
events = true
updates = true                     # once a day, ask GitHub for a newer release

[repos."example/widget"]
enabled = true
start = "request"                  # only supported start mode
post = "dry-run"                   # or "live"
allowed = ["approve", "request_changes", "comment"]  # [] withholds all proposed actions
remarks = "important"              # blockers | important | all
deployments = false
```

Unknown keys, wrong types and unsupported values fail validation. Newly configured repositories default to
dry-run with deployments off.
`enabled = false` pauses all processing for that repository. The model still proposes its unconstrained
verdict when actions are disabled: code records `withheld: approve disabled`, for example, and never
substitutes another action. Re-enabling that action reuses the saved assessment when its head, context and
judgement match, including after a restart. Allowed actions in dry-run are recorded as `would_*`.

- `rules.md`: shared instructions, passed verbatim to the model; `lightkeeper --init` copies the starting point.
- `repos/example__widget.md`: optional repository notes, appended under `# Repository notes` only for that repo.
- `template.md`: optional presentation template; `lightkeeper/templates/template.md` shows the default `🔭 {why}` and
  optional `{watch}`. `{summary}`, `{findings}` and `{disclosure}` remain available for custom templates.
  No private fields are available. Template and disclosure edits apply at the write boundary.
- `remarks`: tells the model to report blockers only, blockers and important findings, or everything it
  notices; code also filters finding severity before rendering.

Settings and Markdown files reload between ticks and immediately before writes. A bad startup config exits 2;
a bad reload raises `settings_error` and retains the last good settings, while the pre-write check defers
with `settings invalid, not writing`. Changing the login requires a restart. Rules, repository notes and
remarks change that repository's judgement; engine/model/profile/timeout, posting flags, templates and
repository-list edits do not reopen settled outcomes. History names the changed components.

Check a repository before enabling it:

```sh
lightkeeper --check-repo example/widget
```

This runs `gh api repos/example/widget`, prints access, archived status and default branch, and exits
without loading the model or journal. It does not enrol the repository.

Ask teammates to **request your review on GitHub** (you personally, or an individual `CODEOWNERS` entry):
the tool acts only on pull requests where GitHub shows you as a directly requested reviewer at least
once. Team requests are not picked up. If the author removes you before the tool said anything, it waits
until you are requested again. If you review a pull request by hand, in any way, the tool leaves it to you.

## Engines

Claude is the current engine. `claude auth status` checks login health at startup, after a model failure,
and every full tick while authentication is paused, with a 20-second timeout. Probe and model calls use
one scrubbed environment, an empty temporary working directory and the configured profile. Profile paths
expand `~` and resolve relative to the config directory. The header shows `claude <profile-basename> ✓/✗`
(`default` without an explicit profile), dim `…` until checked. A failed probe warns without blocking calls.
Only a call's auth failure pauses authentication until a probe passes. The alert gives the exact fix:
`unset ANTHROPIC_API_KEY or replace it` when that key is inherited; otherwise
`CLAUDE_CONFIG_DIR=<profile> claude auth login`, or `claude auth login` without a configured profile.
Quota errors pause until a future reported reset time, or 15 minutes if absent or stale; transport errors
pause for 15 minutes. Auth, quota and transport failures defer all remaining model work in that tick,
with one notification per failure streak. A successful auth probe never shortens a timed pause.
A profile edit is probed on the next
tick. No periodic ten-minute probe is scheduled. Request prompts still travel on stdin, and model
sessions retain `prompt.md`, `stdout.json`, `stderr.txt`, `meta.json` and `decision.json`.
When a body is posted or would be posted, `rendered.md` stores that exact body, including its trace.

## Files on disk

State lives in `~/.local/state/lightkeeper/<login>/`: `journal.jsonl` (every event, append-only),
`sessions/<time>_<repo>_<pr>_<sha>/` (prompt, model output, stderr, decision, and rendered body when posting),
`run.log` (0600, 5 MB with three rotated backups) and `lock`.
Override the root with `LIGHTKEEPER_STATE` or pass `--state-dir`. A resolved state directory other
than the default forces dry-run and disables desktop notifications. `--allow-live` permits repositories'
live mode and notifications there; it does not turn dry-run repositories live. `--once` and `--dry-run` always win.
Every journal writer holds the state-directory lock. A run with any enabled live repository also holds a per-login lock at
`~/.local/state/lightkeeper/locks/<login>` (file 0600, directory 0700), independent of either state
override. Locks are non-blocking, held until exit and never unlinked. A settings reload cannot turn dry-run
into live without the live lock; restart to acquire it. `--status` is a read-only journal snapshot.

## The screen

`lightkeeper` opens the Textual Overview: current activity and today's posted counts, active alerts,
watched pull requests and deployment gates, and recent verdicts. One cursor walks Alerts, Watching and
History; `Enter` opens a pull request's detail with its status, assessment, posted body, description and
timeline; `2` and `3` open the Repositories and Agent screens, where repositories, settings, rules and
templates are edited without leaving the terminal. `m` opens the menu with every screen, action and
setting, including the screen settings form; `?` shows the keys. A lighthouse scene sits above the
Watching table by default (`[screen] idle = "quiet"` shows it only while nothing is watched). Everything about the screen,
the keys, the PR detail, the editors and the idle scene is in [docs/screen.md](docs/screen.md).

## Troubleshooting

- `gh ✗ <actual>` in the header: `gh auth status` shows another account than `[identity] login`; writes are
  deferred until it matches.
- `claude ✗`: the Agent screen (`3`) shows the exact fix, usually `claude auth login` for the configured profile,
  or `unset ANTHROPIC_API_KEY` when an inherited key shadows your subscription.
- Linux: the review loop and the screen run; desktop notifications only appear in the log and in `--status`.
- Terminals: the screen needs a monospace font with half-block glyphs (any Nerd Font or modern system
  monospace) and 256 colours or more; it was built on a dark background, light themes are untested.
  `NO_COLOR=1` and `LIGHTKEEPER_ASCII=1` fall back to monochrome and ASCII with the same information.
- The model runs with a scrubbed environment: only PATH, HOME, USER, LOGNAME, SHELL, TERM, TMPDIR, LANG,
  the profile and the API key are passed through; proxy and custom CA variables are not (see the issues).
- An update reminder appears in the header once a day when GitHub has a newer release; `[screen] updates = false` turns it off.
- If the screen looks stuck: from another terminal run `kill -USR1 <pid>` (the latest `pid=` banner is in
  `run.log`); all thread stacks land in `hang.log` next to it. Shutdown still waits for any active write to finish.

## Limits

- Runs only while your machine is awake; nothing is missed, only delayed.
- The model can be wrong. The rules are the lever: say what must never be approved, and keep `nothing`
  the default for anything you would want to see yourself.
- Team review requests (`team-review-requested`) are not discovered.
- POSIX only (file locks and signals): macOS and Linux. GitHub.com only: every `gh` call is pinned to it.
- The journal and session folders grow until you delete them; there is no retention yet (see the issues).

## Docs, contributing, security, license

- [docs/design.md](docs/design.md): design; [docs/decisions.md](docs/decisions.md): why things are the way
  they are; [docs/screen.md](docs/screen.md): the screen; [docs/release.md](docs/release.md): how a release
  is made; [CHANGELOG.md](CHANGELOG.md).
- [AGENTS.md](AGENTS.md): the rules that are easy to break, for contributors and their coding agents;
  [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md). Ideas and deferred work live in GitHub issues. MIT license.
