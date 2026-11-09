<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="static/branchwarden-logo-dark.svg">
    <img src="static/branchwarden-logo-light.svg" alt="Branchwarden" width="340">
  </picture>
</p>

**Branchwarden** watches the pull requests and branches across a GitHub
organisation or account:
- **Pull requests:** every open PR and what state it's in (draft, needs review,
  changes requested, approved, CI failing, conflicts), who it's waiting on, and for how long.
- **Branches:** those with no PR that have gone stale, and those left behind by
  PRs that were merged or closed.
- **Reviewers:** request reviewers from the dashboard, with suggestions drawn
  from CODEOWNERS and who has reviewed in that repo recently. An optional rule
  assigns one automatically when nobody has been asked: code owners first, then
  the least busy person in a pool.
- **Metrics:** time to first review, time to merge, merges per week, and daily
  trends of PRs needing review and stale branches, per repo or overall.
- **Notifications:** posted to Microsoft Teams (Workflows webhooks, Adaptive Cards) and
  Slack (incoming webhooks, Block Kit) when PRs open, are reviewed, merge or
  close. Reminders go out in working hours, plus a weekly digest of branches to tidy up.

Branchwarden only reads branches. It never pushes and never deletes. Its one
write to GitHub is requesting (or withdrawing) reviewers.

