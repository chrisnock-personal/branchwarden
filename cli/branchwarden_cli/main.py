"""
branchwarden: the command line for Branchwarden's API.

Made for people at a terminal, for scripts, and for AI agents: every command has
complete --help, --json prints the API's own documented JSON, --quiet prints just
ids, and the exit code says what happened. Nothing prompts when --yes is given or
there's no terminal.

Exit codes:
  0  done
  1  something else went wrong
  2  the command line was wrong (see --help)
  3  not signed in, the token isn't valid, or the role doesn't allow it
  4  not found
  5  refused: the input was wrong, or it conflicts (the message says why)
  6  the server, GitHub or the network failed
  7  rate limited: try again later
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
from typing import Any, Optional
from urllib.parse import urlencode, urlsplit

from . import __version__

KEYRING_SERVICE = "branchwarden-cli"
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "branchwarden", "cli.json")
EXIT = {400: 5, 401: 3, 403: 3, 404: 4, 405: 2, 409: 5, 413: 5, 415: 5, 429: 7}


class Fail(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# ─── Settings and the token ─────────────────────────────────────────────────

def load_config() -> dict:
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG)


def keyring_get(url: str) -> Optional[str]:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, url)
    except Exception:  # no keychain on this machine (a headless server, say)
        return None


def keyring_set(url: str, token: Optional[str]) -> bool:
    try:
        import keyring
        if token:
            keyring.set_password(KEYRING_SERVICE, url, token)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, url)
            except Exception:  # nothing stored
                pass
        return True
    except Exception:
        return False


def normalise_url(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme != "https" and not os.environ.get("BRANCHWARDEN_CLI_ALLOW_HTTP"):
        raise Fail("Branchwarden's API is HTTPS only: use an https:// address.", 2)
    if not parts.hostname:
        raise Fail(f"That isn't an address: {raw}", 2)
    return f"{parts.scheme}://{parts.netloc}"


def clean_fingerprint(fp: str) -> str:
    return fp.replace(":", "").replace(" ", "").strip().upper()


def nice_fingerprint(fp: str) -> str:
    fp = clean_fingerprint(fp)
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))


# ─── Talking to the API ─────────────────────────────────────────────────────

class Client:
    """HTTPS to the API. With a pinned fingerprint, the server's certificate must
    match it exactly (for Branchwarden's own self-signed one); otherwise it must be
    trusted by the system, as for any website."""

    def __init__(self, url: str, token: Optional[str], fingerprint: Optional[str] = None, timeout: float = 30):
        self.url, self.token, self.timeout = url, token, timeout
        self.fingerprint = clean_fingerprint(fingerprint) if fingerprint else None
        self.server_version: Optional[str] = None

    def _connection(self) -> http.client.HTTPConnection:
        parts = urlsplit(self.url)
        if parts.scheme == "http":
            return http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=self.timeout)
        ctx = ssl.create_default_context()
        if self.fingerprint:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE        # checked by fingerprint below instead
        return http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=self.timeout, context=ctx)

    def request(self, method: str, path: str, body: Optional[dict] = None, params: Optional[dict] = None) -> Any:
        path = "/api/v1" + path
        if params:
            q = urlencode({k: v for k, v in params.items() if v not in (None, "", False)})
            if q:
                path += ("&" if "?" in path else "?") + q
        headers = {"Accept": "application/json", "User-Agent": f"branchwarden-cli/{__version__}"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn = self._connection()
        try:
            conn.connect()
            if self.fingerprint:
                der = conn.sock.getpeercert(binary_form=True)
                got = hashlib.sha256(der).hexdigest().upper()
                if got != self.fingerprint:
                    raise Fail(f"The server's certificate has changed: it's {nice_fingerprint(got)}, not the one you "
                               "trusted. If Branchwarden's certificate was replaced on purpose, check the new "
                               "fingerprint under Settings › TLS and run branchwarden login again.", 3)
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            self.server_version = resp.getheader("X-Branchwarden-Version")
        except ssl.SSLCertVerificationError:
            raise Fail("The server's certificate isn't trusted by this computer. If it's Branchwarden's own "
                       "(self-signed) certificate, run: branchwarden login --url URL (it shows the fingerprint "
                       "to compare with Settings › TLS).", 3) from None
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise Fail(f"Can't reach {self.url}: {exc}", 6) from None
        finally:
            conn.close()
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            raise Fail(f"{self.url} didn't answer with JSON (HTTP {resp.status}): is it Branchwarden?", 6) from None
        if resp.status >= 400:
            err = (payload.get("error") or {}) if isinstance(payload, dict) else {}
            code = EXIT.get(resp.status, 6 if resp.status >= 500 else 1)
            msg = err.get("message") or f"HTTP {resp.status}"
            if resp.status == 429 and resp.getheader("Retry-After"):
                msg += f" (try again in {resp.getheader('Retry-After')} s)"
            raise Fail(msg, code)
        return payload

    def all_pages(self, path: str, params: dict, limit: Optional[int]) -> list:
        """Follow next_cursor until there's no more, or `limit` items."""
        items: list = []
        params = dict(params, limit=min(200, limit or 200))
        while True:
            page = self.request("GET", path, params=params)
            items.extend(page.get("items", []))
            if not page.get("next_cursor") or (limit and len(items) >= limit):
                return items[:limit] if limit else items
            params["cursor"] = page["next_cursor"]


def server_fingerprint(url: str, timeout: float = 15) -> str:
    parts = urlsplit(url)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((parts.hostname, parts.port or 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=parts.hostname) as tls:
                return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest().upper()
    except OSError as exc:
        raise Fail(f"Can't reach {url}: {exc}", 6) from None


def system_trusts(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme == "http":
        return True
    try:
        with socket.create_connection((parts.hostname, parts.port or 443), timeout=15) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname):
                return True
    except ssl.SSLCertVerificationError:
        return False
    except OSError as exc:
        raise Fail(f"Can't reach {url}: {exc}", 6) from None


def client(args) -> Client:
    cfg = load_config()
    url = os.environ.get("BRANCHWARDEN_URL") or cfg.get("url")
    if not url:
        raise Fail("Not signed in. Run: branchwarden login --url https://your-branchwarden", 3)
    url = normalise_url(url)
    token = os.environ.get("BRANCHWARDEN_TOKEN") or keyring_get(url)
    if not token:
        raise Fail(f"No token for {url}. Run: branchwarden login --url {url} (or set BRANCHWARDEN_TOKEN).", 3)
    fp = os.environ.get("BRANCHWARDEN_FINGERPRINT") or (cfg.get("fingerprint") if cfg.get("url") == url else None)
    return Client(url, token, fp)


# ─── Output ─────────────────────────────────────────────────────────────────

def emit(args, data: Any, table: Optional[tuple] = None, ids: Optional[list] = None, text: Optional[str] = None):
    """--json: the API's JSON as is. --quiet: ids, one per line. Otherwise a table or a sentence."""
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=False))
    elif args.quiet:
        for i in ids or []:
            print(i)
    elif table:
        headers, rows = table
        print(render_table(headers, rows) if rows else "(none)")
    elif text is not None:
        print(text)
    else:
        print(json.dumps(data, indent=2))


