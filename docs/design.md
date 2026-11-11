# lightkeeper — design

Status: 0.1.0. Legacy answer schema and v1 marker reader retained for journals written by earlier versions.

## 1. Idea

**The script finds requests and collects full context; a model decides by a human-written rules file;
the script posts.** No path globs, author lists or size ladders; repositories are explicitly configured. A teammate copies the tool, writes rules in plain language, and is done.

## 1a. Mechanics kept deliberately simple

- Runs locally as a foreground process (`lightkeeper`), uses the owner's `gh` login; no server, webhook,
  GitHub App, bot account, personal access token or Actions cron. Polls every 60 s.
- One append-only JSONL journal is both state and ledger. One decision per PR head SHA or per
  (run, attempt, environment). State is re-checked immediately before every write.
- One folder per model call under `sessions/`: `prompt.md`, `stream.jsonl`, `stderr.txt`, `meta.json`, `decision.json`, `files.tsv`. Never overwritten.
- `dry-run` mode journals and notifies what it would do and writes nothing.
- Shared by clone: configuration is outside the checkout; legacy `policy.toml` stays gitignored.

## 2. Settings outside the checkout

Resolve `--config-dir`, then `LIGHTKEEPER_CONFIG`, then `~/.config/lightkeeper`.
`settings.py` validates `settings.toml`: identity login; Claude engine/model/effort/profile/timeout/budget;
comment language/disclosure; each repository's enabled/start/post/allowed/remarks/deployments.
Defaults and a complete example are in README. Only `start = "request"` and `engine = "claude"` are supported today.
Fresh installs without a legacy policy have no repositories and a persistent warning. `--check-repo org/name`
checks access, archived state and default branch through `gh api`; it makes no journal or model calls.

If settings are missing and `--policy PATH` is given, migrate that legacy policy, including its configured rules
path; an unfinished migration resumes from the source recorded in `migration.json`. Resume incomplete manifests, never overwrite a conflicting destination, preserve sources,
and print/log the report. Under the state lock, replay the journal, then append one `rehash` mapping from the
manifest's frozen semantics-5 legacy hash to current per-repository judgements and publish `rehash.json` last.
A crash after the append recovers the original mapping rather than appending another. Metadata changes neither
item history nor unknown/closed/forever-final state; contextual finality still checks mode and fingerprint.
Rules live only in the configuration directory (`rules.md`); the package ships a starting point in `lightkeeper/templates/rules.md`.

Reload between ticks. Invalid settings produce an error alert and retain the last good settings; startup errors
exit 2. The login is fixed for a process. The global mode ceiling applies the state-path and CLI rules;
repository `post` supplies the effective mode beneath it. Always hold the state lock, and take the fixed per-login
live lock when any enabled repository can post live. A reload cannot enable live posting without that lock.

## 3. Rules and judgement

`rules.md` is shared verbatim; optional `repos/<org>__<name>.md` is appended under `# Repository notes`.
The code-owned frames and answer schemas are frozen inputs of the judgement hash. The twelve-character judgement hash covers
both current frames/schemas, shared rules, repository notes and remarks. Component digests accompany journal
records, so reloads and replay can explain rules/notes/remarks changes. Engine, model, profile, timeout, budget, allowed
flags, enabled, post, deployment switch, comment settings, template and repository list are outside this hash.
`template.md`, comment presentation and remarks severity filtering act on the structured answer.
The legacy v1 marker abbreviated the judgement to six characters; the journal's `policy` field stores the full
judgement, and readers of both marker forms remain supported.

## 4. What the code decides (invariants), what the model decides (rules)

Code-raised conditions always alert; parsed model notification text alerts only with user-level `[alerts] reviews = true`, and otherwise remains a plain journal/detail note.

Code, before the model sees anything (`policy.evaluate_pr`):
- PR: repository in `repos`; open; not the owner's own; the owner's own approval at head → `satisfied`;
  the owner's own manual review (approve/request changes without the marker) → `ignore` (human took over);
  dismissed automated review on this head → `ignore`; draft → `defer`; checks GitHub requires for this PR
  still running → `defer` (after 3 h one notification, still no approval). Optional checks are shown to the model as running or
  not required and do not defer review; any failed check, including optional ones, still blocks approve.
  Required flags and base-branch declarations come from one extra GraphQL query per full fetch with a PR
  node ID. Missing declared contexts become pending checks; failed lookups use the old all-checks gate
  and log once per repository, PR and head. Required flags are excluded from the fingerprint.
  An incomplete file list reaches the model with its fetched/expected counts. Missing patches (binary or
  too large) are marked in the overview and on disk.
