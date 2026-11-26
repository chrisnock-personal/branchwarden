<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="static/branchwarden-logo-dark.svg">
    <img src="static/branchwarden-logo-light.svg" alt="Branchwarden" width="340">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/chrisnock-personal/branchwarden/actions/workflows/ci.yml"><img src="https://github.com/chrisnock-personal/branchwarden/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI status"></a>
  ·
  <a href="https://github.com/chrisnock-personal/branchwarden/releases/latest">Latest release</a>
  ·
  <a href="https://github.com/chrisnock-personal/branchwarden/pkgs/container/branchwarden">Container image</a>
</p>

**Branchwarden** watches the pull requests and branches across a GitHub
organisation or account:
- **Pull requests:** every open PR and what state it's in (draft, needs review,
  changes requested, approved, CI failing, conflicts), who it's waiting on, and for how long.
  Big PRs (by lines or files) and stalled ones (no commits, reviews or comments
  for a week) are flagged, and ticket keys like `ABC-123` in titles, branch
  names and descriptions link to Jira, Linear or any tracker.
- **Branches:** those with no PR that have gone stale, and those left behind by
  PRs that were merged or closed.
- **Repositories:** every repo with its open PRs, branches to tidy, failing
  workflows and the settings behind most branch and review problems (head
  branches not deleted after merge, an unprotected default branch, merging
  without an approval, no CODEOWNERS), each linked to where it's changed on
  GitHub. A page per repo brings together its PRs, branches, metrics,
  workflows, reviewers, CODEOWNERS rules and the channels it's routed to.
- **Reviewers:** request reviewers from the dashboard, with suggestions drawn
  from CODEOWNERS and who has reviewed in that repo recently. An optional rule
  assigns one automatically when nobody has been asked: code owners first, then
  the least busy person in a pool.
- **My work** (in the menu under your name, which shows how many reviews are waiting on you): what's waiting on you: reviews asked of you (oldest first),
  your open PRs and what each needs next, your branches to tidy, and your
  snoozed reminders. People who sign in with GitHub land on it. Local accounts
  can give their GitHub login on the page itself.
- **Workflows:** every GitHub Actions workflow, day by day over the last 7, 14
  or 30 days: when each scheduled workflow was due (from its cron), whether it
  ran, and whether its runs passed or failed. It flags workflows failing on the
  default branch, scheduled runs that never started, and scheduled workflows
  GitHub has switched off after 60 days without activity in the repo.
- **Metrics:** time to first review, time to merge, merges per week, both by
  PR size, and daily trends of PRs needing review and stale branches, per repo or overall.
- **MVP:** for the last 7 days (or the 7 before), who submitted the most
  reviews, merged the most PRs, deleted the most branches and opened the most
  PRs, with team totals. An optional weekly post sends the MVPs to Teams and Slack.
- **Notifications:** posted to Microsoft Teams (Workflows webhooks, Adaptive Cards) and
  Slack (incoming webhooks, Block Kit) when PRs open, are reviewed, merge or
  close, and when a workflow starts failing (or passes again), misses a
  scheduled run, or is switched off. Each person can also get a morning
  message in Slack with what's waiting on them. Reminders go out in working hours, plus a weekly digest of branches to tidy up.

Branchwarden only reads branches. It never pushes and never deletes. Its one
write to GitHub is requesting (or withdrawing) reviewers.

