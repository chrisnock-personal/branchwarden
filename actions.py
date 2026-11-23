"""
actions.py
~~~~~~~~~~
Everything that changes something, for both the dashboard's forms and the API:
the same checks, the same history lines, the same messages. Each function
takes a database connection, its input and who is acting, and returns a
Result; the dashboard flashes its message, the API returns it as JSON.

Input comes as a Mapping with the dashboard's form field names (the API turns
its JSON into one), so a field means the same thing in both.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Mapping, Optional
from urllib.parse import urlsplit

import psycopg2
from psycopg2.extras import Json

import analysis
import data
import db
import notify
import rules
import settings
import tls
from github import Connections, GitHubError
from metrics import compact as metrics_compact


@dataclass
class Result:
    ok: bool
    message: str
    tone: str = "ok"                 # ok | info | bad: for the dashboard's flash
    status: int = 200                # for the API
    data: dict = field(default_factory=dict)


def done(message: str, tone: str = "ok", status: int = 200, **data) -> Result:
    return Result(True, message, tone, status, data)


def refused(message: str, status: int = 400, **data) -> Result:
    return Result(False, message, "bad", status, data)


def _flag(form: Mapping, key: str) -> bool:
    v = form.get(key)
    return bool(v) and str(v).lower() not in ("0", "false", "off", "no")


def _list(form: Mapping, key: str) -> list[str]:
    if hasattr(form, "getlist"):
        return form.getlist(key)
    v = form.get(key)
    return list(v) if isinstance(v, (list, tuple)) else ([] if v is None else [v])


def _box(form: Mapping, key: str, values: dict) -> str:
    """A true/false setting: from the form, where an unticked box is simply absent; or, from
    the API, only when it's given (a partial update leaves the rest alone)."""
    if hasattr(form, "getlist") or key in form:
        return "true" if _flag(form, key) else "false"
    return settings.get(values, key)


def demo_loaded(conn) -> bool:
    row = db.one(conn, "SELECT value FROM settings WHERE key = 'DEMO_MODE'")
    return bool(row) and row["value"].lower() == "true"


DEMO_REFUSAL = "Demo data is loaded. Remove it first (python demo.py --remove), then connect GitHub."
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SLACK_CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{6,}$")
SLACK_ID = re.compile(r"^[UW][A-Z0-9]{6,}$")
SNOOZE_DAYS = (1, 3, 7)


# ─── Member actions ─────────────────────────────────────────────────────────

def reviewers(conn, values: dict, pr_id: int, add: Iterable[str], remove: Iterable[str], actor: str, *,
              tell: bool = False, dry_run: bool = False, connections: Callable = Connections) -> Result:
    """Request (add) or withdraw (remove) reviewers on GitHub. Entries are
    "u:login" or "t:org/team"; already-requested ones in `add`, and ones not
    requested in `remove`, are left alone."""
    found = data.open_prs(conn, pr_id=pr_id)
    if not found:
        return refused("No open pull request with that id.", 404)
    pr = found[0]
    current = {f"u:{u}" for u in pr["requested_users"]} | {f"t:{t}" for t in pr["requested_teams"]}
    add = {x.lower() for x in add} - current
    remove = {x.lower() for x in remove} & current
    bad = [x for x in add | remove if not (x.startswith("u:") or x.startswith("t:")) or len(x) < 3]
    if bad:
        return refused(f"Reviewers are u:login or t:org/team, not {', '.join(sorted(bad))}.")
    split = lambda items, p: sorted(x[2:] for x in items if x.startswith(p))  # noqa: E731
    plan = {"add": split(add, "u:") + split(add, "t:"), "remove": split(remove, "u:") + split(remove, "t:")}
    if not add and not remove:
        return done("Nothing changed.", "info", **plan)
    if dry_run:
        return done(f"Would request {', '.join(plan['add']) or 'nobody new'}"
                    + (f" and withdraw {', '.join(plan['remove'])}" if remove else "") + " on GitHub.", "info",
                    dry_run=True, **plan)
    try:
        client = connections(values).client_for(pr["target_login"], pr["target_via"])
        if add:
            client.request_reviewers(pr["full_name"], pr["number"], split(add, "u:"), split(add, "t:"))
        if remove:
            client.remove_reviewers(pr["full_name"], pr["number"], split(remove, "u:"), split(remove, "t:"))
    except GitHubError as exc:
        return refused(f"GitHub refused the change: {exc}", 502)
    new = [x for x in pr["requested"] if f"u:{x.get('user')}" not in remove and f"t:{x.get('team')}" not in remove]
    new += [{"user": x[2:]} if x.startswith("u:") else {"team": x[2:]} for x in sorted(add)]
    db.execute(conn, "UPDATE pull_requests SET requested = %s, review_requested_at = NOW() WHERE id = %s",
               (Json(new), pr_id))
    db.request_scan(conn, pr["full_name"], "reviewers changed")
    for x in sorted(add):
        db.execute(conn, "INSERT INTO assignments (pr_id, reviewer, by_rule, by_name) VALUES (%s, %s, FALSE, %s)",
                   (pr_id, x[2:], actor))
    settings.history_add(conn, actor, f"{pr['full_name']} #{pr['number']}: requested review from "
                         f"{', '.join(plan['add']) or 'nobody new'}"
                         + (f"; removed {', '.join(plan['remove'])}" if remove else ""))
    told, notified = "", []
    if tell and add:
        pr = data.enrich_pr({**pr, "requested": new}, datetime.now(timezone.utc), data.pr_prefs(conn))
        msg = {"heading": f"Review requested by {actor}", "tone": "info", "title": pr["title"],
               "title_url": pr["url"], "facts": rules._facts(pr), "pr": pr["ref"],
               "text": f"{', '.join(f'@{{{u}}}' for u in split(add, 'u:'))}"
                       f"{', ' if split(add, 'u:') and split(add, 't:') else ''}"
                       f"{', '.join(split(add, 't:'))}: please review this.",
               "buttons": rules._buttons(pr, values)}
        chans = notify.channels_for(conn, "routed", pr["full_name"], pr["requested_teams"])
        notify.enqueue(conn, chans, "assigned", msg, pr_id)
        notified = [c["name"] for c in chans]
        told = f" A notice went to {', '.join(notified)}." if chans else \
            " No channel is routed for this repo, so no notice went out."
    what = f"Review requested on GitHub from {', '.join(plan['add'])}." if add else "Reviewers removed on GitHub."
    return done(what + told, notified=notified, **plan)


