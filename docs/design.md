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
- One folder per model call under `sessions/`: prompt, stdout, stderr, meta, decision. Never overwritten.
- `dry-run` mode journals and notifies what it would do and writes nothing.
- Shared by clone: configuration is outside the checkout; legacy `policy.toml` stays gitignored.

## 2. Settings outside the checkout

Resolve `--config-dir`, then `LIGHTKEEPER_CONFIG`, then `~/.config/lightkeeper`.
`settings.py` validates `settings.toml`: identity login; Claude engine/model/effort/profile/timeout;
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
records, so reloads and replay can explain rules/notes/remarks changes. Engine, model, profile, timeout, allowed
flags, enabled, post, deployment switch, comment settings, template and repository list are outside this hash.
`template.md`, comment presentation and remarks severity filtering act on the structured answer.
The legacy v1 marker abbreviated the judgement to six characters; the journal's `policy` field stores the full
judgement, and readers of both marker forms remain supported.

## 4. What the code decides (invariants), what the model decides (rules)

Code, before the model sees anything (`policy.evaluate_pr`):
- PR: repository in `repos`; open; not the owner's own; the owner's own approval at head → `satisfied`;
  the owner's own manual review (approve/request changes without the marker) → `ignore` (human took over);
  dismissed automated review on this head → `ignore`; draft → `defer`; checks still running → `defer`
  (after 3 h one notification, still no approval); GitHub's changed-file count must equal the files
  fetched, else `nothing` + notification. A missing patch (binary or too large) is shown to the model as
  such; the rules say when that is acceptable.
- Deployment: unchanged from v2 (pending environment with `current_user_can_approve`, found via
  notifications `reason = approval_requested`, filtered by enabled repositories with `deployments = true`; jobs still running → `defer`).
- Context over the byte cap → `nothing` + notification, no call.
- Model output must match the contract; anything else is `unclear`: settled for that input, owner notified.
  The CLI giving up on one input (max turns, prompt too long) is `unclear` too, so one bad PR never pauses
  the rest. CLI failures (auth, timeout, crash) pause model calls for 15 minutes and `defer`.
- `approve` floor, checked by code after the model: bot author, failed check, patch not visible →
  `nothing` + notification. The rules say the same; the floor exists because the model reads untrusted text.
- Removed as reviewer before the tool ever reviewed → wait (the author handed it to someone else; a
  re-request resumes). Removed after the tool reviewed (GitHub does that on every submitted review) → still
  watched. Any manual review by the owner, a comment included, ends the tool's involvement with that PR.

Model, with `rules.md` and repository notes:
- PR: description, author, base/head, every check with link, tail of each failed job log (120 lines, at
  most 5 jobs), reviews and the last 12 comments, file list, full diff →
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

## 5. Discovery and finality

- Candidates each tick: `gh search prs user-review-requested:<login> --repo … --state open` (direct
  requests only) ∪ every PR in the journal without a `closed` event (watched). A watched PR is forgotten
  when seen closed or merged; reopened, it is found again by search. A search failure does not stop the
  watched PRs from running.
- Finality: `applied` approve and `satisfied` forever; `nothing`, `ignore`, `unclear`, `error`, `would_*`
  and `applied` comment/request_changes per (repository judgement, effective mode, context fingerprint). Unknown writes are
  reconciled from GitHub before anything else runs.
- Any exception on one item is logged and counted; the tick goes on. A 403 that is a rate limit backs off
  instead of quarantining the repository.
- Notifications are journal events (`notify`, with a tag for one-shot ones) and `--status` lists the last
  20; macOS `display notification` is the delivery, never the record.

## 6. Launch of the model

`claude -p --model <model> --effort <effort> --output-format json --json-schema <contract> --tools ""
--strict-mcp-config --mcp-config '{"mcpServers":{}}' --setting-sources project --permission-mode dontAsk
--append-system-prompt-file <0600 file holding frame + rules.md + repository notes, inside the temporary directory>
--no-session-persistence --max-turns 3`, run from an empty temporary directory with only `PATH`, `HOME`, `USER`,
`LOGNAME`, `SHELL`, `TERM`, `TMPDIR`, `LANG`, the profile (`CLAUDE_CONFIG_DIR`) and `ANTHROPIC_API_KEY` passed through. `--setting-sources project` keeps the
profile's hooks and plugins out while OAuth keeps working; `--bare` does not
read OAuth at all (probed on CLI 2.1).

## 7. Verification

Run `python3 -m unittest discover -s tests`. Integration tests use real scratch settings and fake GitHub/model
calls for migration, rehash, reload, pre-write races, withheld retries, per-repository modes and CLI admission checks.
The existing eligibility, fingerprint, identity, lock, unknown-write, engine isolation and stdin tests remain active.
No external model or live GitHub write is needed for this suite.