def render_table(headers: list[str], rows: list[list]) -> str:
    cells = [[("" if v is None else str(v)) for v in r] for r in rows]
    width = shutil_columns()
    widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)]
    # Shrink the widest column (a title, usually) to fit the terminal.
    total = sum(widths) + 2 * (len(widths) - 1)
    if total > width and len(widths) > 1:
        widest = widths.index(max(widths))
        widths[widest] = max(12, widths[widest] - (total - width))
    clip = lambda s, w: s if len(s) <= w else s[:max(1, w - 1)] + "…"  # noqa: E731
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    for r in cells:
        lines.append("  ".join(clip(v, w).ljust(w) for v, w in zip(r, widths)).rstrip())
    return "\n".join(lines)


def shutil_columns() -> int:
    import shutil
    return shutil.get_terminal_size((120, 24)).columns


def ago(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 3600:
        return f"{max(1, s // 60)} min"
    if s < 48 * 3600:
        return f"{s // 3600} h"
    return f"{s // 86400} days"


def interactive(args) -> bool:
    return not args.yes and sys.stdin.isatty() and sys.stdout.isatty()


def confirm(args, question: str) -> bool:
    if not interactive(args):
        return True
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def warn_version(c: Client) -> None:
    sv = c.server_version
    if sv and sv.split(".")[:2] != __version__.split(".")[:2]:
        print(f"Note: this CLI is {__version__} and the server is {sv}: commands may differ. "
              "Update the one that's older.", file=sys.stderr)


# ─── Commands ───────────────────────────────────────────────────────────────

def cmd_login(args):
    url = normalise_url(args.url or os.environ.get("BRANCHWARDEN_URL") or "")
    pin = None
    if url.startswith("https://") and not system_trusts(url):
        got = server_fingerprint(url)
        if args.fingerprint:
            if clean_fingerprint(args.fingerprint) != got:
                raise Fail(f"The server's certificate fingerprint is {nice_fingerprint(got)}, not the one given.", 3)
        elif interactive(args):
            print(f"{url} uses a certificate this computer doesn't trust (Branchwarden's own self-signed one?).")
            print(f"Its SHA-256 fingerprint is:\n  {nice_fingerprint(got)}")
            print("Compare it with Settings › TLS in Branchwarden, or the dashboard's log.")
            if input("Trust this certificate for this address? [y/N] ").strip().lower() not in ("y", "yes"):
                raise Fail("Not trusted, so not signed in.", 3)
        else:
            raise Fail(f"{url}'s certificate isn't trusted by this computer. Its fingerprint is "
                       f"{nice_fingerprint(got)}: check it against Settings › TLS, then pass --fingerprint.", 3)
        pin = got
    token = args.token
    if token == "-":
        token = sys.stdin.readline().strip()
    token = token or os.environ.get("BRANCHWARDEN_TOKEN")
    if not token:
        if not interactive(args):
            raise Fail("Give a token: --token -, piped in, or BRANCHWARDEN_TOKEN. Make one in Branchwarden under "
                       "your name › API tokens.", 2)
        token = getpass.getpass("API token (from your name › API tokens; it isn't shown): ").strip()
    c = Client(url, token, pin)
    me = c.request("GET", "/me")
    cfg = {"url": url, "fingerprint": pin}
    save_config(cfg)
    stored = keyring_set(url, token) if not args.no_keychain else False
    warn_version(c)
    who = f"{me['name']} ({me['role']}{', read-only' if me['scope'] == 'read' else ''})"
    if stored:
        emit(args, me, text=f"Signed in to {url} as {who}. The token is in this computer's keychain.")
    else:
        emit(args, me, text=f"Checked: the token works for {url}, as {who}. It wasn't stored (no keychain here"
                            f"{', or --no-keychain' if args.no_keychain else ''}): set BRANCHWARDEN_TOKEN for each run.")


def cmd_logout(args):
    cfg = load_config()
    if cfg.get("url"):
        keyring_set(cfg["url"], None)
    if os.path.exists(CONFIG):
        os.remove(CONFIG)
    emit(args, {"ok": True}, text="Signed out: the token is removed from the keychain (revoke it in Branchwarden too, "
                                  "if it shouldn't work anywhere).")


def cmd_whoami(args):
    c = client(args)
    me = c.request("GET", "/me")
    warn_version(c)
    emit(args, me, text=f"{me['name']}, {me['role']}{' (read-only token)' if me['scope'] == 'read' else ''}, "
                        f"at {c.url}, token {me['token']['prefix']}… (server {me['version']}, CLI {__version__})")


def pr_rows(items):
    return [[p["id"], f"{p['repo'].split('/')[-1]} #{p['number']}", p["title"], p["state_label"]
             + (" · big" if p.get("big") else "") + (" · stalled" if p.get("stalled") else "")
             + (" · CI failing" if "ci" in p["filters"] else ""), p["waiting_on"], ago(p["waiting_seconds"])]
            for p in items]


PR_HEAD = ["ID", "PR", "TITLE", "STATE", "WAITING ON", "FOR"]


def cmd_me(args):
    w = client(args).request("GET", "/my-work")
    if args.json or args.quiet:
        return emit(args, w, ids=[p["id"] for p in w["reviews_waiting"] + w["my_pulls"]])
    print(f"Reviews waiting on {w['login']}: {len(w['reviews_waiting'])}")
    if w["reviews_waiting"]:
        print(render_table(PR_HEAD, pr_rows(w["reviews_waiting"])))
    print(f"\nYour pull requests: {len(w['my_pulls'])}")
    if w["my_pulls"]:
        print(render_table(["ID", "PR", "TITLE", "NEXT STEP", "FOR"],
                           [[p["id"], f"{p['repo'].split('/')[-1]} #{p['number']}", p["title"],
                             p["next_step"] or f"waiting on {p['waiting_on']}", ago(p["waiting_seconds"])]
                            for p in w["my_pulls"]]))
    if w["my_branches"]:
        print(f"\nYour branches to tidy: {len(w['my_branches'])}")
        print(render_table(["ID", "REPO", "BRANCH", "STATUS"], [[b["id"], b["repo"].split("/")[-1], b["name"],
                                                               b["status_label"]] for b in w["my_branches"]]))


def cmd_prs_list(args):
    c = client(args)
    items = c.all_pages("/pulls", {"filter": args.filter, "repo": args.repo, "author": args.author,
                                   "reviewer": args.reviewer, "q": args.search}, args.limit)
    emit(args, items, table=(PR_HEAD, pr_rows(items)), ids=[p["id"] for p in items])


def cmd_prs_show(args):
    p = client(args).request("GET", f"/pulls/{args.id}")
    lines = [f"{p['repo']} #{p['number']}: {p['title']}", p["url"],
             f"State: {p['state_label']}{' (draft)' if p['draft'] else ''} · checks {p['ci'] or 'none'}",
             f"Waiting on {p['waiting_on']} for {ago(p['waiting_seconds'])}",
             f"Author: {p['author']} · {p['head']} → {p['base']} · +{p['additions']} −{p['deletions']} in {p['changed_files']} files",
             "Asked to review: " + (", ".join(p["requested"]["users"] + p["requested"]["teams"]) or "nobody")]
    if p["reviews"]:
        lines.append("Reviews: " + ", ".join(f"{r['login']} {r['state'].lower().replace('_', ' ')}" for r in p["reviews"]))
    if p["tickets"]:
        lines.append("Tickets: " + ", ".join(t["key"] for t in p["tickets"]))
    if p.get("snoozed"):
        lines.append(f"Reminders snoozed until {p['snoozed']['until']} by {p['snoozed']['by_name']}")
    emit(args, p, ids=[p["id"]], text="\n".join(lines))


def cmd_prs_reviewers(args):
    who = (args.user or []) + (args.team or [])
    if not who:
        raise Fail("Name at least one --user or --team.", 2)
    key = "add" if args.action == "add" else "remove"
    body = {key: who, "notify": args.notify, "dry_run": args.dry_run}
    c = client(args)
    if not args.dry_run and not confirm(args, f"This {'requests' if key == 'add' else 'withdraws'} reviewers on "
                                              f"GitHub ({', '.join(who)}) for PR {args.id}. Go ahead?"):
        raise Fail("Nothing done.", 1)
    r = c.request("POST", f"/pulls/{args.id}/reviewers", body)
    emit(args, r, text=r["message"])


def cmd_prs_snooze(args):
    c = client(args)
    if args.off:
        r = c.request("DELETE", f"/pulls/{args.id}/snooze")
    else:
        r = c.request("POST", f"/pulls/{args.id}/snooze", {"days": args.days})
    emit(args, r, text=r["message"])


def branch_rows(items):
    return [[b["id"], b["repo"].split("/")[-1], b["name"], b["status_label"], b.get("last_committer") or "",
             f"#{b['pr']['number']}" if b.get("pr") else ""] for b in items]


def cmd_branches_list(args):
    items = client(args).all_pages("/branches", {"status": args.status, "repo": args.repo, "q": args.search}, args.limit)
    emit(args, items, table=(["ID", "REPO", "BRANCH", "STATUS", "LAST COMMIT BY", "PR"], branch_rows(items)),
         ids=[b["id"] for b in items])


def cmd_branches_ignore(args):
    c = client(args)
    if args.undo:
        if not (args.repo and args.branch):
            raise Fail("To flag a branch again, give --repo owner/name and --branch NAME.", 2)
        r = c.request("DELETE", "/branches/ignored", params={"repo": args.repo, "branch": args.branch})
    else:
        if args.id is None:
            raise Fail("Give the branch's ID (from branches list).", 2)
        r = c.request("POST", f"/branches/{args.id}/ignore")
    emit(args, r, text=r["message"])


def cmd_repos(args):
    c = client(args)
    if args.repo:
        if "/" not in args.repo:
            raise Fail("Give the repository as owner/name.", 2)
        d = c.request("GET", f"/repos/{args.repo}")
        if args.json or args.quiet:
            return emit(args, d, ids=[p["id"] for p in d["pulls"]])
        print(f"{d['repo']['full_name']}  {d['repo']['url'] or ''}")
        print("Health: " + ("; ".join(f["title"] for f in d["health"]) or "nothing to flag"))
        print(f"\nOpen pull requests: {len(d['pulls'])}")
        if d["pulls"]:
            print(render_table(PR_HEAD, pr_rows(d["pulls"])))
        print(f"\nStale branches: {len(d['stale_branches'])} · left after a PR: {len(d['left_after_pr'])}")
        print("Reviewers here: " + (", ".join(f"{r['login']} ({r['reviewed']})" for r in d["reviewers"]) or "none yet"))
        print("Messages go to: " + (", ".join(f"{x['kind']} {x['name']}" for x in d["routing"]) or "no channel"))
        return None
    items = c.request("GET", "/repos")["items"]
    emit(args, items, ids=[r["full_name"] for r in items], table=(
        ["REPO", "OPEN", "REVIEW", "STALE", "LEFT", "WF FAILING", "HEALTH"],
        [[r["full_name"], r["open"], r["review"], r["stale"], r["left"], r["failing"],
          "; ".join(f["title"] for f in r["flags"]) or "ok"] for r in items]))


def cmd_workflows(args):
    w = client(args).request("GET", "/workflows", params={"days": args.days, "repo": args.repo})
    items = [x for x in w["items"] if not args.problems or x["problems"]]
    emit(args, {**w, "items": items}, ids=[x["id"] for x in items], table=(
        ["REPO", "WORKFLOW", "RUNS", "PASSED", "FAILING SINCE", "MISSED", "STATE"],
        [[x["repo"].split("/")[-1], x["name"], x["runs"], f"{x['pass_rate']}%" if x["pass_rate"] is not None else "",
          (x["failing_since"] or "")[:10], len(x["missed"]) or "", x["state"]] for x in items]))


def cmd_metrics(args):
    m = client(args).request("GET", "/metrics", params={"days": args.days, "repo": args.repo})
    if args.json or args.quiet:
        return emit(args, m)
    print(render_table(["MEASURE", "VALUE", "NOTE"], [[t["label"], t["value"], t["note"]] for t in m["tiles"]]))
    print("\nBy size:")
    print(render_table(["LINES", "REVIEWED", "FIRST REVIEW", "MERGED", "TO MERGE"],
                       [[b["band"], b["reviewed"], b["first"], b["merged"], b["merge"]] for b in m["by_size"]]))
    return None


def cmd_mvp(args):
    r = client(args).request("GET", "/mvp", params={"period": "prev" if args.prev else "this"})
    if args.json or args.quiet:
        return emit(args, r)
    for b in r["boards"]:
        top = ", ".join(f"{x['login']} {x['count']}" for x in b["ranking"][:3]) or "nobody yet"
        print(f"{b['title']}: {top}")
    return None


def cmd_scan(args):
    r = client(args).request("POST", "/scan")
    emit(args, r, text=r["message"])


def cmd_tokens(args):
    c = client(args)
    if args.action == "list":
        items = c.request("GET", "/tokens")["items"]
        return emit(args, items, ids=[t["id"] for t in items], table=(
            ["ID", "NAME", "TOKEN", "ACCESS", "LAST USED", "EXPIRES"],
            [[t["id"], t["name"], t["prefix"] + "…", t["scope"], (t["last_used_at"] or "never")[:16],
              (t["expires_at"] or "never")[:10]] for t in items]))
    if args.action == "create":
        if not args.name:
            raise Fail("Give the token a --name.", 2)
        t = c.request("POST", "/tokens", {"name": args.name, "scope": "read" if args.read_only else "write",
                                          "expires_days": args.days})
        return emit(args, t, ids=[t["token"]], text=f"{t['token']}\n(shown once: keep it somewhere safe)")
    if args.id is None:
        raise Fail("Give the token's ID (from tokens list).", 2)
    r = c.request("DELETE", f"/tokens/{args.id}")
    return emit(args, r, text=r["message"])


def parse_pairs(pairs: list[str]) -> dict:
    out: dict = {}
    for p in pairs or []:
        if "=" not in p:
            raise Fail(f"Give settings as KEY=VALUE, not {p}", 2)
        k, v = p.split("=", 1)
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        elif v.lstrip("-").isdigit():
            out[k] = int(v)
        else:
            out[k] = v
    return out


def cmd_admin(args):
    c = client(args)
    what, action = args.what, args.action
    if what == "settings" and action == "set":
        if not args.name:
            raise Fail("Give the section: " + "github-app, github-sign-in, github-token, pull-requests, scanning, work-hours", 2)
        r = c.request("PATCH", f"/admin/settings/{args.name}", parse_pairs(args.pairs))
        return emit(args, r, text=r["message"])
    if what == "rules" and action == "set":
        if not args.name:
            raise Fail("Give the rule's key (from admin rules list).", 2)
        r = c.request("PATCH", f"/admin/rules/{args.name}", parse_pairs(args.pairs))
        return emit(args, r, text=r["message"])
    if action != "list":
        raise Fail(f"admin {what} {action}: use list, or for settings and rules, set (or the api command).", 2)
    path = {"settings": "/admin/settings", "rules": "/admin/rules", "channels": "/admin/channels", "people": "/admin/people",
            "users": "/admin/users", "targets": "/admin/targets", "scans": "/admin/scans", "history": "/admin/history",
            "service-tokens": "/admin/service-tokens"}[what]
    data = c.request("GET", path)
    if what == "settings":
        rows = [[k, ("(secret: set)" if v.get("set") else "(secret: not set)") if v.get("secret") else v.get("value")]
                for k, v in data["settings"].items()]
        return emit(args, data, table=(["SETTING", "VALUE"], rows), ids=list(data["settings"]))
    items = data.get("items", [])
    if not items:
        return emit(args, data, table=(["(none)"], []))
    keys = [k for k in items[0] if not isinstance(items[0][k], (dict, list))][:7]
    return emit(args, data, table=([k.upper() for k in keys], [[str(i.get(k, ""))[:60] for k in keys] for i in items]),
                ids=[i.get("id", i.get("key", i.get("login"))) for i in items])


def cmd_api(args):
    body = None
    if args.data:
        raw = sys.stdin.read() if args.data == "-" else args.data
        try:
            body = json.loads(raw)
        except ValueError:
            raise Fail("--data must be JSON.", 2) from None
    path = args.path if args.path.startswith("/") else "/" + args.path
    if path.startswith("/api/v1"):
        path = path[len("/api/v1"):]
    method = args.method.upper()
    if method not in ("GET", "POST", "PATCH", "PUT", "DELETE"):
        raise Fail("The method is GET, POST, PATCH, PUT or DELETE.", 2)
    print(json.dumps(client(args).request(method, path, body), indent=2))


# ─── The command line ───────────────────────────────────────────────────────

HELP = """\
Branchwarden from the command line: pull requests and who they're waiting on,
reviews waiting on you, branches to tidy, repositories, workflows, metrics.

First, make a token in Branchwarden (your name › API tokens), then:
  branchwarden login --url https://branchwarden.example.com

Every command takes --json (the API's JSON, stable and documented in the
server's docs/api.md and /api/v1/openapi.json) and --quiet (just ids). The
token comes from this computer's keychain, or BRANCHWARDEN_TOKEN (and the
address from BRANCHWARDEN_URL), so scripts and CI need no login.
"""

EPILOG = """\
examples:
  branchwarden me                               what's waiting on you
  branchwarden prs list --filter review --repo 'api-*'
  branchwarden prs list --reviewer sasha --json
  branchwarden prs reviewers add 412 --user sasha --dry-run
  branchwarden prs snooze 412 --days 3
  branchwarden branches list --status stale --quiet
  branchwarden workflows --problems
  branchwarden api GET /overview                any endpoint, raw JSON

exit codes: 0 done, 2 bad command line, 3 not signed in or not allowed,
4 not found, 5 refused (bad input or conflict), 6 server, GitHub or
network failure, 7 rate limited, 1 anything else.
"""


def parser() -> argparse.ArgumentParser:
    # Accepted before or after the command. SUPPRESS keeps a subcommand from resetting one given earlier.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print the API's JSON instead of a table")
    common.add_argument("--quiet", "-q", action="store_true", default=argparse.SUPPRESS,
                        help="print only ids (one per line)")
    common.add_argument("--yes", "-y", action="store_true", default=argparse.SUPPRESS,
                        help="never ask: assume yes (also when there's no terminal)")

    p = argparse.ArgumentParser(prog="branchwarden", description=HELP, epilog=EPILOG, parents=[common],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"branchwarden-cli {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, help=help_, description=help_, parents=[common], **kw)
        if fn:
            sp.set_defaults(fn=fn)
        return sp

    s = add("login", cmd_login, "sign in: check a token works and keep it in this computer's keychain")
    s.add_argument("--url", help="Branchwarden's address, e.g. https://branchwarden.example.com (or BRANCHWARDEN_URL)")
    s.add_argument("--token", help="the token; - to read it from stdin; leave out to be asked (or BRANCHWARDEN_TOKEN)")
    s.add_argument("--fingerprint", help="trust the server's own certificate with this SHA-256 fingerprint "
                                         "(from Settings › TLS), without being asked")
    s.add_argument("--no-keychain", action="store_true", help="check the token, but don't store it")
    add("logout", cmd_logout, "forget the address and remove the token from the keychain")
    add("whoami", cmd_whoami, "who the token acts as, its role and access, and the versions")
    add("me", cmd_me, "My work: reviews waiting on you, your pull requests and what each needs, your branches")

    prs = add("prs", None, "pull requests: list, show, reviewers, snooze")
    psub = prs.add_subparsers(dest="action", metavar="ACTION", required=True)
    s = psub.add_parser("list", help="open pull requests, longest waiting first", parents=[common])
    s.add_argument("--filter", choices=["all", "review", "changes", "approved", "ci", "conflicts", "draft", "big",
                                        "stalled", "noticket"], default="all", help="as the dashboard's filters (all)")
    s.add_argument("--repo", help="owner/name, or a pattern such as 'api-*'")
    s.add_argument("--author", help="GitHub login")
    s.add_argument("--reviewer", help="a login or org/team asked to review")
    s.add_argument("--search", "-s", help="search titles, repos, authors and branch names")
    s.add_argument("--limit", type=int, help="at most this many (all of them by default)")
    s.set_defaults(fn=cmd_prs_list)
    s = psub.add_parser("show", help="one pull request in full", parents=[common])
    s.add_argument("id", type=int, help="its ID (the first column of prs list)")
    s.set_defaults(fn=cmd_prs_show)
    s = psub.add_parser("reviewers", parents=[common], help="request or withdraw reviewers. This writes to GitHub: "
                                                            "it asks first (unless --yes), and --dry-run only shows what would change")
    s.add_argument("action", choices=["add", "remove"])
    s.add_argument("id", type=int, help="the PR's ID")
    s.add_argument("--user", action="append", help="a GitHub login (repeat for more)")
    s.add_argument("--team", action="append", help="an org/team slug (repeat for more)")
    s.add_argument("--notify", action="store_true", help="also tell the PR's Teams and Slack channels")
    s.add_argument("--dry-run", action="store_true", help="only say what would change")
    s.set_defaults(fn=cmd_prs_reviewers)
    s = psub.add_parser("snooze", help="put a PR's reminders off for 1, 3 or 7 days, or --off to turn them back on",
                        parents=[common])
    s.add_argument("id", type=int, help="the PR's ID")
    s.add_argument("--days", type=int, choices=[1, 3, 7], default=1)
    s.add_argument("--off", action="store_true", help="turn its reminders back on")
    s.set_defaults(fn=cmd_prs_snooze)

    br = add("branches", None, "branches: list the flagged ones, ignore one, or flag it again")
    bsub = br.add_subparsers(dest="action", metavar="ACTION", required=True)
    s = bsub.add_parser("list", help="stale branches and those left after a PR (or --status)", parents=[common])
    s.add_argument("--status", choices=["stale", "left", "active", "open_pr", "all"],
                   help="stale, left (after a PR), active, open_pr, or all (default: stale and left)")
    s.add_argument("--repo", help="owner/name, or a pattern")
    s.add_argument("--search", "-s", help="search branch names and committers")
    s.add_argument("--limit", type=int, help="at most this many")
    s.set_defaults(fn=cmd_branches_list)
    s = bsub.add_parser("ignore", help="stop flagging a branch (or --undo to flag it again)", parents=[common])
    s.add_argument("id", type=int, nargs="?", help="the branch's ID (from branches list)")
    s.add_argument("--undo", action="store_true", help="flag it again: give --repo and --branch")
    s.add_argument("--repo", help="with --undo: owner/name")
    s.add_argument("--branch", help="with --undo: the branch name")
    s.set_defaults(fn=cmd_branches_ignore)

    s = add("repos", cmd_repos, "every repository with its counts and health, or one (owner/name) in full")
    s.add_argument("repo", nargs="?", help="owner/name for one repository")
    s = add("workflows", cmd_workflows, "GitHub Actions workflows: runs, pass rate, failing, missed schedules")
    s.add_argument("--days", type=int, choices=[7, 14, 30], default=14)
    s.add_argument("--repo", help="owner/name")
    s.add_argument("--problems", action="store_true", help="only those failing, missing runs or switched off")
    s = add("metrics", cmd_metrics, "time to first review and to merge, merges, by size")
    s.add_argument("--days", type=int, choices=[30, 90, 180], default=30)
    s.add_argument("--repo", help="owner/name")
    s = add("mvp", cmd_mvp, "the week's MVPs: reviews, merges, branch prunes, PRs raised")
    s.add_argument("--prev", action="store_true", help="the 7 days before the last 7")
    add("scan", cmd_scan, "start a full scan now (members and admins)")

    s = add("tokens", cmd_tokens, "your API tokens: list, create (shown once) or revoke")
    s.add_argument("action", choices=["list", "create", "revoke"])
    s.add_argument("id", type=int, nargs="?", help="revoke: the token's ID")
    s.add_argument("--name", help="create: what it's for")
    s.add_argument("--read-only", action="store_true", help="create: a read-only token")
    s.add_argument("--days", type=int, help="create: expires after this many days (never, by default)")

    s = add("admin", cmd_admin, "settings, rules, channels, people, users, targets, scans and history (admins)")
    s.add_argument("what", choices=["settings", "rules", "channels", "people", "users", "targets", "scans", "history",
                                    "service-tokens"])
    s.add_argument("action", choices=["list", "set"], nargs="?", default="list")
    s.add_argument("name", nargs="?", help="set: the settings section, or the rule's key")
    s.add_argument("pairs", nargs="*", metavar="KEY=VALUE", help="set: what to change, e.g. STALE_DAYS=45")

    s = add("api", cmd_api, "call any API endpoint and print its JSON (see docs/api.md)")
    s.add_argument("method", help="GET, POST, PATCH, PUT or DELETE")
    s.add_argument("path", help="e.g. /overview or /pulls?filter=stalled")
    s.add_argument("--data", "-d", help="a JSON body, or - to read it from stdin")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    for flag in ("json", "quiet", "yes"):
        setattr(args, flag, getattr(args, flag, False))
    try:
        args.fn(args)
    except Fail as exc:
        print(f"branchwarden: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