- Deployment: `approval_requested` WorkflowRun notifications name the repository and update time;
  a run URL is optional. Fresh requests trigger one `actions/runs?status=waiting&per_page=50` listing
  per enabled repository with `deployments = true`, then the existing pending-environment path checks
  `current_user_can_approve`. Settled runs are remembered; journaled pending gates keep being checked
  without fresh notifications. Failed discovery retries; there is no sweep of every repository.
  Jobs still running → `defer`.
- Model output must match the contract; anything else is `unclear`: settled for that input, owner notified.
  A turn/budget limit, or a timeout after any tool call, is `unclear` too: settled for that input, journaled,
  alerted and notified for PRs and deployments, with the bound and opened/offered file counts. A timeout
  before any tool call remains a transport failure and retries under the normal model pause policy.
- PR `approve` floors: bot author, failed check, patch omitted by GitHub (except for a removed file), incomplete file list →
  `nothing` + notification. A denied tool call refuses approval for both PRs and deployments, with the
  first denial in the private reason, journal and notification. Unopened patches are the model's judgement.
  Fresh PR floors are checked again before posting; other verdicts are unaffected by denied calls.
  An empty Git blob counts as visible without a patch, including a removed empty file; binary and
  oversized files without patches remain incomplete. Blob SHAs do not enter the context fingerprint.
- Removed as reviewer before the tool ever reviewed → wait (the author handed it to someone else; a
  re-request resumes). Removed after the tool reviewed (GitHub does that on every submitted review) → still
  watched. Any manual review by the owner, a comment included, ends the tool's involvement with that PR.

Model, with `rules.md` and repository notes:
- PR: description, author, base/head, every check with link, complete collected failed job logs (the same
  at-most-5 job requests as before), all collected reviews and comments, file list, full diff and head tree →
  `{"action": "approve|request_changes|comment|nothing", "why", "watch", "summary", "findings", "reason", "notify"}`.
  The public summary is capped at 600 characters; at most 12 findings have severity, origin and up to
  500 characters of text, plus optional path/URL. The prompt states the repository's remarks level and
  asks for an honest verdict regardless of posting permissions. The reason and optional notification are private.
- Deployment: `{"decision": "approve|ignore", "summary", "reason"}`; summary is public, reason private.