def snooze(conn, pr_id: int, days: Optional[int], actor: str) -> Result:
    """Snooze a PR's reminders for 1, 3 or 7 days; any other value (or None) turns them back on."""
    pr = db.one(conn, "SELECT p.number, r.full_name FROM pull_requests p JOIN repos r ON r.id = p.repo_id "
                      "WHERE p.id = %s AND p.state = 'open'", (pr_id,))
    if not pr:
        return refused("No open pull request with that id.", 404)
    if days in SNOOZE_DAYS:
        until = datetime.now(timezone.utc) + timedelta(days=days)
        db.execute(conn, """INSERT INTO snoozes (pr_id, until, by_name) VALUES (%s, %s, %s)
                            ON CONFLICT (pr_id) DO UPDATE SET until = EXCLUDED.until, by_name = EXCLUDED.by_name,
                                at = NOW()""", (pr_id, until, actor))
        settings.history_add(conn, actor, f"{pr['full_name']} #{pr['number']}: reminders snoozed for "
                                          f"{days} day{'s' if days != 1 else ''}")
        return done(f"No reminders about this PR until {until:%a %d %b %H:%M} UTC.", until=until.isoformat())
    db.execute(conn, "DELETE FROM snoozes WHERE pr_id = %s", (pr_id,))
    return done("Reminders about this PR are back on.", until=None)


def ignore_branch(conn, branch_id: int, actor: str) -> Result:
    b = db.one(conn, """SELECT b.repo_id, b.name, r.full_name FROM branches b JOIN repos r ON r.id = b.repo_id
                         WHERE b.id = %s""", (branch_id,))
    if not b:
        return refused("No branch with that id.", 404)
    db.execute(conn, """INSERT INTO ignored_branches (repo_id, name, ignored_by) VALUES (%s, %s, %s)
                        ON CONFLICT DO NOTHING""", (b["repo_id"], b["name"], actor))
    settings.history_add(conn, actor, f"Stopped flagging {b['full_name']}:{b['name']}")
    return done(f"{b['name']} won't be flagged again. It's listed under Ignored branches if you change your mind.",
                repo_id=b["repo_id"], name=b["name"])


def unignore_branch(conn, repo_id: int, name: str, actor: str) -> Result:
    n = db.execute(conn, "DELETE FROM ignored_branches WHERE repo_id = %s AND name = %s", (repo_id, name))
    if not n:
        return refused("That branch isn't ignored.", 404)
    settings.history_add(conn, actor, f"Flagging branch {name} again")
    return done(f"{name} is flagged again when it's stale or left behind.")


def scan(conn, actor: str) -> Result:
    db.request_scan(conn, "*", f"asked for by {actor}")
    return done("A full scan starts within a few seconds. Reload in a minute to see the results.", status=202)


def set_my_login(conn, user: dict, login: str) -> Result:
    if user.get("via_github"):
        return refused("Your GitHub login comes from signing in with GitHub.")
    login = (login or "").strip().lstrip("@")
    if login and not LOGIN.match(login):
        return refused("That isn't a GitHub login.")
    settings.set_my_login(conn, user["id"], login)
    return done(f"My work now follows {login}." if login else "GitHub login removed.", login=login.lower() or None)


# ─── Admin: GitHub ──────────────────────────────────────────────────────────