The look follows [Skyfarer](https://github.com/chrisnock-personal/skyfarer).

For an illustrated tour of every feature, see the
[project overview](https://github.com/chrisnock-personal/project-overviews/blob/main/branchwarden/README.md).

![The Branchwarden overview: summary tiles, a card per repository, the pull requests that need attention, delivery channel health and the scan log](docs/images/hero.png)

## Quick start

```bash
cp .env.example .env              # set POSTGRES_PASSWORD (compose won't start without it)
podman-compose pull               # or docker compose; the published image, ghcr.io/chrisnock-personal/branchwarden
podman-compose up -d              # starts Postgres, the worker and the dashboard
podman-compose logs dashboard     # the one-time setup code, and the certificate's fingerprint
```

Open `https://<host>:8443`. The certificate is self-signed until you upload
your own, and the fingerprint is in the log so you can check it. Enter the
setup code and create the first admin. Then, under **Settings › GitHub**:

1. Paste a token, or the GitHub App's ID and private key (see below).
2. Add the organisations and accounts to scan.

The first scan of each account records what's there without sending messages.
It also imports the pull requests merged or closed in the last 180 days, so
Metrics and MVP have numbers from the start (the daily trend charts and branch
prunes can't be imported: they start when Branchwarden does).
After that, scans pick up new PRs, reviews, merges and closes.

**Trying it without GitHub:** after creating the first admin, load made-up
activity for a "demo-org" organisation: six repositories, eight people, open
PRs in every state, five weeks of merges, reviews, branch deletions, daily
snapshots and workflow runs.

```bash
podman-compose exec worker python demo.py            # load it
podman-compose exec worker python demo.py --remove   # take it out, before connecting GitHub
```

It only loads into an empty Branchwarden, with no token or App set up. While
it's there, every page says so, nothing is scanned or sent, and connecting
GitHub is refused until it's removed.

**Updating:** `podman-compose pull && podman-compose up -d`. From an older
install, especially one built on the server (0.1.0), follow
[UPGRADING.md](UPGRADING.md): back up, switch to the published image, add the
Actions permission, and how to go back. Each release is
published for amd64 and arm64 as `:<version>`, `:<major>.<minor>` and
`:latest`. To stay on one, set `BRANCHWARDEN_VERSION` in `.env` (for example
`0.2`, which gets that version's fixes). Releases and what changed are in
[CHANGELOG.md](CHANGELOG.md).

**Building it yourself:** `podman-compose build` makes the image from this
checkout under the same name, and `up -d` then runs it.

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
  rescanned within seconds. Reading a repo takes about three GraphQL queries:
  open PRs (with reviews, checks and changed files), branches (each with its
  newest PR and how far it is ahead of or behind the default branch), and its
  workflows. Comparing each scan with the last gives the events.
  - **Unchanged repos are skipped.** The query that lists an account's repos
    (100 per request) also reads each one's last push, its number of open PRs
    and when the newest of them changed. A scheduled scan skips the PR and
    branch queries for a repo where none of that changed, but reads every repo
    in full at least once an hour (CI results don't show in those three). Most
    repos in a large organisation are quiet, so most scans read only a few.
  - **Four repos at a time,** each on its own connection. When GitHub answers
    with a secondary rate limit, the worker waits as long as GitHub asks (up
    to two minutes) and tries again.
  - *Settings › Deliveries* lists each scan's time, the repos it read and
    skipped, and the requests it made.
- **The dashboard** (`dashboard.py`, `admin.py`) serves the pages and the
  webhook endpoint. A webhook only queues a rescan of its repo, so a forged or replayed event
  can't change what Branchwarden shows. Events without a valid
  `X-Hub-Signature-256` are refused anyway.
- **Messages** are queued per channel in Postgres and retried with back-off
  (1 min, 5 min, 15 min, 1 h, 3 h, 6 h) on 429 or 5xx responses. Other 4xx
  responses are permanent failures, shown under *Settings › Deliveries*.

## Connecting to GitHub

You can use both connections: per account, Branchwarden uses the App where it's
installed and otherwise the token (or only one, set per account).

**A fine-grained personal access token** is the quickest. Give it:
- Pull requests: read and write. The write access is only for requesting reviewers.
- Contents: read.
- Metadata: read.
- Members: read, for organisations, so the reviewer picker can list people and teams.
- Actions: read, for the Workflows page. Without it, PRs and branches are
  still scanned and the Workflows page lists the repos it can't read.

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
   - Actions: read
2. Subscribe it to these events: *Pull request*, *Pull request review*, *Check suite*, *Push*, *Create*, *Delete*, *Workflow run*.
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
  - **Workflow failing or fixed:** once when a workflow goes from passing to
    failing, and again when it passes. For scheduled runs only, scheduled runs
    and the default branch (the default), or every branch. It can @mention who
    started the run.
  - **Scheduled run missed or workflow switched off:** a cron schedule that
    didn't start in time (90 minutes, or longer where GitHub usually runs that
    workflow late), or a scheduled workflow that GitHub, or someone, switched off.

  PR messages carry buttons for the PR, for Branchwarden, and for up to two
  tickets named in it (see *Pull request settings* below).
- **Reminder rules:**
  - No reviewer assigned (after 4 working hours)
  - Waiting for review (24 working hours)
  - Changes requested with no new commits (3 days)
  - Approved but not merged (2 days, off by default)
  - Stalled: no commits, reviews or comments (7 days, off by default)

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
- **Morning message** (off by default): on working days at a set time, a
  message to each person who has opted in on My work, with the reviews waiting
  on them (oldest first) and their PRs that need something. Nothing is sent
  when nothing's waiting. Each person picks where it goes:
  - **Slack:** a direct message from a bot. It needs a Slack bot token with
    `chat:write` and `im:write` (set in the rule) and the person's Slack
    member ID under *Settings › People*.
  - **Teams:** their own chat. Each person makes a Teams Workflows flow ("Post
    to a chat when a webhook request is received", posting to their chat with
    Workflows) and pastes its URL on My work, where it's stored encrypted. No
    Teams bot or admin setup is needed.

  "Send me one now" on My work checks it.
- **Weekly MVP post** (off by default): each MVP board's winner and the next
  two, at a set day and time (Fridays 16:00 by default).
  - **Reviews** counts PRs reviewed for other people, not every comment.
  - **Merges** counts who pressed merge.
  - **PRs raised** counts PRs opened.
  - **Branch prunes** counts branch deletions. Who deleted a branch is only known
    from the GitHub App's `delete` webhook. A deletion seen only by a scan counts
    in the team total, but for nobody by name.
  - Bots are left out of every board.

**Pull request settings** (*Settings › GitHub › Pull requests*):
- **Big PRs:** from 500 lines changed (added plus removed) or 30 files, by
  default. They get a badge and a filter, and Metrics compares review and
  merge times by size.
- **Stalled PRs:** no commits, reviews or comments for 7 days. Drafts never
  stall. They get a badge, a filter and the reminder above.
- **Ticket links:** a URL with `{key}`, such as
  `https://company.atlassian.net/browse/{key}` or
  `https://linear.app/team/issue/{key}`, and a pattern for the keys (by
  default `ABC-123` style). Keys in PR titles, branch names and descriptions
  are linked on the Pull requests and Branches pages and in messages, and
  "No ticket" filters PRs without one. Only the keys are kept from a
  description, never its text. With no link set, tickets aren't looked for.

**Settings › People** matches GitHub logins to a Teams user principal name and
a Slack member ID, so @mentions reach the right person. Anyone without a match
appears as a bold name.

## API

Everything the dashboard shows or does is also a JSON API under `/api/v1`, for
scripts, CI and AI agents (the [reference](docs/api.md), and the OpenAPI
document at `/api/v1/openapi.json`).

- **Tokens:** each person makes their own under their name › *API tokens*,
  read-only or read and write, with an optional expiry. A token acts with
  its owner's role as it is at each request, so changing someone's role
  changes what their tokens can do. Admins can also make **service tokens**
  with a role of their own (*Settings › Users and access*). A token is shown
  once; only its hash is kept, with when it was last used.
  For people who **sign in with GitHub**, whose role comes from their GitHub
  teams at each sign-in, tokens only work while they've signed in within the
  last 30 days, and a refused sign-in (no longer in any role list) revokes
  all their tokens: leaving the teams ends API access too.
- **The same rules as the dashboard:** one check decides what a viewer, member
  or admin may do in both, and every change goes through the same code, with
  the same validation and the same entry in the change history, naming the
  token. CI calls every endpoint as each role and checks it's allowed or
  refused exactly as its page is.
- **Safe by default:** bearer tokens only (cookies don't work on the API, so
  there's nothing for a cross-site form to use), HTTPS only, a rate limit per
  token and on bad tokens per address, and secrets stay write-only, as in the
  dashboard. Requesting
  reviewers takes `dry_run` to see what would be sent to GitHub.

```bash
curl -s -H "Authorization: Bearer $TOKEN" https://branchwarden.example.com/api/v1/my-work
curl -s -H "Authorization: Bearer $TOKEN" 'https://branchwarden.example.com/api/v1/pulls?filter=stalled&repo=api-*'
```

## Prometheus

`/metrics/prometheus` serves the headline numbers for Grafana and alerting,
labelled by repo:
- **Pull requests:** open PRs by state, and those that are big, stalled,
  failing CI, conflicting, without a ticket or needing review.
- **Waiting:** the longest wait per repo, reviews waiting per reviewer and
  their oldest request.
- **Branches:** by status: stale, left after a merge or close, pushed after merge.
- **Speed:** time to first review and to merge, as summaries (median and
  90th percentile) over 30 days.
- **Repos:** failing workflows and repo health flags.
- **The worker:** the last full scan's time, duration, error, repos read and
  skipped, and GitHub budget left; whether scans have stopped; and messages
  pending, failed and sent in the last day.

It's scraped with an API token, so there's no separate secret: make a
read-only **service token** with the viewer role under *Settings › Users and
access*. It's worked out at most every 15 seconds, however often it's scraped.

```yaml
scrape_configs:
  - job_name: branchwarden
    scheme: https
    metrics_path: /metrics/prometheus
    authorization: { credentials: bwt_… }
    tls_config: { ca_file: branchwarden.pem }   # or insecure_skip_verify while it's self-signed
    static_configs: [{ targets: ["branchwarden.example.com:8443"] }]
```

For example, alert on `branchwarden_scans_stopped == 1`, on
`branchwarden_messages{status="failed"} > 0`, or when
`branchwarden_reviews_waiting` stays high for someone.

## Command line

`branchwarden`, in [`cli/`](cli/README.md), wraps the API for the terminal,
scripts and AI agents: `me`, `prs list --filter stalled`, `prs reviewers add
412 --user sasha --dry-run`, `branches list --status stale`, `workflows
--problems`, `admin settings set scanning STALE_DAYS=45`, and `api` for
anything else.

```bash
pipx install "git+https://github.com/chrisnock-personal/branchwarden#subdirectory=cli"
branchwarden login --url https://branchwarden.example.com
branchwarden me
```

- **The token** lives in the OS keychain, or in `BRANCHWARDEN_TOKEN` for CI.
- **A self-signed certificate** is pinned by its fingerprint at login.
- **Output:** `--json` gives the API's own JSON and `--quiet` just ids.
- **MCP:** `branchwarden mcp` serves the same as tools for AI agents (Claude
  Code, Claude Desktop): read-only tools marked as such, reviewer requests a
  dry run unless asked, and a read-only token keeps an agent to looking.
  `branchwarden mcp --http` serves them over HTTP for agents on other
  machines. Each client sends its own API token, and the server keeps none (see
  [cli/README.md](cli/README.md#over-http-for-agents-on-other-machines)). To run
  it next to the app, see [The MCP service](#the-mcp-service-for-ai-agents-on-other-machines).
- **Exit codes** say what happened, so scripts can act on them.

### The MCP service, for AI agents on other machines

The image includes the CLI, so the HTTP MCP server can run as one more
container next to the app. It's optional, and it starts only when asked:

```bash
podman-compose --profile mcp up -d
```

- **Address:** it listens on 8765 (`MCP_EXPOSE` in `.env`) at
  `https://<your server>:8765/mcp`.
- **Certificate:** it uses the dashboard's own certificate, so agents trust
  the same fingerprint as the CLI. When the certificate is replaced under
  *Settings › TLS*, it restarts itself with the new one.
- **Tokens:** it keeps none. Each agent sends its own API token, checked with
  Branchwarden, and gets what that token allows.
- **Host names:** set `MCP_ALLOWED_HOSTS` (comma-separated `name:port`) to
  answer only requests addressed to those names.
- **Address for agents:** set `MCP_PUBLIC_URL` in `.env` to the full address
  agents reach, e.g. `https://branchwarden.example.com:8765/mcp`. The *API
  tokens* page then shows the HTTP command for that address. The dashboard
  can't detect the service, so this is an option to use, not a health check.
- **Behind a reverse proxy** (`BRANCHWARDEN_TLS=off`): it follows the
  dashboard and speaks plain HTTP on 8765, for the proxy to put HTTPS in front
  of it. Publish it to the proxy only, e.g. `MCP_EXPOSE=127.0.0.1:8765`. With
  quadlets, uncomment `BRANCHWARDEN_TLS=off` in `branchwarden-mcp.container`.

Connecting Claude Code, with a token from *API tokens*:

```bash
claude mcp add --transport http branchwarden https://branchwarden.example.com:8765/mcp \
  --header "Authorization: Bearer bwt_…"
```

With Branchwarden's own self-signed certificate, the agent's machine has to
trust it. For Claude Code that's `NODE_EXTRA_CA_CERTS=/path/to/cert.pem` (the
certificate from *Settings › TLS*).

## Roles

| Role | Can |
|---|---|
| Admin | Everything: GitHub connections, channels and rules, people, the TLS certificate, users |
| Member | What viewers can, plus request reviewers, snooze reminders, ignore branches and start a scan |
| Viewer | Read the overview, pull requests, branches, repositories, workflows, metrics and MVP, and their own My work |

Everyone can make their own API tokens, which act with their role (or less, if read-only).

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
- **CI** runs the tests, `pip-audit` and `gitleaks` on every push (see [CI and releases](#ci-and-releases)).

**Retention:** closed PRs are kept for 365 days, for the metrics. Messages,
workflow runs and scans are kept for 90 days. Both are set under *Settings › GitHub*, and older
records are removed once an hour.

**Reporting a vulnerability:** privately, through the repository's Security
tab. See [SECURITY.md](SECURITY.md).

**Not covered by Branchwarden:** the Postgres volume itself isn't encrypted.
Run it on an encrypted disk if PR titles and branch names are sensitive.
Backups are encrypted (see [Backups](#backups)).

## Backups

`deploy/backup.sh` writes one file holding everything needed to rebuild
Branchwarden, encrypted with [age](https://age-encryption.org):
- **the database:** a `pg_dump`,
- **the `keys` volume:** without it, the stored tokens and webhook URLs in the
  dump can't be read, so the two always travel together,
- **the `certs` volume:** your TLS certificate,
- **`.env`:** the database password and settings.

It's encrypted to public keys you choose, so the server can make backups but
can't read them: keep the private key somewhere else.

```bash
age-keygen -o branchwarden-backup.key           # once, on your own machine; note the public key (age1…)
deploy/backup.sh -r age1… -o /backups --keep 14  # from the directory with docker-compose.yml
```

To restore, on the same host or a fresh one with the repo (or just the compose
file and `deploy/`) in place:

```bash
deploy/restore.sh -i branchwarden-backup.key /backups/branchwarden-20261115-020000.tar.age
```

It puts back `.env` if there isn't one, the volumes and the database, then
starts Branchwarden. It won't replace a database that already has data unless
you add `--force`, and it checks that before stopping anything. Volumes it
has to create aren't labelled as compose's, so a later `down -v` leaves them:
remove them with `podman volume rm` when you mean to.

- **Other setups:** `ENGINE=docker COMPOSE="docker compose"` for Docker; for
  the quadlets, `--db-container branchwarden-db` and, when restoring,
  `--services "branchwarden-worker branchwarden-dashboard"`.
- **Without age installed:** `age` is in Fedora, Debian, Ubuntu and Homebrew.
  The scripts call whatever `AGE` names, so a container works too.
- **Nightly, with systemd** (`~/.config/systemd/user/branchwarden-backup.service`
  and `.timer`):

  ```ini
  # branchwarden-backup.service
  [Service]
  Type=oneshot
  WorkingDirectory=%h/branchwarden
  ExecStart=%h/branchwarden/deploy/backup.sh -r age1… -o %h/backups --keep 14

  # branchwarden-backup.timer
  [Timer]
  OnCalendar=*-*-* 02:00
  Persistent=true
  [Install]
  WantedBy=timers.target
  ```

  Then `systemctl --user enable --now branchwarden-backup.timer`.

CI backs up the real stack with the demo data and a sealed secret, destroys
everything, restores it and checks it all came back, on every push
(`tests/backup_roundtrip.sh`).

## Health checks

- **Containers:** compose checks the dashboard with `/healthz` (which also
  checks the database) and the worker with its heartbeat. The worker touches a
  file every few seconds, and after each repo while a scan runs, so a long scan
  isn't mistaken for a stuck one. The MCP service is up when it answers 401
  (it wants a token). They're `python healthcheck.py dashboard|worker|mcp`.
- **On the dashboard:** when no full scan has finished for more than twice the
  scan interval (and none is running), every page shows a banner saying that
  pull requests, reminders and messages are on hold until the worker runs again.

## Running as a service (Podman quadlets)

`deploy/quadlet/` has quadlet files to run Branchwarden as systemd user services
instead of compose: the database, the worker, the dashboard, their network,
and volumes under the same names compose uses, so a compose install moves over
with its data and key.

```bash
mkdir -p ~/.config/containers/systemd/branchwarden ~/.config/branchwarden
# Everything but the optional MCP service: copy branchwarden-mcp.container too if you want it.
cp deploy/quadlet/branchwarden.network deploy/quadlet/*.volume deploy/quadlet/branchwarden-{db,worker,dashboard}.container \
   ~/.config/containers/systemd/branchwarden/
install -m 600 .env ~/.config/branchwarden/branchwarden.env   # the same settings as compose's .env
podman-compose down                                           # if moving from compose (volumes are kept)
systemctl --user daemon-reload
systemctl --user start branchwarden-dashboard                 # starts the database and the worker too
loginctl enable-linger "$USER"                                # keep them running after you log out
```

They start at boot (`WantedBy=default.target`). The dashboard is published on
8443; change `PublishPort` in `branchwarden-dashboard.container` for another
port. Uncomment `AutoUpdate=registry` to let `podman auto-update` install new
releases of the image tag. `branchwarden-mcp.container` is the optional MCP
service, on 8765. Copy it as well and run `systemctl --user start
branchwarden-mcp` to use it.

Volumes shared by two containers are mounted with `:z`, not `:Z`. On a host
with SELinux enforcing, `:Z` makes a volume private to one container, and the
other loses access to it.

## Configuration (.env)

| Variable | Default | |
|---|---|---|
| `POSTGRES_PASSWORD` | required | |
| `BRANCHWARDEN_SECRET_KEY` | generated into the `keys` volume | 32+ characters if you set it |
| `BRANCHWARDEN_SECRET_KEY_PREVIOUS` | | for rotation |
| `BRANCHWARDEN_VERSION` | `latest` | the published image to run, e.g. `0.2` |
| `DASHBOARD_EXPOSE` | `8443` | where compose publishes HTTPS, e.g. `127.0.0.1:8443` |
| `TLS_HOSTNAMES` | | extra names/IPs for the self-signed certificate |
| `MCP_EXPOSE`, `MCP_ALLOWED_HOSTS` | `8765`, any name | the optional HTTP MCP service |
| `MCP_PUBLIC_URL` | | its full address (`https://…:8765/mcp`), to show the HTTP command on the API tokens page |
| `BRANCHWARDEN_TLS` | `on` | `off` only behind a TLS proxy (with `BRANCHWARDEN_BEHIND_PROXY=true`) |
| `GITHUB_TARGETS`, `GITHUB_TOKEN`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_FILE`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_API_URL`, `PUBLIC_URL` | | imported on the first start only, then edited in the dashboard |

## CI and releases

Two GitHub Actions workflows, both in `.github/workflows/`:

- **`ci.yml`, on every push and pull request** ([runs](https://github.com/chrisnock-personal/branchwarden/actions/workflows/ci.yml)):
  - the unit tests: PR states, branch rules, CODEOWNERS, working hours,
    reviewer selection, cron schedules, workflow matching, repo health, PR
    size and stalls, ticket keys, message rendering, signatures,
    encryption, TLS checks and templates,
  - the integration tests, against a Postgres service container: the scanner
    end to end with a stand-in for GitHub, routing and the delivery queue,
    workflows, skipping unchanged repos, the demo data, and every page,
  - the API tests: every endpoint called as each role and with a read-only
    token, tokens' life cycle, and the main calls end to end; plus checks
    that the OpenAPI document is valid and matches the routes, and that
    `docs/api.md` is current,
  - the CLI's tests: every command against the running app, exit codes,
    and certificate pinning against a real HTTPS server,
  - the browser test, in Chromium: first-run setup, every section, a repo
    page, filters, a workflow row, the theme switch, a form and the menu,
  - `pip-audit` for known vulnerabilities in the pinned dependencies,
  - `gitleaks` for secrets anywhere in the history,
  - a backup and restore from scratch, with docker compose: back up the stack
    with the demo data and a sealed secret, destroy it, restore it, check.
- **`release.yml`, on a `vX.Y.Z` tag** ([runs](https://github.com/chrisnock-personal/branchwarden/actions/workflows/release.yml)):
  checks the tag matches `version.py`, then builds the image for amd64 and
  arm64 and publishes it to `ghcr.io/chrisnock-personal/branchwarden` as
  `:X.Y.Z`, `:X.Y` and `:latest`. It refuses a tag whose commit hasn't passed
  `ci.yml`.

**Dependabot** (`.github/dependabot.yml`) opens update PRs each Monday,
grouped, for the Python packages (the app's and the CLI's), the GitHub
Actions and the container's base image; CI tests each before it's merged.

The version is in `version.py`. It's shown at the foot of every page, in
`/healthz`, and in the User-Agent sent to GitHub. Each release gets a
[CHANGELOG](CHANGELOG.md) entry, a `vX.Y.Z` tag and a GitHub release. A
release is tagged after CI has passed on its commit; `release.yml` checks.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # the pure logic, rendering, crypto and TLS checks; no database needed
pip-audit -r requirements.txt
```

With a throwaway Postgres, the integration tests also run: the scanner end to
end against a stand-in for GitHub (first-scan silence, then opened, ready,
review, merged and closed events, and branch bookkeeping), skipping unchanged
repos, routing and the delivery queue (sent, retried with back-off, failed,
switched-off channels), workflows, the demo data, and every page rendering.
The browser test adds Chromium: first-run setup, every section from the
header, a repo page, filters, a workflow row, the theme switch, a form and
the menu. [CI](#ci-and-releases) runs both on every push.

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/branchwarden_test python -m pytest -q
python -m playwright install chromium
BROWSER_TESTS=1 TEST_DATABASE_URL=… python -m pytest -q tests/test_browser.py
```

The test database is emptied for every test: never point it at real data.

The files:

| File | What it does |
|---|---|
| `analysis.py` | PR states, branch statuses, CODEOWNERS, PR size and stalls, ticket keys, picking reviewers and working-hours maths. No I/O |
| `scanner.py` | The GraphQL queries, skipping unchanged repos, scanning in parallel, importing past PRs, and turning a scan into rows and events |
| `data.py` | Reading PRs and branches back with their state; whether scans have stopped |
| `workflows.py` | GitHub Actions: workflow files, cron schedules, runs, missed runs, and the Workflows page |
| `repos.py`, `repohealth.py` | The Repositories pages, and the repo settings they flag |
| `rules.py` | Events, auto-assign, reminders and the digest |
| `metrics.py`, `mvp.py`, `housekeeping.py` | The metrics and MVP pages; retention and the daily snapshot |
| `ghlogin.py` | Sign in with GitHub |
| `notify.py` | Routing, rendering for Teams and Slack, delivery and Slack threads |
| `github.py` | App and token clients |
| `settings.py` | Encrypted settings, users and history |
| `tls.py` | Certificates |
| `worker.py` | The loop |
| `access.py` | Who may do what: one rule for the dashboard and the API |
| `actions.py` | Every change, shared by the dashboard's forms and the API |
| `api.py`, `apitokens.py` | The JSON API and its OpenAPI document; API tokens |
| `cli/` | The `branchwarden` command line, a separate package |
| `prom.py` | The Prometheus endpoint |
| `demo.py` | Loading and removing the demo data |
| `healthcheck.py` | Container health checks, and the worker's heartbeat |
| `mcp_service.py` | The optional mcp service: `branchwarden mcp --http` next to the app |
| `deploy/quadlet/` | Podman quadlet files, to run as systemd services |
| `deploy/backup.sh`, `deploy/restore.sh` | Encrypted backups and restores |
| `dashboard.py`, `admin.py`, `templates/` | The web UI |

## Licence

[MIT](LICENSE). Branchwarden is fully open source.
