# Branchwarden API

Generated from `api.py` (`python api.py --markdown > docs/api.md`); CI checks it's current.
The same, machine-readable: `GET /api/v1/openapi.json`.

- **Base:** `https://<branchwarden>/api/v1`, HTTPS only.
- **Auth:** `Authorization: Bearer bwt_…`, a personal token (your menu › API tokens) or a service token
  (Settings › Users and access). A token acts with its owner's role (a service token with its own);
  a read-only token only reads. Changes are recorded in the change history with the token named.
- **Errors:** `{"error": {"status": 404, "code": "not_found", "message": "…"}}`.
- **Lists:** `{"items": [...], "next_cursor": "…", "total": n}`; pass `?cursor=` for the next page,
  `?limit=` (1 to 200, 50 by default).
- **Rate limit:** 300 requests a minute per token (`BRANCHWARDEN_API_RATE`); then 429 with `Retry-After`.

## Account

| | Path | Who | What |
|---|---|---|---|
| GET | `/me` | viewer | The token and who it acts as |
| GET | `/tokens` | viewer | Your personal API tokens (not the tokens themselves) |
| POST | `/tokens` | anyone (not read-only) | Make a personal API token (shown once) (body: `name`, `scope`, `expires_days`) |
| DELETE | `/tokens/{token_id}` | anyone (not read-only) | Revoke one of your personal tokens |

## Overview

| | Path | Who | What |
|---|---|---|---|
| GET | `/overview` | viewer | Headline numbers, as on the overview |

## Pulls

| | Path | Who | What |
|---|---|---|---|
| GET | `/pulls` | viewer | Open pull requests, longest waiting first (params: `filter`, `repo`, `author`, `reviewer`, `q`, `limit`, `cursor`) |
| GET | `/pulls/{pr_id}` | viewer | One open pull request |
| POST | `/pulls/{pr_id}/reviewers` | member | Request or withdraw reviewers on GitHub (body: `add`, `remove`, `notify`, `dry_run`) |
| POST | `/pulls/{pr_id}/snooze` | member | Snooze a PR's reminders for 1, 3 or 7 days (body: `days`) |
| DELETE | `/pulls/{pr_id}/snooze` | member | Turn a PR's reminders back on |

## Branches

| | Path | Who | What |
|---|---|---|---|
| GET | `/branches` | viewer | Flagged branches: stale, left after a PR, and the rest (params: `status`, `repo`, `q`, `limit`, `cursor`) |
| GET | `/branches/ignored` | viewer | Branches someone chose not to flag |
| POST | `/branches/{branch_id}/ignore` | member | Stop flagging a branch |
| DELETE | `/branches/ignored` | member | Flag an ignored branch again (params: `repo`, `branch`) |

## Repos

| | Path | Who | What |
|---|---|---|---|
| GET | `/repos` | viewer | Every repository with its counts and health flags |
| GET | `/repos/{owner}/{name}` | viewer | One repository: PRs, branches, health, reviewers, routing (params: `days`) |

## Workflows

| | Path | Who | What |
|---|---|---|---|
| GET | `/workflows` | viewer | GitHub Actions workflows: due times, runs and results per day (params: `days`, `repo`) |

## Me

| | Path | Who | What |
|---|---|---|---|
| GET | `/my-work` | viewer | What's waiting on the token's owner: reviews, their PRs, branches, snoozes |
| PUT | `/my-work/morning` | anyone (not read-only) | Turn the token owner's morning message on or off, and say where it goes (body: `enabled`, `to`, `teams_url`, `remove_teams`) |
| POST | `/my-work/morning/test` | anyone (not read-only) | Send the token owner's morning message now |

Both morning-message endpoints check ownership: only the account that set up a login's message, or a
GitHub sign-in as that login, can change or send it (403). Setting one up for the first time needs that
GitHub sign-in (409): a login typed on My work isn't proof that it's yours.

## Metrics

| | Path | Who | What |
|---|---|---|---|
| GET | `/metrics` | viewer | Review and merge times, merges per week, by repo and by size (params: `days`, `repo`) |
| GET | `/mvp` | viewer | The week's MVPs: reviews, merges, branch prunes, PRs raised (params: `period`) |