def github_app(conn, form: Mapping, actor: str) -> Result:
    if demo_loaded(conn):
        return refused(DEMO_REFUSAL, 409)
    values = settings.load(conn)
    changes = {"GITHUB_APP_ENABLED": _box(form, "GITHUB_APP_ENABLED", values),
               "GITHUB_APP_ID": str(form.get("GITHUB_APP_ID", values.get("GITHUB_APP_ID", "")) or "").strip()}
    key = str(form.get("GITHUB_APP_PRIVATE_KEY", "") or "").strip()
    if key:
        if "PRIVATE KEY-----" not in key:
            return refused("The private key should be the whole .pem file GitHub gave you, BEGIN and END lines included.")
        changes["GITHUB_APP_PRIVATE_KEY"] = key
    elif _flag(form, "remove_key"):
        changes["GITHUB_APP_PRIVATE_KEY"] = None
    secret = str(form.get("GITHUB_WEBHOOK_SECRET", "") or "").strip()
    if secret and len(secret) < 20:
        return refused("Use a webhook secret of at least 20 random characters.")
    if secret:
        changes["GITHUB_WEBHOOK_SECRET"] = secret
    if changes["GITHUB_APP_ID"] and not changes["GITHUB_APP_ID"].isdigit():
        return refused("The App ID is a number, shown on the App's settings page.")
    return saved(settings.save(conn, changes, actor))


def github_token(conn, form: Mapping, actor: str) -> Result:
    if demo_loaded(conn):
        return refused(DEMO_REFUSAL, 409)
    changes = {"GITHUB_TOKEN_ENABLED": _box(form, "GITHUB_TOKEN_ENABLED", settings.load(conn))}
    token = str(form.get("GITHUB_TOKEN", "") or "").strip()
    if token:
        changes["GITHUB_TOKEN"] = token
        changes["GITHUB_TOKEN_EXPIRES"] = None
    elif _flag(form, "remove_token"):
        changes["GITHUB_TOKEN"] = None
        changes["GITHUB_TOKEN_EXPIRES"] = None
    return saved(settings.save(conn, changes, actor))


def pr_settings(conn, form: Mapping, actor: str) -> Result:
    limits = {"BIG_PR_LINES": (50, 100000), "BIG_PR_FILES": (5, 10000), "STALLED_DAYS": (1, 365)}
    values = settings.load(conn)
    changes = {}
    for key, (lo, hi) in limits.items():
        v = str(form.get(key, settings.get(values, key)) or "").strip()
        if not v.isdigit() or not lo <= int(v) <= hi:
            return refused(f"{settings.label(key)}: a whole number from {lo} to {hi}.")
        changes[key] = v
    pattern = str(form.get("TICKET_PATTERN", settings.get(values, "TICKET_PATTERN")) or "").strip() \
        or settings.DEFAULTS["TICKET_PATTERN"][1]
    if len(pattern) > 100 or analysis.ticket_regex(pattern) is None:
        return refused("The ticket key pattern isn't a regular expression Branchwarden can use (100 characters at most).")
    url = str(form.get("TICKET_URL", settings.get(values, "TICKET_URL")) or "").strip()
    if url and (urlsplit(url).scheme != "https" or "{key}" not in url):
        return refused("The ticket link must start with https:// and contain {key}.")
    changes.update(TICKET_PATTERN=pattern, TICKET_URL=url)
    lines = settings.save(conn, changes, actor)
    return done("Saved. " + ("; ".join(lines) if lines else "Nothing changed.")
                + (" Ticket keys in descriptions are picked up as each repo is next read in full." if url else ""),
                changes=lines)


def scan_settings(conn, form: Mapping, actor: str) -> Result:
    values = settings.load(conn)
    get = lambda k: str(form.get(k, settings.get(values, k)) or "").strip()  # noqa: E731
    stale = get("STALE_DAYS")
    if not stale.isdigit() or not 1 <= int(stale) <= 3650:
        return refused("Stale after must be a number of days.")
    keep_pr, keep_log = get("RETAIN_PR_DAYS"), get("RETAIN_LOG_DAYS")
    if not (keep_pr.isdigit() and 30 <= int(keep_pr) <= 3650 and keep_log.isdigit() and 7 <= int(keep_log) <= 3650):
        return refused("Keep closed PRs for 30 to 3650 days, and messages and scans for 7 to 3650 days.")
    flag = lambda k: _box(form, k, values)  # noqa: E731
    changes = {
        "REPO_INCLUDE": settings.join_list(settings.split_list(get("REPO_INCLUDE"))) or "*",
        "REPO_EXCLUDE": settings.join_list(settings.split_list(get("REPO_EXCLUDE"))),
        "INCLUDE_ARCHIVED": flag("INCLUDE_ARCHIVED"),
        "INCLUDE_FORKS": flag("INCLUDE_FORKS"),
        "STALE_DAYS": stale, "RETAIN_PR_DAYS": keep_pr, "RETAIN_LOG_DAYS": keep_log,
        "NEVER_FLAG": settings.join_list(settings.split_list(get("NEVER_FLAG"))),
        "PUBLIC_URL": get("PUBLIC_URL").rstrip("/"),
        "GITHUB_API_URL": get("GITHUB_API_URL").rstrip("/") or "https://api.github.com",
    }
    interval = get("SCAN_INTERVAL_MINUTES")
    if interval in dict(settings.SCAN_INTERVALS):
        changes["SCAN_INTERVAL_MINUTES"] = interval
    for key in ("PUBLIC_URL", "GITHUB_API_URL"):
        if changes[key] and urlsplit(changes[key]).scheme != "https":
            return refused(f"{settings.label(key)} must start with https://")
    lines = settings.save(conn, changes, actor)
    if any(k in " ".join(lines) for k in ("Only repos", "Skip repos", "archived", "forks")):
        db.request_scan(conn, "*", "scan settings changed")
    return saved(lines)


