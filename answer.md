Done: check reruns count once, and model review alerts are opt-in; no version bump.

| Change | Location |
| --- | --- |
| Keep the latest check by name/context; use startedAt, then completedAt, then list order when timestamps tie or cannot be compared. Undated queued reruns still wait. Required flags, logs and reviewer context use the survivor. | `lightkeeper/github.py:217` |
| Add user-level `[alerts] reviews = false`, strict validation, serialization and preservation through settings saves. | `lightkeeper/settings.py:52`, `lightkeeper/settings.py:201`, `lightkeeper/migrate.py:38` |
| Mark parsed model text explicitly, gate its notifications, preserve code alerts and restore provenance from saved/legacy assessments. Retain model notes on local nothing outcomes too. | `lightkeeper/models.py:214`, `lightkeeper/reviewer.py:247`, `lightkeeper/reviewer.py:296`, `lightkeeper/__main__.py:694`, `lightkeeper/__main__.py:1163` |
| Show a plain model note in PR detail without creating a needs-you row or badge; retain it across later write errors. | `lightkeeper/ui/project.py:133`, `lightkeeper/ui/detail.py:314` |
| Update the template, README, design, decisions and Unreleased changelog together with regression tests. | `lightkeeper/templates/settings.toml:27`, `README.md:360`, `docs/design.md:55`, `docs/decisions.md:6`, `CHANGELOG.md:5` |

Judgement verification: before editing, `tests.helpers.make_settings().judgement('org/repo').hash` was `77666cbd56be`; it remains identical with alerts disabled or enabled, including a settings reload (`tests/test_settings.py:38`). Compared frozen frame/schema/constants against starting commit `a7bef23a` via AST and compared `_frame_v2.py` and packaged rules byte-for-byte: unchanged. No answer-schema or semantics-version changes.

Validation: `ruff check .` passed; `python3 -S -m unittest` passed (625 tests, 130 skipped); `python3 -m unittest tests.test_ux` passed (45 tests, one skipped). `git diff --check` passed. Tests use fake GitHub/model calls; no live reviews were posted.

Independent Claude review identified the undated queued-rerun case and model-note loss on write errors; both were corrected and regression-tested. Delegation note: accepted both findings; reviewer pane closed.

Clarification: this checkout handles bot authors at the approval floor, not initial eligibility. That behavior is preserved; tests cover the real bot floor, an explicit eligibility nothing, incomplete files, context caps, unclear answers and active blocks. Existing journal notify records remain unchanged and visible; the setting controls future alerts. No unresolved implementation questions.

## Follow-up: light-poll rerun parity

Done: light polls read check names, states and timestamps and share the pure `latest_runs` helper with full fetches (`lightkeeper/github.py:93`, `lightkeeper/github.py:210`). Counts and totals include only surviving reported checks, so superseded cancelled runs no longer cause repeated full-fetch wakes. Batching remains one GraphQL call per up to 50 PRs. Truncated contexts skip only the affected PR and log once per head; missing rollups still mean zero checks (`lightkeeper/github.py:418`).

Regression tests compare `counts`, `ci` and `wake_key` for reruns and unreported declared checks, and cover truncation, warning suppression and recovery (`tests/test_fast_poll.py:62`). Replaced the light-poll fixture with context nodes; updated README API-budget wording, design and decisions.

Validation: `ruff check .` passed; `python3 -S -m unittest` passed (627 tests, 130 skipped); `python3 -m unittest tests.test_ux` passed (45 tests, one skipped); `git diff --check` passed. All GitHub/model calls in tests were fake.
