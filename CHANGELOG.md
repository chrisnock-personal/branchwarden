# Changelog

All notable changes to Branchwarden. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

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
- **My work:** reviews waiting on you, your open PRs and what they need next,
  your branches to tidy, and your snoozed reminders.
- **Teams and Slack:**
  - events when a PR opens, gets a review, or merges or closes,
  - reminders in working hours, each with a snooze button,
  - a weekly stale-branch digest,
  - Slack threads per PR through a bot token,
  - @mentions through the People page.
- **Metrics:** time to first review and to merge, merges per week, and daily
  trends.
- **MVP** (a tab under Metrics): 7-day leaderboards for reviews, merges, branch prunes and PRs
  raised, with an optional weekly post.
- **Access:** admin, member and viewer roles; local accounts with one-time
  setup links; sign in with GitHub, with roles from GitHub teams.
- **Security:**
  - HTTPS only, with a generated certificate and an upload page,
  - secrets sealed with AES-256-GCM, with a rotatable key kept apart from the database,
  - fail-closed defaults,
  - CI with pip-audit and gitleaks.
- **Housekeeping:** retention for closed PRs, messages and scans.

[0.1.0]: https://github.com/chrisnock-personal/branchwarden/releases/tag/v0.1.0