def saved(lines: list[str]) -> Result:
    return done("Saved: " + "; ".join(lines) + "." if lines else "Nothing changed.", "ok" if lines else "info",
                changes=lines)


def target_add(conn, form: Mapping, actor: str) -> Result:
    if demo_loaded(conn):
        return refused(DEMO_REFUSAL, 409)
    login = str(form.get("login", "") or "").strip().lstrip("@").lower()
    kind = form.get("kind") or "org"
    via = form.get("via") or "auto"
    if not LOGIN.match(login) or kind not in ("org", "user") or via not in ("auto", "app", "token"):
        return refused("Enter a GitHub organisation or account name.")
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT target_add")
            cur.execute("INSERT INTO targets (login, kind, via) VALUES (%s, %s, %s) RETURNING id", (login, kind, via))
            tid = cur.fetchone()[0]
    except psycopg2.errors.UniqueViolation:
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT target_add")
        return refused(f"{login} is already on the list.", 409)
    settings.history_add(conn, actor, f"Added {kind} {login} to scan")
    db.request_scan(conn, "*", f"{login} added")
    return done(f"{login} added. Its first scan starts within a few seconds; it records what's there "
                "without sending any messages.", status=201, id=tid)


def target_update(conn, target_id: int, form: Mapping, actor: str) -> Result:
    t = db.one(conn, "SELECT * FROM targets WHERE id = %s", (target_id,))
    if not t:
        return refused("No account with that id.", 404)
    if _flag(form, "delete"):
        db.execute(conn, "DELETE FROM targets WHERE id = %s", (target_id,))
        settings.history_add(conn, actor, f"Stopped scanning {t['login']}")
        return done(f"{t['login']} removed, with its repos, PRs and branches.")
    if _flag(form, "toggle") or ("enabled" in form and _flag(form, "enabled") != t["enabled"]):
        db.execute(conn, "UPDATE targets SET enabled = NOT enabled WHERE id = %s", (target_id,))
        settings.history_add(conn, actor, f"{t['login']}: scanning {'off' if t['enabled'] else 'on'}")
        return done(f"Scanning {t['login']} switched {'off' if t['enabled'] else 'on'}.")
    if form.get("via") in ("auto", "app", "token"):
        db.execute(conn, "UPDATE targets SET via = %s WHERE id = %s", (form["via"], target_id))
        settings.history_add(conn, actor, f"{t['login']}: reached through {form['via']}")
        return done(f"{t['login']} is now reached through {form['via']}.")
    return done("Nothing changed.", "info")


# ─── Admin: channels, working hours and rules ───────────────────────────────

def channel_url_problem(kind: str, url: str) -> Optional[str]:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        return "The webhook URL must start with https://"
    host = parts.hostname or ""
    if kind == "slack" and not host.endswith("slack.com"):
        return "A Slack incoming webhook URL starts with https://hooks.slack.com/"
    if kind == "teams" and not (host.endswith(".logic.azure.com") or host.endswith(".powerplatform.com")
                                or host.endswith(".office.com") or host.endswith(".microsoft.com")):
        return ("A Teams Workflows webhook URL is on logic.azure.com or powerplatform.com: "
                "make one with the “Post to a channel when a webhook request is received” workflow")
    return None


def slack_bot_problem(token: str, channel: str) -> Optional[str]:
    if not token.startswith("xoxb-"):
        return "A Slack bot token starts with xoxb- (OAuth & Permissions › Bot User OAuth Token)"
    if not SLACK_CHANNEL_ID.match(channel):
        return "The Slack channel ID looks like C01ABCDEF23 (channel details › About, at the bottom)"
    return None


def _teams(raw) -> str:
    return settings.join_list(t.lower().lstrip("@") for t in settings.split_list(str(raw or "")))


def channel_add(conn, form: Mapping, actor: str) -> Result:
    kind = form.get("kind")
    name = str(form.get("name", "") or "").strip()
    url = str(form.get("url", "") or "").strip()
    token = str(form.get("bot_token", "") or "").strip()
    slack_channel = str(form.get("slack_channel", "") or "").strip().upper()
    if kind not in ("teams", "slack") or not name:
        return refused("Give the channel a name and a webhook URL.")
    if kind == "slack" and (token or slack_channel):
        problem = slack_bot_problem(token, slack_channel)
        url = ""
    else:
        problem = channel_url_problem(kind, url)
    if problem:
        return refused(problem)
    cid = db.one(conn, """INSERT INTO channels (kind, name, repos, teams, slack_channel) VALUES (%s, %s, %s, %s, %s)
                          RETURNING id""",
                 (kind, name, settings.join_list(settings.split_list(str(form.get("repos", "") or ""))),
                  _teams(form.get("teams")), slack_channel if token else ""))["id"]
    secret = {settings.channel_token_key(cid): token} if token else {settings.channel_url_key(cid): url}
    settings.save(conn, secret, actor, record=False)
    settings.history_add(conn, actor, f"Added {kind} channel {name}" + (" (bot token, threads)" if token else ""))
    return done(f"{name} added. Send a test message to check it.", status=201, id=cid)


