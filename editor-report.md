# Editor and terminal fixes — 0.1.1

Implemented on `editor`; release tagging and the Homebrew tap remain with the maintainer.

| File | Change |
|---|---|
| `lightkeeper/ui/editor.py:14` | Worker-loaded `Editor(ModalScreen)` with plain TextArea, validated save, dirty-close confirmation, external-editor shortcut and inline errors. |
| `lightkeeper/ui/editing.py:37` | Shared candidate commit preserves validation, private permissions, fsync, write lock, original-byte comparison and atomic replacement; external launch hops to the main thread. |
| `lightkeeper/ui/repos.py:203` | Repository notes open the editor; shared RepoCheckbox focus CSS suppresses the border and underlines the label. |
| `lightkeeper/ui/agent.py:111` | Settings/rules/template keys open the editor with existing validators and completion callbacks; the existing Menu routes use these same entry points. |
| `lightkeeper/ui/textual_app.py:259` | Editor stack blocks app navigation and printable quit/help bindings; separate Ctrl-C action retains quitting. |
| `tests/test_editor.py:27` | Offline pilots cover editing, validation, conflicts, confirmations, shortcuts, thread ownership, symlinks, layout and focused marks in both forms. |
| `tests/test_s3.py:289` | Main-thread regression test; existing Runner-progress test retained; entry-point and failure-survival pilots updated. |
| `tests/test_menu.py:46` | Version assertions follow the package version. |
| `README.md:321`, `docs/screen.md:94` | Document built-in editing, keys, external fallback and save behavior. |
| `docs/decisions.md:55`, `AGENTS.md:41` | Record the default-editor rationale, main-thread suspension requirement and focus-border rule. |
| `CHANGELOG.md:7`, `lightkeeper/__init__.py:3` | Add the requested dated 0.1.1 release notes and version bump. |

Choices: `Editor` is a plain-text modal, at most 100 columns and 90% terminal height. Markdown wraps;
TOML does not. Tab moves focus in both because TOML needs no tab indentation. `q`, `m`, `?`, and `1`–`3`
are text; Ctrl-C quits. The footer lists editor keys only. Ctrl-E refuses dirty buffers. External errors
retain the private rejected copy; built-in errors retain the editable buffer. `call_from_thread(launch)`
blocks the disk worker while the synchronous main-thread callback owns suspend plus subprocess; the
Runner remains independent. Textual 8.2.8 already calls `refresh(layout=True)` after resume, so no extra
refresh is needed. Subprocess launch exceptions leave suspend normally before being reported.

Verification (no interactive app launch):

- `ruff check .` (worktree virtual environment on PATH): `All checks passed!`
- `python3 -m unittest`: `Ran 577 tests in 88.690s`; `OK (skipped=1)` — only the opt-in benchmark.
- `python3 -m unittest tests.test_editor tests.test_s3 tests.test_menu`: `Ran 53 tests in 37.052s`; `OK`.
- `python3 -S -m unittest`: `Ran 562 tests in 10.354s`; `OK (skipped=114)` — optional UI dependencies excluded.
- `git diff --check`: no output, exit 0.

**Independent Claude review unavailable:** the visible reviewer returned an expired OAuth-session error,
with no model review. No review success is claimed. The transcript remains in the ignored worktree
virtual environment. No requested implementation or test was left incomplete. The report filename was
not supplied in the visible launch request, so this report uses the announced `editor-report.md` fallback.
