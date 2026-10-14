# Changelog

All notable changes to lightkeeper. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
