# Working on lightkeeper (for people and for coding agents)

lightkeeper reviews GitHub pull requests in its user's name: the script collects, a model proposes, the code
decides what may be posted, the journal records everything. Read README.md first, then docs/design.md.
These are the rules that are easy to break without noticing.

## Boundaries that must hold

- **The model proposes, the code posts.** The model has no tools, no network and no say over the three
  floors (bot author, failed check, missing patch). Never route a write through model output.
- **One review per head and action.** Posting re-fetches the pull request and compares head, description,
  checks, files and reviews before writing; approvals carry `commit_id`. Keep that check in front of every write.
- **Frozen text is hashed.** The prompt frame in `lightkeeper/reviewer.py` and the rules, repository notes,
  remarks level and answer schema form the judgement hash; changing a word re-opens every settled outcome
  for every user. `lightkeeper/_frame_v2.py` and `policy.SEMANTICS_VERSION` are frozen legacy inputs for
  re-hashing old journals: never edit them.
- **Journal records are append-only and forward-compatible.** New record kinds and fields are fine; never
  change the meaning of an existing field, never rewrite the file (except the torn-tail repair in
  `journal.py`, which only truncates an incomplete last line). `--status` and replay must never write.
- **The comment trace is the provenance.** `<!-- lightkeeper v2 head=… judgement=… engine=… -->` is how the
  tool recognises its own reviews; the former name `approval-delegate` must stay recognised. Model text is
  neutralised so it can never forge a trace or a marker line.
- **Secrets and raw text never travel as command-line arguments.** Prompts go through stdin; the model runs
  in an empty temporary directory with a scrubbed environment.
- **Every boundary is UTF-8** regardless of locale: subprocess pipes, persisted files, log streams.
- **Configuration and state directories are private** (0700/0600) and are never written by the screen; the
  Runner owns writes and holds the locks. Settings saves patch one table atomically.

## Style

- Standard library in the core; Rich and Textual only under `lightkeeper/ui/`. No new dependencies.
- Maximally simple and transparent: short functions, literal text, no hidden state machines, no magic numbers
  without a comment that says why. Prefer deleting over adding.
- English in code, comments, docs and commits. No personal data, machine paths or employer specifics anywhere.
- Code, tests and README change in the same commit. A non-obvious choice gets one line in `docs/decisions.md`.
- Tests never call GitHub or a model and never depend on the working directory, `HOME` or installed CLIs
  (`tests/helpers.py` has the isolation helpers). The suite must pass without Rich and Textual (skips are fine).

## The screen and the idle picture

- `App.suspend` must run on the main thread (editor sessions go through `edit_file`);
  one-row checkboxes need the focus border suppressed.
- Textual is the only live screen; `--plain` is the fallback and must keep working.
- The idle picture (`lightkeeper/ui/idle.py`) is a deterministic function of time, width and settings: no
  randomness, no mutable state between frames; events come from a hash of the time slot. Keep it a
  big-pixel half-block raster made from literal Rich Text; the frame budget is 4 ms at 200 columns
  (`LIGHTKEEPER_BENCH=1` runs the budget test). Judge changes in `lightkeeper --preview-idle`, not in screenshots.
- Depth order is distance: stars, sun or moon, clouds, the beam, events on the water, rock and tower.
- The kitty keyboard protocol stays disabled (`TEXTUAL_DISABLE_KITTY_KEY=1` in `__main__`): some terminals
  keep it on after exit. Every binding has a plain encoding; do not add bindings that need it.
- The screen was built on a dark background with a font that has half-block glyphs; `NO_COLOR` and
  `LIGHTKEEPER_ASCII=1` are the fallbacks and must keep rendering the same information.

## Releasing

See docs/release.md: bump `__version__`, tag, update the Homebrew tap formula. The update reminder reads the
latest GitHub release of `ridjex/lightkeeper`, so a tag without a release is invisible to users.
