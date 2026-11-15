# Changelog

All notable changes to Branchwarden. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Health checks** (#4): compose checks the dashboard (`/healthz`, which also
  checks the database) and the worker (a heartbeat it touches every few
  seconds and after each repo during a scan), with `healthcheck.py`.
- **A banner on every page when scans have stopped:** no full scan finished
  for more than twice the scan interval, and none running.
- **Podman quadlets** in `deploy/quadlet/`, to run as systemd user services,
  using the same volumes as compose.

### Changed

- **Faster scans for large organisations** (#2):
  - A scheduled scan skips the PR and branch queries for repos whose last
    push, open PR count and newest PR update haven't changed. Every repo is
    still read in full at least hourly.
  - Four repos are scanned at a time.
  - The worker waits and retries when GitHub returns a secondary rate limit.
  - *Settings › Deliveries* shows each scan's skipped repos and requests made.

## [0.2.1] - 2026-11-15

### Added

- **A published image** (#6): each release is built for amd64 and arm64 and
  pushed to `ghcr.io/chrisnock-personal/branchwarden` as `:<version>`,
  `:<major>.<minor>` and `:latest`. The compose file runs it by default
  (`BRANCHWARDEN_VERSION` in `.env` picks a version); `podman-compose build`
  still builds from the checkout.

### Fixed

- **Merges, reviews and PRs raised showed 0 on a new install** (#20). A merge
  only counted when a scan had first seen the PR open. Now each repo's first
  scan also imports the PRs merged or closed in the last 180 days (or the
  *Keep closed pull requests* period, if shorter), with who merged them and
  their reviews, so Metrics and MVP have numbers from the start. Existing
  installs import once on their next scan. No messages go out for them.

## [0.2.0] - 2026-11-15

### Added

- **Workflows page:** every GitHub Actions workflow per repo, as a strip of
  days (7, 14 or 30): each scheduled workflow's due times from its cron
  schedule, matched to the runs that answered them (passed, failed,
  cancelled, still running, or missed), and other workflows' runs per day.
  GitHub often starts scheduled runs late, hours late for quiet repos, so a
  due time counts as missed only after 1.5 times that workflow's usual delay
  plus an hour (at least 90 minutes, at most 16 hours). Tiles for failing, missed and
  switched-off workflows, a "Needs attention" list, and each workflow's recent
  runs with trigger, branch, result, duration and who started it.
- **Teams and Slack:** two new rules. "Workflow failing or fixed" posts when a
  workflow goes from passing to failing (and back) on scheduled runs, the
  default branch, or every branch. "Scheduled run missed or workflow switched
  off" posts when a schedule didn't run, or when GitHub switched a scheduled
  workflow off for inactivity.
- The GitHub App's *Workflow run* webhook triggers a rescan.

### Changed

- The token and the GitHub App need **Actions: read** for workflows. Without
  it, everything else works as before.
- New dependency: PyYAML, to read workflow files.

## [0.1.0] - 2026-11-10

The first release.

### Added

- **Scanning:** GitHub organisations and accounts, through a GitHub App, a
  fine-grained personal access token, or both. Webhooks trigger a rescan
  within seconds. Repos are included or skipped by pattern.
- **Pull requests:** one headline state per PR, CI status, who it's waiting
  on and for how long, and filters.
- **Reviewers:** request or withdraw them from the dashboard, with suggestions
  from CODEOWNERS and recent reviewers. An optional auto-assign rule picks code
  owners first, then the least busy.
- **Branches:** stale branches with no PR; branches left after a merged or
  closed PR, or pushed to after their merge. Flagged only, never deleted.
- **My work** (in the menu under your name, with a count of reviews waiting):
  reviews waiting on you, your open PRs and what they need next, your
  branches to tidy, and your snoozed reminders.
- **Teams and Slack:**
  - events when a PR opens, gets a review, or merges or closes,
  - reminders in working hours, each with a snooze button,
  - a weekly stale-branch digest,
  - Slack threads per PR through a bot token,
  - @mentions through the People page.
- **Metrics:** time to first review and to merge, merges per week, and daily
  trends.
- **MVP:** 7-day leaderboards for reviews, merges, branch prunes and PRs
  raised, with an optional weekly post.
- **Access:** admin, member and viewer roles; local accounts with one-time
  setup links; sign in with GitHub, with roles from GitHub teams.
- **Security:**
  - HTTPS only, with a generated certificate and an upload page,
  - secrets sealed with AES-256-GCM, with a rotatable key kept apart from the database,
  - fail-closed defaults,
  - CI with pip-audit and gitleaks.
- **Deliveries** (under Settings): every message and its result, scans, and
  webhook events.
- **Housekeeping:** retention for closed PRs, messages and scans.

[0.1.0]: https://github.com/chrisnock-personal/branchwarden/releases/tag/v0.1.0
