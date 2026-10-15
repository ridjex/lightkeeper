# Changelog

All notable changes to lightkeeper. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.4] — 2026-10-15

### Changed

- Every posted comment is labelled `Automated review for @<login> (lightkeeper)` by default
  (`[comment] disclose = true`); set it to `false` to post without the label. Users who never wrote the
  key get the label from this version on.

## [0.1.3] — 2026-10-15

### Changed

- The rules template is a pure prompt again: the notes to the person and the choice of posture moved to
  README "Writing rules" and to the `--init` output.
- The settings template keeps `[agent]`, `[comment]` and `[screen]` headers live, so uncommenting one key works.

## [0.1.2] — 2026-10-15

### Changed

- The settings template written by `--init` sets only `login`, the repository and `post`; every other key is
  commented out with its default, so omitted keys follow the installed version's defaults and written keys stay
  the user's across updates.
- The rules template opens with a choice of posture: approve by default, or comment by default with
  approval left to a human.

## [0.1.1] — 2026-10-15

### Fixed

- Focused one-row checkboxes no longer collapse into a dark block.
- Launching an external editor from a worker thread no longer corrupts the terminal.

### Added

- Built-in editor for settings, rules, templates and repository notes, with validated saving and
  `Ctrl-E` access to `$EDITOR`.

## [0.1.0] — 2026-10-14

First public release.

- Reviews pull requests where you are a requested reviewer, by your `rules.md`, through your `gh` and `claude`
  logins; every repository starts in dry-run; approvals are bound to the reviewed commit.
- Deployment gate approvals from your GitHub notifications.
- Textual screen with Overview, PR detail, Repositories and Agent; an explicit menu with every setting;
  the lighthouse idle picture, on by default.
- `lightkeeper --init`, `--once`, `--dry-run`, `--status`, `--check-repo`, `--preview-idle`, `--version`.
- Update reminder from GitHub releases, once a day, `[screen] updates = false` turns it off.
- Homebrew tap and pipx installation.
