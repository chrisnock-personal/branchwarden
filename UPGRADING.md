# Upgrading Branchwarden

How to move an install to a newer release, keeping its data, its stored secrets
and a way back. Written for podman-compose; with Docker, use `docker compose`
and `docker` instead of `podman-compose` and `podman`.

The steps were tested by upgrading a v0.1.0 install (built locally, with
users, PRs and stored secrets) and rolling it back:
- **To v0.6.1:** that test found that restoring an older backup over the
  upgraded database failed. 0.6.2 fixed it, and CI now restores an older
  backup over a newer database on every push.
- **To v0.8.0, on 2026-11-24:** every step as written, then the way back,
  with no changes needed. 0.8.1 adds bug fixes and one database column on
  top, so the steps are the same.

Run everything from the folder with `docker-compose.yml` and `.env`.

## What's different since 0.1.0

- **A published image.** Releases come from `ghcr.io/chrisnock-personal/branchwarden`
  instead of being built on the server. `BRANCHWARDEN_VERSION` in `.env`
  picks the release.
- **GitHub needs one more permission: Actions: read**, for the Workflows page
  (step 7). Without it, everything else works and the page lists the repos it
  can't read.
- **The first scan after upgrading does more:**
  - It imports the PRs merged or closed in the last 180 days, so Metrics and
    MVP have numbers.
  - It reads each repo's settings for the Repositories page.
  - It reads the workflow history.

  Expect it to take longer and use more of GitHub's API budget than usual.
  Later scans skip unchanged repos.
- **Nothing to change in `.env`** beyond the version, and the database is
  brought up to date on start: there's no migration to run by hand.

## 1. Note where you are

```bash
curl -sk https://localhost:8443/healthz        # {"ok":true,"version":"0.1.0"}
podman images | grep branchwarden              # localhost/branchwarden:latest if you built it
```

## 2. Get the new version's files

Your `.env` stays as it is.

With a git clone:

```bash
git fetch --tags
git checkout v0.8.1
```

Without git, download the release and copy `docker-compose.yml` and the
`deploy/` folder over yours:

```bash
curl -L -o bw.tar.gz https://github.com/chrisnock-personal/branchwarden/archive/refs/tags/v0.8.1.tar.gz
tar -xzf bw.tar.gz && cp branchwarden-0.8.1/docker-compose.yml . && cp -r branchwarden-0.8.1/deploy .
```

The running containers aren't touched yet.

## 3. Back up

`deploy/backup.sh` (from the new files) works with the old containers. It
saves the database, the encryption key, the TLS certificate and `.env` in one
encrypted file (see the README's *Backups*).

```bash
sudo dnf install age                           # or apt install age, brew install age
age-keygen -o branchwarden-backup.key          # on your own computer, not the server: note the age1… line
deploy/backup.sh -r age1… -o ~/branchwarden-backups
```

Keep `branchwarden-backup.key` off the server: the backup can't be read
without it. On a server without `age`, point the scripts at a container that
has it, with `AGE="podman run --rm -i … age"`.

## 4. Keep the old image

If you built Branchwarden on the server, give that image a name the upgrade
won't reuse, so you can go back to it:

```bash
podman tag localhost/branchwarden:latest localhost/branchwarden:before-upgrade
```

## 5. Switch over

```bash
echo "BRANCHWARDEN_VERSION=0.8.1" >> .env
podman-compose down                            # not "down -v": that would delete the data
podman-compose pull
podman-compose up -d
```

`down` first matters: `up -d` alone keeps the old containers running.

## 6. Check it

```bash
curl -sk https://localhost:8443/healthz        # "version":"0.8.1"
podman ps                                      # all three "(healthy)" after a minute or so
podman-compose logs worker | grep -iE "imported|scan \\("
```

- **Sign in** as usual: accounts and passwords are unchanged.
- **Stored secrets:** *Settings › GitHub* still says the token or App key is
  *Stored*, and *Settings › Channels* shows the channels. If a secret shows
  as unreadable, the `keys` volume didn't come through: restore it from the
  backup.

## 7. On GitHub

- **Token:** edit the fine-grained token and add **Actions: read**.
- **GitHub App:**
  1. Under *Permissions*, add **Actions: read**.
  2. Under *Subscribe to events*, add **Workflow run**.
  3. Have an organisation owner accept the updated permissions on the
     installation: GitHub asks them by email and in *Settings › GitHub Apps*.

Branchwarden picks the new permission up on its next scan.

## 8. New things to set up (optional)

- **API tokens and the command line:** your name › *API tokens*. The page
  shows the commands to install the CLI and connect an AI agent.
- **Prometheus:** make a read-only service token under *Settings › Users and
  access*, then follow the README's *Prometheus* section.
- **The morning message:** the rule under *Settings › Channels and
  reminders*. Each person turns it on from My work and picks Slack or Teams.
- **The MCP service**, for AI agents on other machines:
  `podman-compose --profile mcp up -d`. It listens on 8765 (see the README's
  *The MCP service*).
- **A nightly backup:** the systemd timer in the README's *Backups*.

## Going back

If something's wrong, return to the old version and the backup taken in step 3:

```bash
cp deploy/restore.sh /tmp/                     # the old version doesn't have it
podman-compose down
git checkout v0.1.0                            # or put your old docker-compose.yml back
sed -i '/^BRANCHWARDEN_VERSION=/d' .env
podman tag localhost/branchwarden:before-upgrade localhost/branchwarden:latest
/tmp/restore.sh -i branchwarden-backup.key --force ~/branchwarden-backups/branchwarden-….tar.age
```

`--force` is needed because the database already has tables. Copy the key back
to the server for this, then remove it again.

## Later upgrades

From 0.6 on, there's less to it:

1. Read the [CHANGELOG](CHANGELOG.md).
2. Back up.
3. Change `BRANCHWARDEN_VERSION` in `.env`.
4. Run `podman-compose pull && podman-compose up -d`.

Keep the previous version's number to hand, to set it back if needed.
