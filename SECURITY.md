# Security policy

Branchwarden holds credentials that matter: a GitHub token or a GitHub App's
private key, a webhook secret, and Teams and Slack webhook URLs and bot tokens.
Please report anything that could expose them, or let someone act without
the right role, privately.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: the repository's **Security**
tab › **Report a vulnerability**
(<https://github.com/chrisnock-personal/branchwarden/security/advisories/new>).
Only the maintainer sees the report.

Please don't open a public issue, pull request or discussion for it.

Useful to include: the version (shown at the foot of every page and in
`/healthz`), how it's deployed, and steps to reproduce. You'll get a reply
within a week. Once a fix is released, the advisory is published with credit,
unless you'd rather not be named.

## Supported versions

Fixes go into the latest release only. Releases are published as images at
`ghcr.io/chrisnock-personal/branchwarden` and listed in
[CHANGELOG.md](CHANGELOG.md). Update with `podman-compose pull && podman-compose up -d`.

| Version | Supported |
|---|---|
| Latest release | Yes |
| Anything older | No: update to the latest |

## How it's built

In short (details in the [README's Security section](README.md#security)):

- **HTTPS only**, TLS 1.2 and up, with a generated certificate until you upload
  your own. HSTS, and `Secure`, `HttpOnly`, `SameSite=Lax` cookies.
- **Secrets sealed at rest** with AES-256-GCM. The key lives in its own volume,
  away from the database, can be rotated, and no secret is shown again once saved.
- **Fail closed:** no default accounts (the first admin needs a one-time code
  from the server's log), no default database password, webhooks without a
  valid signature refused, CSRF tokens on every form, throttled sign-ins.
- **Narrow reach:** Postgres isn't published, the containers run as an
  unprivileged user, and Branchwarden's only write to GitHub is requesting reviewers.
- **CI** runs the tests, `pip-audit` and `gitleaks` on every push.

Out of scope: the Postgres volume isn't encrypted by Branchwarden (put it on
an encrypted disk), and the security of the host and of GitHub, Teams and Slack themselves.