def channel_update(conn, cid: int, form: Mapping, actor: str) -> Result:
    c = db.one(conn, "SELECT * FROM channels WHERE id = %s", (cid,))
    if not c:
        return refused("No channel with that id.", 404)
    if _flag(form, "delete"):
        db.execute(conn, "DELETE FROM channels WHERE id = %s", (cid,))
        settings.save(conn, {settings.channel_url_key(cid): None, settings.channel_token_key(cid): None},
                      actor, record=False)
        settings.history_add(conn, actor, f"Removed channel {c['name']}")
        return done(f"{c['name']} removed, with its queued messages.")
    if _flag(form, "toggle") or ("enabled" in form and _flag(form, "enabled") != c["enabled"]):
        db.execute(conn, "UPDATE channels SET enabled = NOT enabled WHERE id = %s", (cid,))
        settings.history_add(conn, actor, f"{c['name']}: switched {'off' if c['enabled'] else 'on'}")
        return done(f"{c['name']} switched {'off' if c['enabled'] else 'on'}.")
    url = str(form.get("url", "") or "").strip()
    token = str(form.get("bot_token", "") or "").strip()
    slack_channel = str(form.get("slack_channel", c["slack_channel"]) or "").strip().upper()
    if url:
        problem = channel_url_problem(c["kind"], url)
        if problem:
            return refused(problem)
        settings.save(conn, {settings.channel_url_key(cid): url, settings.channel_token_key(cid): None}, actor)
        db.execute(conn, "UPDATE channels SET slack_channel = '' WHERE id = %s", (cid,))
    elif c["kind"] == "slack" and (token or slack_channel != c["slack_channel"]):
        has_token = bool(token) or settings.channel_token_key(cid) in settings.secret_meta(conn)
        problem = slack_bot_problem(token or "xoxb-kept", slack_channel) if has_token else \
            "Enter the bot token too, or a webhook URL instead."
        if problem:
            return refused(problem)
        if token:
            settings.save(conn, {settings.channel_token_key(cid): token, settings.channel_url_key(cid): None}, actor)
        db.execute(conn, "UPDATE channels SET slack_channel = %s WHERE id = %s", (slack_channel, cid))
    repos = settings.join_list(settings.split_list(str(form.get("repos", c["repos"]) or "")))
    teams = _teams(form.get("teams", c["teams"]))
    name = str(form.get("name", "") or "").strip() or c["name"]
    db.execute(conn, "UPDATE channels SET name = %s, repos = %s, teams = %s WHERE id = %s", (name, repos, teams, cid))
    if (name, repos, teams) != (c["name"], c["repos"], c["teams"]):
        settings.history_add(conn, actor, f"Channel {name}: repos [{repos or 'all'}], teams [{teams or 'none'}]")
    return done(f"{name} saved.")


def channel_test(conn, cid: int, actor: str, public_url: str) -> Result:
    c = db.one(conn, "SELECT * FROM channels WHERE id = %s", (cid,))
    if not c:
        return refused("No channel with that id.", 404)
    person = db.one(conn, "SELECT login FROM people WHERE teams_id <> '' OR slack_id <> '' LIMIT 1")
    login = person["login"] if person else None
    msg = {"heading": "Test from Branchwarden", "tone": "info",
           "text": f"Sent by {actor} from Settings › Channels and reminders, to check this channel works."
                   + (f" Mention check: @{{{login}}}" if login else ""),
           "facts": [["Channel", c["name"]], ["Kind", "Teams Adaptive Card" if c["kind"] == "teams" else "Slack Block Kit"]],
           "buttons": [["Open Branchwarden", public_url]]}
    started = time.monotonic()
    try:
        notify.send(conn, c, msg)
    except (notify.PermanentFailure, notify.RetryLater) as exc:
        return refused(f"The test failed: {exc}.", 502)
    return done(f"Test sent and accepted ({(time.monotonic() - started) * 1000:.0f} ms).")


def work_hours(conn, form: Mapping, actor: str) -> Result:
    days = form.get("days")
    if isinstance(days, (list, tuple)):
        days = [d for d in analysis.DAYS if d in days]
    else:
        days = [d for d in analysis.DAYS if _flag(form, f"day_{d}")]
    start, end = str(form.get("WORK_START", "") or ""), str(form.get("WORK_END", "") or "")
    tz = str(form.get("WORK_TZ", "") or "").strip()
    if not days or not HHMM.match(start) or not HHMM.match(end) or start >= end:
        return refused("Pick at least one day, and a start time before the end time (HH:MM).")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:
        return refused(f"{tz} isn't a time zone name like Europe/London.")
    return saved(settings.save(conn, {"WORK_DAYS": ",".join(days), "WORK_START": start, "WORK_END": end,
                                      "WORK_TZ": tz}, actor))