## Scans

| | Path | Who | What |
|---|---|---|---|
| POST | `/scan` | member | Start a full scan now |

## Admin

| | Path | Who | What |
|---|---|---|---|
| GET | `/admin/settings` | admin | Every setting: values, and which secrets are set (never their values) |
| PATCH | `/admin/settings/{section}` | admin | Change one section of settings, with the same fields and checks as its dashboard form (body: `<FIELD>`) |
| GET | `/admin/targets` | admin | Accounts and organisations to scan |
| POST | `/admin/targets` | admin | Add an account or organisation to scan (body: `login`, `kind`, `via`) |
| PATCH | `/admin/targets/{target_id}` | admin | Switch scanning on or off, or change the connection (body: `enabled`, `via`) |
| DELETE | `/admin/targets/{target_id}` | admin | Stop scanning it, and remove its repos, PRs and branches |
| GET | `/admin/channels` | admin | Teams and Slack channels (webhook URLs and tokens are never shown) |
| POST | `/admin/channels` | admin | Add a Teams or Slack channel (body: `kind`, `name`, `url`, `bot_token`, `slack_channel`, `repos`, `teams`, `enabled`) |
| PATCH | `/admin/channels/{channel_id}` | admin | Change a channel (body: `kind`, `name`, `url`, `bot_token`, `slack_channel`, `repos`, `teams`, `enabled`) |
| DELETE | `/admin/channels/{channel_id}` | admin | Remove a channel and its queued messages |
| POST | `/admin/channels/{channel_id}/test` | admin | Send a test message to a channel |
| GET | `/admin/rules` | admin | Event, reminder, digest and post rules |
| PATCH | `/admin/rules/{key}` | admin | Switch a rule on or off, or change its settings (body: `enabled`, `<PARAM>`) |
| POST | `/admin/rules/{key}/send` | admin | Send the weekly digest or MVP post now |
| GET | `/admin/people` | admin | GitHub logins matched to Teams and Slack identities |
| PUT | `/admin/people/{login}` | admin | Set a person's name, Teams user and Slack member ID (body: `name`, `teams_id`, `slack_id`) |
| GET | `/admin/users` | admin | Accounts, and how each signs in |
| POST | `/admin/users` | admin | Add an account; returns its one-time setup link (body: `name`, `email`, `role`) |
| PATCH | `/admin/users/{user_id}` | admin | Change an account's role (body: `role`) |
| POST | `/admin/users/{user_id}/setup-link` | admin | A new one-time setup link for an account |
| DELETE | `/admin/users/{user_id}` | admin | Remove an account |
| GET | `/admin/service-tokens` | admin | Service tokens (not the tokens themselves) |
| POST | `/admin/service-tokens` | admin | Make a service token with its own role (shown once) (body: `name`, `scope`, `expires_days`, `role`) |
| DELETE | `/admin/service-tokens/{token_id}` | admin | Revoke a service token |
| GET | `/admin/tls` | admin | The TLS certificate in use |
| PUT | `/admin/tls` | admin | Install a certificate (checked first), a new self-signed one, or the previous one (body: `cert`, `key`, `chain`, `self_signed`, `restore_previous`) |
| GET | `/admin/deliveries` | admin | Messages to Teams and Slack, newest first (params: `status`, `limit`, `cursor`) |
| GET | `/admin/scans` | admin | Recent scans: what they read, skipped and cost |
| GET | `/admin/history` | admin | Who changed what, from the dashboard and the API |

## Meta

| | Path | Who | What |
|---|---|---|---|
| GET | `/openapi.json` | viewer | This API, described in OpenAPI 3 |

## Examples

```bash
TOKEN=bwt_…   # your menu › API tokens
curl -s -H "Authorization: Bearer $TOKEN" https://branchwarden.example.com/api/v1/my-work
curl -s -H "Authorization: Bearer $TOKEN" 'https://branchwarden.example.com/api/v1/pulls?filter=stalled&repo=api-*'
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"add": ["sasha"], "dry_run": true}' https://branchwarden.example.com/api/v1/pulls/42/reviewers
```