Code, after the model (`Runner._write_pr`): re-fetch; same head and fingerprint (base, title, body, draft,
checks with their job links, files, other people's reviews) or `defer`; re-run eligibility; an automated
review with the same action already on this head → `applied` without posting. Reload and validate settings
immediately before POST: repository enabled, action allowed, deployment switch on for gates, assessed judgement
unchanged, effective mode live, live lock held and current GitHub identity matching. Identity is also checked
before every tick: lookup failures retry twice (1 s then 3 s pauses, 15 s timeout per attempt); exhausted
checks skip the tick and defer writes. Three consecutive failed checks raise an error alert; until then the header shows the
last known login as unconfirmed. Any successful lookup resets the failure streak; a mismatch stops and
alerts immediately without retry, while a matching login clears the alert. Disable/stale/invalid cases
defer; disallowed actions journal `withheld` with the proposal body, notification and reason. The assessment stays
available across defer/withheld and replay. A later tick rechecks eligibility, context and judgement and re-settles
without a model call when only authority changed. No alternate action is substituted. Then one
`POST /pulls/{n}/reviews` with the event, the assessed `commit_id`, and the code-rendered body. Rendering
filters findings by severity, expands the template with public summary/findings/optional disclosure,
caps public text at 4,000 characters and appends the hidden v2 trace as the last line. A comment or changes
request without retained public rationale is withheld. Deployments use only their public summary plus trace.
The trace identifies head, judgement and answering engine; both v1 visible markers and v2 traces remain
recognised. A human merely mentioning the tool is still a human. Journal outcomes retain the exact rendered
body, published finding count, summary and private reason; sessions also save `rendered.md` for live/dry writes.
`request_changes` is superseded by the next
head's review in the owner's name; when the next head is not approved and the block still stands, the
owner is told once. A 4xx on the POST is `error` (settled for this context, owner notified); a 5xx or a
transport failure is `unknown` and reconciled against GitHub before anything else runs.

Reconciliation fetches the pull request and its reviews. A review by the user on the recorded head with the
wanted state, or dismissed since, means the write happened (`satisfied`, `applied` or `owner_approve`). No such
review ten minutes after the record (`UNKNOWN_WRITE_GRACE_S`: a review can appear seconds after a 502, never
later) means it was lost: `lost` settles the original judgement, mode and fingerprint, clears the unknown set
and the alert, and claims neither an approval nor owner takeover. A failed fetch stays unknown and is retried.
A 0.1.10 reader ignores `lost` and keeps its unknown entry and alert. Deployment approvals are not scoped to a
run attempt, so an unknown one still needs a human.

## 5. Discovery and finality

- Between full ticks, one light GraphQL query per batch of up to 50 watched PRs reads head/state/draft,
  author/title and up to 100 reported check contexts per PR, including names and run timestamps. Both
  parsers share latest-run deduplication; unreported declared checks stay outside light-status counts.
  Truncated contexts skip only that PR with one warning per head; a missing rollup means zero checks.
  Pushes, state/draft changes, all checks finishing or a changed failure count can wake a full tick.
- Candidates each tick: `gh search prs review-requested:<login> --repo … --state open` (personal and
  team requests) ∪ every PR in the journal without a `closed` event (watched). A watched PR is forgotten
  when seen closed or merged; reopened, it is found again by search. A search failure does not stop the
  watched PRs from running.
- Finality: `applied` approve and `satisfied` forever; `nothing`, `ignore`, `unclear`, `error`, `lost`, `would_*`
  and `applied` comment/request_changes per (repository judgement, effective mode, context fingerprint). Unknown writes are
  reconciled from GitHub before anything else runs.
- Any exception on one item is logged and counted; the tick goes on. A 403 that is a rate limit backs off
  instead of quarantining the repository.
- Notifications are journal events (`notify`, with a tag for one-shot ones) and `--status` lists the last
  20; macOS `display notification` is the delivery, never the record.

## 6. Workspace and model invocation

Every review gets a fresh 0700 directory of UTF-8 0600 material files. PRs have `request.md` (identity/title),
`description.md`, `files.tsv`, `diff/0001.patch` onwards, `checks.md`, `reviews.md`, `comments.md`, `logs/01.log`
onwards and `tree.txt`. TSV columns are number, status, additions, deletions, patch bytes, generated diff
name, original path and previous path. Paths are escaped data, never filesystem names; each patch starts
with `path: <path>`. Missing patches carry `(patch omitted by GitHub: binary or too large)`.
The tree uses one recursive GitHub request at the head; truncation is marked on its first line. Timeouts and
definite 4xx omit the file with a reason; rate limits and 5xx propagate to retry next tick.

Deployments have `request.md`, `workflow.yml`, `jobs.tsv` (id/name/status/conclusion/log bytes/log file),
complete `logs/01.log` onwards, and `compare/files.tsv` plus `compare/diff/0001.patch` onwards.
The first overview section, Material on disk, lists files/directories with byte sizes and file counts,
the turn/time allowance and collection problems (tree unavailable, incomplete list, missing comparison).
The sections keep their order. Each is inlined whole while the request stays within `INLINE_MAX_BYTES =
200_000` UTF-8 bytes; otherwise its body is `(in <file or directory>: <n> bytes)`. Space for subsequent
pointers is reserved. The PR log tail and deployment `excerpt_log` remain inline conveniences; disk logs
are complete. There is no pre-call size cap and no size rule selecting which material matters.

`claude -p --model <model> --effort <effort> --output-format stream-json --verbose --json-schema <contract>
--tools "Read,Grep,Glob" --strict-mcp-config --mcp-config '{"mcpServers":{}}' --setting-sources project
--permission-mode dontAsk --append-system-prompt-file <0600 file in a separate private directory>
--no-session-persistence --max-turns 60 --max-budget-usd <agent.budget_usd>` runs from that workspace.
The system prompt is never material. Only `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `TMPDIR`, `LANG`,
the profile (`CLAUDE_CONFIG_DIR`) and model credential (`ANTHROPIC_API_KEY`) pass through; no GitHub token.
`--setting-sources project` keeps profile hooks and plugins out while OAuth keeps working. Built-in file
tools are confined by the CLI's permission mode, not a broker or a kernel sandbox.

The last stream `result` uses the existing envelope parser. Successful Read results produce distinct
workspace-relative opened paths; attempted reads alone do not. Permission denials from system events and
the result envelope are deduplicated into one-line tool/argument records. Private journal records add
`opened`, `offered`, `denied`, `turns`, `cost_usd`, with denied calls retained for the approval floor and replay.
Sessions keep the raw stream and file index, not separate patch files; old `stdout.json` sessions remain
readable. PR detail shows opened/offered counts. No coverage sentence is added to public comments.

Frame 4 hashes the new instructions. Frame 3 is frozen in `_frame_v3.py`; the frame-2 alias targets that
frozen hash, never frame 4. No frame-3 alias exists: open contextual outcomes, including cap skips, get
reviewed again once. Applied approvals, satisfied and owner approvals remain final. Turn, budget and time
bounds live outside hashed text; their tuning does not reopen outcomes. Defaults: 60 turns, $5, 900 seconds.

## 7. Verification

Run `python3 -m unittest discover -s tests`. Integration tests use real scratch settings and fake GitHub/model
calls for migration, rehash, reload, pre-write races, withheld retries, per-repository modes and CLI admission checks.
The existing eligibility, fingerprint, identity, lock, unknown-write, engine isolation and stdin tests remain active.
No external model or live GitHub write is needed for this suite.