def _num(raw, lo: float, hi: float) -> Optional[float]:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def rule_update(conn, key: str, form: Mapping, actor: str) -> Result:
    rule = rules.RULE_BY_KEY.get(key)
    if not rule:
        return refused("No rule with that key.", 404)
    current = rules.load(conn)[key]
    if _flag(form, "toggle") or ("enabled" in form and _flag(form, "enabled") != current["enabled"]):
        db.execute(conn, "UPDATE rules SET enabled = NOT enabled, updated_at = NOW() WHERE key = %s", (key,))
        settings.history_add(conn, actor, f"Rule {rule.name}: switched {'off' if current['enabled'] else 'on'}")
        if "enabled" not in form or len(form) == 1:
            return done(f"{rule.name} switched {'off' if current['enabled'] else 'on'}.")
    if _flag(form, "send_now") and key in ("mvp", "digest"):
        values = settings.load(conn)
        if key == "mvp":
            n = rules.run_mvp_post(conn, values, force=True)
            return done(f"MVP post queued for {n} channel{'s' if n != 1 else ''}." if n
                        else "Nothing to post: no reviews, merges, prunes or new PRs in the last 7 days, or no channels.",
                        "ok" if n else "info", queued=n)
        n = rules.run_digest(conn, values, force=True)
        return done(f"Digest queued for {n} channel{'s' if n != 1 else ''}." if n
                    else "Nothing to put in a digest: no stale or left-behind branches in any channel's repos.",
                    "ok" if n else "info", queued=n)
    p = dict(current["params"])
    target = str(form.get("target", p.get("target", "routed")))
    if target in ("routed", "all") or (target.isdigit() and db.one(conn, "SELECT 1 AS x FROM channels WHERE id = %s",
                                                                    (int(target),))):
        p["target"] = target
    # A dashboard form leaves an unticked box out altogether; the API leaves out what it isn't changing.
    if hasattr(form, "getlist") or "mention" in form:
        p["mention"] = _flag(form, "mention")
    get = lambda k: form.get(k, p.get(k))  # noqa: E731
    box = lambda k: _flag(form, k) if (hasattr(form, "getlist") or k in form) else bool(p.get(k))  # noqa: E731
    if rule.kind == "reminder":
        after, repeat, mx = _num(get("after"), 0, 10000), _num(get("repeat"), 0.1, 10000), _num(get("max"), 1, 50)
        if after is None or repeat is None or mx is None or get("unit") not in dict(rules.UNITS):
            return refused("Use numbers for the wait, the repeat and the maximum.")
        p.update(after=metrics_compact(after), repeat=metrics_compact(repeat), max=int(mx), unit=get("unit"),
                 skip_label=str(get("skip_label") or "").strip(), group=box("group"))
    elif rule.kind == "assign":
        after, count = _num(get("after"), 0, 10000), _num(get("count"), 1, 5)
        if after is None or count is None or get("unit") not in dict(rules.UNITS):
            return refused("Use a number for the wait, and 1 to 5 reviewers.")
        p.update(after=metrics_compact(after), unit=get("unit"), count=int(count), codeowners=box("codeowners"),
                 pool=settings.join_list(e.lower().lstrip("@") for e in settings.split_list(str(get("pool") or ""))),
                 repos=settings.join_list(settings.split_list(str(get("repos") or ""))),
                 skip_label=str(get("skip_label") or "").strip())
    elif key == "wf_failed":
        if get("scope") in dict(rules.WF_SCOPES):
            p["scope"] = get("scope")
        p["fixed"] = box("fixed")
    elif rule.kind == "dm":
        if not HHMM.match(str(get("time") or "")):
            return refused("Give the time as HH:MM, in the working-hours time zone.")
        p["time"] = get("time")
        token = str(form.get("slack_token", "") or "").strip()
        if token:
            if not token.startswith("xoxb-"):
                return refused("A Slack bot token starts with xoxb- (OAuth & Permissions › Bot User OAuth Token).")
            settings.save(conn, {"SLACK_DM_TOKEN": token}, actor)
        elif _flag(form, "remove_token"):
            settings.save(conn, {"SLACK_DM_TOKEN": None}, actor)
    elif rule.kind in ("digest", "post"):
        if get("day") not in analysis.DAYS or not HHMM.match(str(get("time") or "")):
            return refused("Pick a day and a time (HH:MM).")
        p.update(day=get("day"), time=get("time"))
    db.execute(conn, "UPDATE rules SET params = %s, updated_at = NOW() WHERE key = %s", (Json(p), key))
    settings.history_add(conn, actor, f"Rule {rule.name}: settings changed")
    return done(f"{rule.name} saved.", params=p)


# ─── Admin: people, TLS, sign-in, users ─────────────────────────────────────

def person_save(conn, login: str, name: str, teams_id: str, slack_id: str) -> Result:
    """One person's mapping: returns ok with changed=True/False, or refused for a bad Slack ID."""
    login = (login or "").strip().lower()
    if not LOGIN.match(login.replace("[bot]", "")):
        return refused("That isn't a GitHub login.")
    name, teams_id, slack_id = (name or "").strip(), (teams_id or "").strip(), (slack_id or "").strip().upper()
    if slack_id and not SLACK_ID.match(slack_id):
        return refused("Slack member IDs look like U01ABCDEF2 (Profile › ⋮ › Copy member ID).")
    old = db.one(conn, "SELECT * FROM people WHERE login = %s", (login,))
    if old and (old["name"], old["teams_id"], old["slack_id"]) == (name, teams_id, slack_id):
        return done("Nothing changed.", "info", changed=False)
    if not old and not (name or teams_id or slack_id):
        return done("Nothing changed.", "info", changed=False)
    db.execute(conn, """INSERT INTO people (login, name, teams_id, slack_id, updated_at) VALUES (%s, %s, %s, %s, NOW())
                        ON CONFLICT (login) DO UPDATE SET name = EXCLUDED.name, teams_id = EXCLUDED.teams_id,
                            slack_id = EXCLUDED.slack_id, updated_at = NOW()""", (login, name, teams_id, slack_id))
    return done(f"{login} saved.", changed=True)