The look follows [Skyfarer](https://github.com/chrisnock-personal/skyfarer).

For an illustrated tour of every feature, see the
[project overview](https://github.com/chrisnock-personal/project-overviews/blob/main/branchwarden/README.md).

![The Branchwarden overview: summary tiles, a card per repository, the pull requests that need attention, delivery channel health and the scan log](docs/images/hero.png)

## Quick start

```bash
cp .env.example .env              # set POSTGRES_PASSWORD (compose won't start without it)
podman-compose up -d              # or docker compose; builds the image, starts Postgres, the worker and the dashboard
podman-compose logs dashboard     # the one-time setup code, and the certificate's fingerprint
```

Open `https://<host>:8443`. The certificate is self-signed until you upload
your own, and the fingerprint is in the log so you can check it. Enter the
setup code and create the first admin. Then, under **Settings › GitHub**:

1. Paste a token, or the GitHub App's ID and private key (see below).
2. Add the organisations and accounts to scan.

The first scan of each account records what's there without sending messages.
After that, scans pick up new PRs, reviews, merges and closes.

## How it fits together

```
┌───────────────────────── compose ─────────────────────────┐
│                                                            │
│  ┌──────────┐   ┌────────────────────────────┐             │
│  │ postgres │◄──┤ worker                     │──► GitHub API (GraphQL, REST)
│  │ (not     │   │ scans, events, reminders,  │──► Teams / Slack webhooks
│  │ published)│  │ digest, delivery queue     │             │
│  │          │   └────────────────────────────┘             │
│  │          │   ┌────────────────────────────┐             │
│  │          │◄──┤ dashboard (gunicorn, HTTPS)│◄── browsers :8443
│  └──────────┘   │ UI, settings, /github/webhook ◄── GitHub App webhooks
│                 └────────────────────────────┘             │
│  volumes: pgdata · keys (secret key) · certs (TLS)         │
└────────────────────────────────────────────────────────────┘
```

- **The worker** (`worker.py`) runs a full scan on a schedule (every 10 minutes
  by default). Repos that a webhook mentions, or whose reviewers changed, are
  rescanned within seconds. Each repo takes about three GraphQL queries:
  repositories, open PRs (with reviews, checks and changed files), and branches
  (each with its newest PR and how far it is ahead of or behind the default
  branch). Comparing each scan with the last gives the events.
- **The dashboard** (`dashboard.py`, `admin.py`) serves the pages and the
  webhook endpoint. A webhook only queues a rescan of its repo, so a forged or replayed event
  can't change what Branchwarden shows. Events without a valid
  `X-Hub-Signature-256` are refused anyway.
- **Messages** are queued per channel in Postgres and retried with back-off
  (1 min, 5 min, 15 min, 1 h, 3 h, 6 h) on 429 or 5xx responses. Other 4xx
  responses are permanent failures, shown on the Deliveries page.

## Connecting to GitHub

You can use both connections: per account, Branchwarden uses the App where it's
installed and otherwise the token (or only one, set per account).

**A fine-grained personal access token** is the quickest. Give it:
- Pull requests: read and write. The write access is only for requesting reviewers.
- Contents: read.
- Metadata: read.
- Members: read, for organisations, so the reviewer picker can list people and teams.

Branchwarden reads the token's expiry from GitHub and shows a warning 14 days before.

**A GitHub App** suits organisations: it has its own API budget, gets webhooks,
and acts as itself.
1. Create it under *Organisation settings › Developer settings › GitHub Apps*
   with these permissions:
   - Pull requests: read and write
   - Contents: read
   - Metadata: read
   - Checks: read
   - Members: read
2. Subscribe it to these events: *Pull request*, *Pull request review*, *Check suite*, *Push*, *Create*, *Delete*.
3. Set its webhook URL to `https://<branchwarden>/github/webhook`, with a random secret.
4. Generate a private key, then install the App on the organisation.
5. Paste the App ID, the key and the webhook secret into Settings › GitHub.

GitHub has to reach the webhook URL. Without webhooks, Branchwarden still
picks everything up on its next scheduled scan.

GitHub Enterprise Server works too: set the API address to `https://host/api/v3`.

## Channels, rules and people

**Settings › Channels and reminders**

- **Channels:** each channel has a webhook URL and is routed by repo patterns
  (`api-*`, `my-org/web-console`) and/or teams (`my-org/platform`, matched
  against a PR's requested team reviewers). A channel with neither gets everything.
  - **Teams:** use the *“Post to a channel when a webhook request is received”*
    workflow. The older Office 365 connectors are retired.
  - **Slack:** use an incoming webhook, or for **threads**, a bot token
    (`chat:write`, with the bot invited to the channel) and the channel ID. With
    a bot token, each PR's messages stay in one thread. Merges and closes are
    also shown in the channel, and the digest is posted on its own.
    Teams webhooks can't reply in a thread, so Teams channels get one card per message.
- **Event rules:**
  - PR opened, or marked ready for review
  - Review submitted (approved, or changes requested)
  - Merged, or closed without merging. The message notes when the branch was left behind.
- **Reminder rules:**
  - No reviewer assigned (after 4 working hours)
  - Waiting for review (24 working hours)
  - Changes requested with no new commits (3 days)
  - Approved but not merged (2 days, off by default)

  Each reminder has its own wait, repeat interval, maximum count, target
  channels, @mentions, a skip label, and an option to group PRs into one
  message per channel. Reminders only go out in working hours (days, times and
  time zone are set on the same page). A reminder starts afresh if the PR
  leaves that state and comes back. Every reminder has a **Snooze** button that
  opens the PR in Branchwarden, where a member can put its reminders off for
  1, 3 or 7 days.
- **Auto-assign reviewers** (off by default, as it writes to GitHub): when a PR
  has had nobody asked for a set time, Branchwarden requests a reviewer.
  - **Who:** code owners of the changed files first, then the least busy
    person in a pool of logins and teams (or recent reviewers in the repo). The
    least busy has the fewest open review requests, then the fewest assignments
    in the last 14 days. The author is never picked.
  - **When:** in working hours only, and once each time a PR is left without a reviewer.
  - The channel is told who was picked and why.
- **Weekly digest:** stale branches and branches left behind, per channel's
  repos, with each branch's last committer @mentioned.

**Settings › People** matches GitHub logins to a Teams user principal name and
a Slack member ID, so @mentions reach the right person. Anyone without a match
appears as a bold name.

## Roles

| Role | Can |
|---|---|
| Admin | Everything: GitHub connections, channels and rules, people, the TLS certificate, users |
| Member | Read everything, request reviewers, snooze reminders, ignore branches, start a scan |
| Viewer | Read the overview, pull requests, branches and deliveries |

People are added under *Settings › Users and access*. Each gets a one-time setup
link that lasts 24 hours. Branchwarden doesn't send email, so you pass the link on.

**Sign in with GitHub** can replace those links.
- **Setup:** register an OAuth App (or use the GitHub App's own client ID and
  secret) with the callback URL shown on that page. Then list who gets each
  role: logins, `org/team` slugs or whole organisations.
- **Roles:** each person's role is worked out from their GitHub teams every
  time they sign in. Anyone matching no list is turned away, so having a
  GitHub account isn't enough.
- **Tokens:** sign-in uses PKCE and a state check. The GitHub token is only
  used to read the person's teams, and is never stored.
- **Fallback:** local accounts keep working, so an admin can always get in.

## Security

Branchwarden follows the secure-by-default baseline from the 2026-11-09 review
of all repos:

- **HTTPS by default.** gunicorn serves TLS 1.2 and up on 8443. A self-signed
  certificate (EC P-256) is made on the first start for the names and IPs in
  `TLS_HOSTNAMES`. Admins upload their own under *Settings › TLS*:
  - The key must match the certificate, and the certificate must be in date, before anything changes.
  - The old pair is kept, so it can be restored.
  - The fingerprint is shown.

  HSTS is on. Cookies are `Secure`, `HttpOnly` and `SameSite=Lax`.
- **Secrets encrypted at rest.** The token, the App's private key, the webhook
  secret and every channel's webhook URL are sealed with AES-256-GCM, and none
  is ever shown again.
  - **The key:** generated on the first start into its own `keys` volume, away
    from the database, unless `.env` sets `BRANCHWARDEN_SECRET_KEY`.
  - **Rotation:** move the old key to `BRANCHWARDEN_SECRET_KEY_PREVIOUS` and
    restart. Secrets are sealed again with the new key on start.
- **Fail closed:**
  - There's no default admin: the first account needs the setup code from the log.
  - compose won't start without `POSTGRES_PASSWORD`.
  - Webhooks with a missing or bad signature, or no secret set, are refused.
  - Every form needs a CSRF token.
  - Sign-in attempts are throttled.
- **Bound narrowly.** Postgres isn't published, only the dashboard port is.
  The containers run as an unprivileged user.
- **Headers:** a Content-Security-Policy without third-party scripts,
  `frame-ancestors 'none'`, `nosniff` and `Referrer-Policy: same-origin`.
- **Outbound URLs are checked.** Channel webhooks must be `https://` on Slack's
  or Microsoft's hosts, and the public and API addresses must be `https://`.
  Errors never echo a webhook URL, since the URL is the secret.
- **CI** runs the tests, `pip-audit` and `gitleaks` on every push.

**Retention:** closed PRs are kept for 365 days, for the metrics. Messages and
scans are kept for 90 days. Both are set under *Settings › GitHub*, and older
records are removed once an hour.

**Not covered yet:** the Postgres volume itself isn't encrypted. Run it on an
encrypted disk if PR titles and branch names are sensitive. Back up the `keys`
volume with the database: without it, stored secrets can't be read.

## Configuration (.env)

| Variable | Default | |
|---|---|---|
| `POSTGRES_PASSWORD` | required | |
| `BRANCHWARDEN_SECRET_KEY` | generated into the `keys` volume | 32+ characters if you set it |
| `BRANCHWARDEN_SECRET_KEY_PREVIOUS` | | for rotation |
| `DASHBOARD_EXPOSE` | `8443` | where compose publishes HTTPS, e.g. `127.0.0.1:8443` |
| `TLS_HOSTNAMES` | | extra names/IPs for the self-signed certificate |
| `BRANCHWARDEN_TLS` | `on` | `off` only behind a TLS proxy (with `BRANCHWARDEN_BEHIND_PROXY=true`) |
| `GITHUB_TARGETS`, `GITHUB_TOKEN`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_FILE`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_API_URL`, `PUBLIC_URL` | | imported on the first start only, then edited in the dashboard |

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # the pure logic, rendering, crypto and TLS checks; no database needed
pip-audit -r requirements.txt
```

The files:

| File | What it does |
|---|---|
| `analysis.py` | PR states, branch statuses, CODEOWNERS, picking reviewers and working-hours maths. No I/O |
| `scanner.py` | The GraphQL queries and turning a scan into rows and events |
| `rules.py` | Events, auto-assign, reminders and the digest |
| `metrics.py`, `housekeeping.py` | The metrics page; retention and the daily snapshot |
| `ghlogin.py` | Sign in with GitHub |
| `notify.py` | Routing, rendering for Teams and Slack, delivery and Slack threads |
| `github.py` | App and token clients |
| `settings.py` | Encrypted settings, users and history |
| `tls.py` | Certificates |
| `worker.py` | The loop |
| `dashboard.py`, `admin.py`, `templates/` | The web UI |

## Licence

[MIT](LICENSE). Branchwarden is fully open source.
