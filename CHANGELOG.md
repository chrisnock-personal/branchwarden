# Changelog

All notable changes to Branchwarden. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Encrypted backups** (#3): `deploy/backup.sh` puts the database dump, the
  `keys` and `certs` volumes and `.env` in one file encrypted with age to the
  public keys you give (with `--keep N` to prune old ones), and
  `deploy/restore.sh` puts it all back on the same host or a fresh one, then
  starts Branchwarden. It won't overwrite existing data without `--force`.
  Works with podman or docker compose and with the quadlets. CI runs a backup,
  destroys the stack, restores it and checks it all came back, on every push.

## [0.4.0] - 2026-11-15

### Added

- **Demo mode** (#9): `python demo.py` loads made-up activity for a demo-org
  organisation (six repositories, eight people, open PRs in every state, five
  weeks of merges, reviews, branch deletions, daily snapshots and workflow
  runs); `--remove` takes it out. It only loads into an empty Branchwarden,
  and while it's loaded every page says so, the worker doesn't scan, and
  connecting GitHub is refused.
- **Integration and browser tests** (#5): against a real Postgres, the scanner
  end to end with a stand-in for GitHub, events and routing, the delivery
  queue's retries and failures, workflows, skipping unchanged repos, the demo
  data and every page; and a Playwright run through setup and every section.
  CI runs them on every push.
- **CI in the README:** a status badge, links to the latest release and the
  image, and a "CI and releases" section saying what each workflow runs.

### Changed

- **Releases check CI:** the release workflow refuses a tag whose commit
  hasn't passed CI.

## [0.3.0] - 2026-11-15

### Added

- **Repositories** (#11, #17), a new *Repos* link:
  - Every repo with its open PRs, branches to tidy, failing workflows and
    health flags: head branches not deleted after merge, the default branch
    not protected, merging without an approving review, no CODEOWNERS. Each
    flag links to the setting on GitHub. Protection and rulesets are read
    once a day; "no required reviews" is only flagged when GitHub shows it.
  - A page per repo: its PRs, stale and left-behind branches, metrics,
    workflows, who reviews there, CODEOWNERS rules and the channels it goes to.
  - Repo names across the dashboard open it, and the overview's cards link
    their status line to the repo's pull requests and their branch line to its
    branches (#16).
- **Big and stalled PRs** (#12): badges and filters for PRs over a size
  (lines or files) and for PRs with no commits, reviews or comments for a
  number of days, both set in *Settings › GitHub*. A "Stalled" reminder rule
  (off until switched on), and Metrics by PR size.
- **Ticket links** (#14): keys like `ABC-123` in PR titles, branch names and
  descriptions link to your tracker (a URL template with `{key}`), on the
  Pull requests and Branches pages and as buttons on messages, with a "No
  ticket" filter. Only the keys are kept from descriptions.
- **SECURITY.md** (#8), with GitHub's private vulnerability reporting switched on.

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