def people_save(conn, form: Mapping, actor: str) -> Result:
    logins = _list(form, "login")
    names, teams_ids, slack_ids = _list(form, "name"), _list(form, "teams_id"), _list(form, "slack_id")
    changed, bad = 0, []
    for i, login in enumerate(logins):
        r = person_save(conn, login, names[i] if i < len(names) else "", teams_ids[i] if i < len(teams_ids) else "",
                        slack_ids[i] if i < len(slack_ids) else "")
        if not r.ok and "Slack" in r.message:
            bad.append(login.strip().lower())
            r = person_save(conn, login, names[i], teams_ids[i], "")
        changed += 1 if r.data.get("changed") else 0
    if changed:
        settings.history_add(conn, actor, f"People: updated {changed}")
    msg = f"Saved {changed} {'person' if changed == 1 else 'people'}." if changed else "Nothing changed."
    if bad:
        msg += (f" Slack member IDs look like U01ABCDEF2 (Profile › ⋮ › Copy member ID); left blank for "
                f"{', '.join(bad)}.")
    return done(msg, "ok" if changed and not bad else ("bad" if bad else "info"), changed=changed, bad=bad)


def tls_install(conn, cert: bytes, key: bytes, chain: bytes, actor: str, *, self_signed: bool = False,
                restore: bool = False) -> Result:
    if not tls.enabled():
        return refused("TLS is switched off (BRANCHWARDEN_TLS=off).")
    if restore:
        ok = tls.restore_previous()
        if ok:
            settings.history_add(conn, actor, "TLS: restored the previous certificate")
        return done("The previous certificate is back. The dashboard reloads it in a few seconds."
                    if ok else "There's no previous certificate.", "ok" if ok else "info")
    if self_signed:
        cert, key = tls.make_self_signed()
    else:
        if chain:
            cert = cert.rstrip() + b"\n" + chain.strip() + b"\n"
        problem = tls.check(cert, key)
        if problem:
            return refused(problem + " Nothing was changed.")
    tls.install(cert, key)
    info = tls.describe(cert)
    settings.history_add(conn, actor, f"TLS: installed {'a new self-signed' if self_signed else 'an uploaded'} "
                                      f"certificate ({info['fingerprint'][:23]}…)")
    return done("Certificate installed. The dashboard reloads it in a few seconds: reload this page then. "
                "If it doesn't come back, restore the previous one from the log's instructions or the certs volume.",
                fingerprint=info["fingerprint"])


def github_login_settings(conn, form: Mapping, actor: str) -> Result:
    values = settings.load(conn)
    changes = {"GITHUB_LOGIN_ENABLED": _box(form, "GITHUB_LOGIN_ENABLED", values),
               "GITHUB_OAUTH_CLIENT_ID": str(form.get("GITHUB_OAUTH_CLIENT_ID",
                                                      settings.get(values, "GITHUB_OAUTH_CLIENT_ID")) or "").strip()}
    if str(form.get("GITHUB_OAUTH_CLIENT_SECRET", "") or "").strip():
        changes["GITHUB_OAUTH_CLIENT_SECRET"] = str(form["GITHUB_OAUTH_CLIENT_SECRET"]).strip()
    for role in settings.ROLES:
        k = f"GITHUB_ROLE_{role.upper()}"
        raw = form.get(k, settings.get(values, k))
        raw = ",".join(raw) if isinstance(raw, (list, tuple)) else str(raw or "")
        changes[k] = settings.join_list(e.lower().lstrip("@") for e in settings.split_list(raw))
    if changes["GITHUB_LOGIN_ENABLED"] == "true" and not any(changes[f"GITHUB_ROLE_{r.upper()}"] for r in settings.ROLES):
        return refused("Name at least one team, organisation or login in the role lists, or nobody could sign in with GitHub.")
    return saved(settings.save(conn, changes, actor))


def user_add(conn, name: str, email: str, role: str, actor: str) -> Result:
    name, email = (name or "").strip(), (email or "").strip()
    if not name or "@" not in email or role not in settings.ROLES:
        return refused("Enter a name, an email and a role.")
    try:
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT user_add")
        new = settings.create_user(conn, email, name, role, actor)
    except psycopg2.errors.UniqueViolation:
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT user_add")
        return refused(f"There's already an account for {email}.", 409)
    return done(f"{name} added.", status=201, id=new.user_id, token=new.token)


def user_role(conn, me: int, user_id: int, role: str, actor: str) -> Result:
    if user_id == me:
        return refused("You can't change your own role. Ask another admin.", 409)
    target = settings.user(conn, user_id)
    if not target:
        return refused("No account with that id.", 404)
    if target["via_github"]:
        return refused(f"{target['name']}'s role comes from their GitHub teams: change the role lists instead.", 409)
    problem = settings.set_role(conn, user_id, role or "", actor)
    return refused(problem) if problem else done("Role changed.")


