# branchwarden CLI

The command line for [Branchwarden](https://github.com/chrisnock-personal/branchwarden)'s
API: pull requests and who they're waiting on, reviews waiting on you, branches
to tidy, repositories, workflows and metrics. Made for people at a terminal,
for scripts and CI, and for AI agents.

## Install

```bash
pipx install "git+https://github.com/chrisnock-personal/branchwarden#subdirectory=cli"
```

Python 3.10 or later. It talks only to Branchwarden's API, and uses the
system keychain (through `keyring`) for the token.

## Sign in

Make a token in Branchwarden (your name › *API tokens*; read-only is enough
for looking), then:

```bash
branchwarden login --url https://branchwarden.example.com
```

- **The token** is asked for without being shown, then kept in the OS
  keychain, never in a file. Scripts and CI set `BRANCHWARDEN_URL` and
  `BRANCHWARDEN_TOKEN` instead, and need no login.
- **Branchwarden's own certificate:** until you upload a proper one,
  Branchwarden uses a self-signed certificate. `login` then shows its SHA-256
  fingerprint to compare with *Settings › TLS* and, once you agree, pins it:
  any other certificate at that address is refused. In scripts, pass
  `--fingerprint` (or `BRANCHWARDEN_FINGERPRINT`).

## Commands

| | |
|---|---|
| `branchwarden me` | Reviews waiting on you, your PRs and what each needs, your branches |
| `branchwarden prs list [--filter review\|stalled\|big\|ci…] [--repo 'api-*'] [--author] [--reviewer]` | Open PRs, longest waiting first |
| `branchwarden prs show ID` | One PR in full |
| `branchwarden prs reviewers add\|remove ID --user LOGIN --team ORG/TEAM [--notify] [--dry-run]` | Request or withdraw reviewers. This writes to GitHub: it asks first unless `--yes`, and `--dry-run` only shows what would change |
| `branchwarden prs snooze ID --days 1\|3\|7` (`--off` to undo) | Put a PR's reminders off |
| `branchwarden branches list [--status stale\|left\|all]` | Branches to tidy |
| `branchwarden branches ignore ID` (`--undo --repo R --branch B`) | Stop flagging a branch, or flag it again |
| `branchwarden repos [OWNER/NAME]` | Repositories and their health, or one in full |
| `branchwarden workflows [--problems]` | GitHub Actions: pass rates, failing, missed schedules |
| `branchwarden metrics`, `branchwarden mvp` | Review and merge times; the week's MVPs |
| `branchwarden scan` | Start a full scan |
| `branchwarden tokens list\|create\|revoke` | Your API tokens |
| `branchwarden admin settings\|rules\|channels\|people\|users\|targets\|scans\|history [list]` | For admins; `admin settings set scanning STALE_DAYS=45`, `admin rules set stalled after=5` |
| `branchwarden api METHOD /path [--data JSON]` | Any endpoint, raw JSON ([the reference](../docs/api.md)) |

## Output, for people and machines

- **For people:** tables by default, trimmed to the terminal's width.
- **`--json`:** the API's JSON, unchanged: stable, and documented in
  [docs/api.md](../docs/api.md) and `/api/v1/openapi.json`.
- **`--quiet`:** only ids, one per line, for piping.
- **No prompts** with `--yes`, or when there's no terminal.
- **Exit codes:**

  | Code | Meaning |
  |---|---|
  | 0 | Done |
  | 2 | The command line was wrong |
  | 3 | Not signed in, or the role or token doesn't allow it |
  | 4 | Not found |
  | 5 | Refused: bad input or a conflict, and the message says why |
  | 6 | The server, GitHub or the network failed |
  | 7 | Rate limited |
  | 1 | Anything else |

- **Versions:** the CLI's version matches the server's, and it warns when the
  two differ.

## For AI agents

`branchwarden --help` and each command's `--help` say everything a command
needs. Give an agent a **read-only** token unless it has to change things: then
it can look but not request reviewers, snooze or scan. Even with a read-write
token, `prs reviewers … --dry-run` shows what would be sent to GitHub without
sending it.