def user_link(conn, user_id: int, actor: str) -> Result:
    person = settings.user(conn, user_id)
    if not person:
        return refused("No account with that id.", 404)
    if person["via_github"]:
        return refused(f"{person['name']} signs in with GitHub, so has no password to set.", 409)
    new = settings.new_setup_link(conn, user_id, actor)
    return done("New setup link made.", token=new.token, name=person["name"], role=person["role"])


def user_delete(conn, me: int, user_id: int, actor: str) -> Result:
    if user_id == me:
        return refused("You can't remove your own account.", 409)
    if not settings.user(conn, user_id):
        return refused("No account with that id.", 404)
    problem = settings.delete_user(conn, user_id, actor)
    return refused(problem, 409) if problem else done("Account removed. They're signed out on their next click.")


# ─── The morning message: opting in ─────────────────────────────────────────

def morning_status(conn, login: str) -> dict:
    login = (login or "").lower()
    p = db.one(conn, "SELECT slack_id, morning, morning_to, morning_last, morning_error FROM people WHERE login = %s",
               (login,))
    rule = rules.load(conn)["morning"]
    meta = settings.secret_meta(conn)
    return {"enabled": bool(p and p["morning"]), "to": p["morning_to"] if p else "slack",
            "slack_id_set": bool(p and p["slack_id"]), "token_set": "SLACK_DM_TOKEN" in meta,
            "teams_set": settings.morning_teams_key(login) in meta,
            "last_sent": p["morning_last"] if p else None, "error": p["morning_error"] if p else None,
            "rule_on": rule["enabled"], "time": rule["params"].get("time")}


def morning_teams_problem(url: str) -> Optional[str]:
    if channel_url_problem("teams", url):
        return ("A Teams Workflows webhook URL starts with https:// and is on logic.azure.com or "
                "powerplatform.com: make one with the “Post to a chat when a webhook request is received” workflow")
    return None


def set_morning(conn, login: str, enabled: Optional[bool], actor: str, to: Optional[str] = None,
                teams_url: Optional[str] = None, remove_teams: bool = False) -> Result:
    """Turn a person's morning message on or off, and say where it goes: Slack (a DM from the bot, which
    needs their Slack member ID) or Teams (their own Workflows webhook, given here and stored encrypted)."""
    login = (login or "").lower()
    if not login:
        return refused("Give your GitHub login on My work first.", 409)
    if to is not None and to not in ("slack", "teams"):
        return refused("Send it to slack or teams.")
    teams_url = (teams_url or "").strip()
    if teams_url and morning_teams_problem(teams_url):
        return refused(morning_teams_problem(teams_url))
    p = db.one(conn, "SELECT slack_id, morning, morning_to FROM people WHERE login = %s", (login,))
    to = to or (p["morning_to"] if p else ("teams" if teams_url else "slack"))
    enabled = bool(p and p["morning"]) if enabled is None else enabled
    key = settings.morning_teams_key(login)
    has_teams = bool(teams_url) or (key in settings.secret_meta(conn) and not remove_teams)
    if enabled and to == "slack" and not (p and p["slack_id"]):
        return refused("Your Slack member ID isn't known yet: ask an admin to add it under Settings › People, "
                       "or send it to Teams instead.", 409)
    if enabled and to == "teams" and not has_teams:
        return refused("Paste your Teams Workflows webhook URL first.", 409)
    if teams_url:
        settings.save(conn, {key: teams_url}, actor)
    elif remove_teams:
        settings.save(conn, {key: None}, actor)
    db.execute(conn, """INSERT INTO people (login, morning, morning_to) VALUES (%s, %s, %s)
                        ON CONFLICT (login) DO UPDATE SET morning = EXCLUDED.morning, morning_to = EXCLUDED.morning_to,
                                                          morning_error = NULL""", (login, enabled, to))
    settings.history_add(conn, actor, f"{login}: morning message {'on' if enabled else 'off'}, to {to.capitalize()}")
    where = "in Teams, in your chat with Workflows" if to == "teams" else "in Slack"
    return done(f"You'll get a morning message {where} on working days, when something's waiting on you."
                if enabled else "No more morning messages.", enabled=enabled, to=to)


def morning_now(conn, values: dict, login: str) -> Result:
    p = db.one(conn, "SELECT * FROM people WHERE login = %s", ((login or "").lower(),))
    if not p:
        return refused("Choose where it goes first: your Slack member ID (ask an admin, under Settings › People) "
                       "or your Teams webhook.", 409)
    if settings.flag(values, "DEMO_MODE"):
        return refused("Demo data is loaded, so nothing is sent (the worker sends no morning messages either).", 409)
    r = rules.send_morning(conn, values, p, datetime.now(timezone.utc))
    if r == "sent":
        return done("Sent: look in Teams, in your chat with Workflows." if p["morning_to"] == "teams"
                    else "Sent: look in Slack, in your messages from the app.")
    if r == "nothing":
        return done("Nothing's waiting on you right now, so there's nothing to send.", "info")
    return refused(r.split(": ", 1)[1], 502 if r.startswith("retry") else 409)
