#!/usr/bin/env python3
"""
dashboard.py
~~~~~~~~~~~~
Branchwarden's web dashboard: the overview, pull requests (with reviewer
assignment), branches and deliveries for everyone who signs in; the settings
pages (admin.py) for admins; and the endpoint GitHub's webhooks post to.

Roles: viewers read; members can also request reviewers, ignore branches and
ask for a rescan; admins change settings and manage people.

Usage:
    python dashboard.py     # serves on 0.0.0.0:$DASHBOARD_PORT (default 8080)
"""

from __future__ import annotations

import json
import logging
import re
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from dotenv import load_dotenv
from flask import Flask, abort, flash, g, redirect, render_template, request, url_for
from psycopg2.extras import Json
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

import analysis  # noqa: E402
import data  # noqa: E402
import db  # noqa: E402
import metrics  # noqa: E402
import mvp  # noqa: E402
import notify  # noqa: E402
import rules  # noqa: E402
from version import VERSION  # noqa: E402
import settings  # noqa: E402
from admin import bp as admin_bp  # noqa: E402
from github import Connections, GitHubError, verify_signature  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("branchwarden.dashboard")

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Secure cookies always, unless someone explicitly runs plain HTTP for a test.
    SESSION_COOKIE_SECURE=os.getenv("BRANCHWARDEN_INSECURE_HTTP", "").lower() not in ("1", "true", "yes"),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=25 * 1024 * 1024,     # GitHub webhook payloads are at most 25 MB
)
if os.getenv("BRANCHWARDEN_BEHIND_PROXY", "").lower() in ("1", "true", "yes"):
    # Only behind a proxy: take the client address, scheme and host from its
    # X-Forwarded-* headers. Without one, those headers would come from anyone.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' "
       "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


app.register_blueprint(admin_bp)

ORG_CACHE_SECONDS = 3600
WEBHOOK_EVENTS = {"pull_request", "pull_request_review", "pull_request_review_comment", "check_suite",
                  "check_run", "status", "push", "create", "delete", "repository"}


def can_act() -> bool:
    return bool(g.get("user")) and g.user["role"] in ("admin", "member")


@app.context_processor
def _globals():
    return {"can_act": can_act(), "now": datetime.now(timezone.utc), "version": VERSION}


@app.template_filter("ago")
def _ago(when):
    return analysis.ago(when)


@app.template_filter("when")
def _when(value):
    return value.strftime("%d %b %H:%M") if value else "—"


def _link(**changes) -> str:
    args = {k: v for k, v in request.args.items()}
    for k, v in changes.items():
        if v in (None, ""):
            args.pop(k, None)
        else:
            args[k] = v
    return request.path + ("?" + urlencode(args) if args else "")


app.jinja_env.globals["link"] = _link


ERRORS = {400: ("Something was wrong with that request", None), 403: ("Not allowed",
          "Your role can't do that. Ask an admin."), 404: ("Not found", "That page or item doesn't exist (any more)."),
          405: ("Not allowed", "That page can't be used like that.")}


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(405)
def _error(exc):
    title, message = ERRORS.get(exc.code, ("Something went wrong", None))
    if request.path.startswith("/github/"):
        return {"error": title}, exc.code
    return render_template("error.html", code=exc.code, title=title,
                           message=message or getattr(exc, "description", "")), exc.code


# ─── Overview ───────────────────────────────────────────────────────────────

def _watching(conn) -> dict:
    targets = db.rows(conn, "SELECT login, kind, enabled, repo_count FROM targets ORDER BY login")
    last = db.one(conn, """SELECT * FROM scan_log WHERE finished_at IS NOT NULL
                            ORDER BY id DESC LIMIT 1""")
    values = settings.load(conn)
    return {"targets": [t for t in targets if t["enabled"]], "last": last,
            "repo_count": db.one(conn, "SELECT COUNT(*) AS n FROM repos WHERE included")["n"],
            "interval": settings.number(values, "SCAN_INTERVAL_MINUTES", 10)}


@app.get("/")
def index():
    now = datetime.now(timezone.utc)
    with db.get_conn() as conn:
        values = settings.load(conn)
        prs = data.open_prs(conn, now=now)
        branches = data.classified_branches(conn, values, now=now)
        watching = _watching(conn)
        channels = notify.channel_health(conn)
        bars = db.rows(conn, """SELECT * FROM (SELECT * FROM scan_log WHERE finished_at IS NOT NULL
                                                ORDER BY id DESC LIMIT 24) x ORDER BY id""")
        hooks_today = db.one(conn, """SELECT COUNT(*) AS n FROM webhook_log
                                       WHERE received_at > NOW() - INTERVAL '24 hours'""")["n"]
        conns_label = []
        if values.get("GITHUB_APP_ID") and values.get("GITHUB_APP_PRIVATE_KEY") \
                and settings.flag(values, "GITHUB_APP_ENABLED"):
            conns_label.append("GitHub App")
        if values.get("GITHUB_TOKEN") and settings.flag(values, "GITHUB_TOKEN_ENABLED"):
            conns_label.append("token")
        repos = db.rows(conn, "SELECT id, name, full_name FROM repos WHERE included ORDER BY name")

    day = timedelta(hours=24)
    awaiting = [p for p in prs if "review" in p["cats"] and not p["draft"]]
    changes = [p for p in prs if p["state_key"] == "changes"]
    approved = [p for p in prs if p["state_key"] == "approved"]
    stale = [b for b in branches if b["status"] == "stale"]
    left = [b for b in branches if b["status"] in ("left_merged", "left_closed", "pushed_after_merge")]
    kpis = [
        {"label": "Open pull requests", "value": len(prs), "tone": "",
         "note": f"in {len({p['repo_id'] for p in prs})} repos · {sum(1 for p in prs if p['draft'])} drafts"},
        {"label": "Needs review", "value": len(awaiting), "tone": "tone-warn" if awaiting else "",
         "note": f"{sum(1 for p in awaiting if p['waiting_seconds'] > day.total_seconds())} waiting over 24 h"},
        {"label": "Changes requested", "value": len(changes), "tone": "",
         "note": f"oldest {max(changes, key=lambda p: p['waiting_seconds'])['waiting']}" if changes else "none"},
        {"label": "Approved, not merged", "value": len(approved), "tone": "tone-warn" if approved else "",
         "note": f"{sum(1 for p in approved if p['waiting_seconds'] > 2 * day.total_seconds())} over 2 days"},
        {"label": "Stale branches, no PR", "value": len(stale), "tone": "tone-warn" if stale else "",
         "note": f"idle over {settings.number(values, 'STALE_DAYS', 30)} days"},
        {"label": "Left after a PR", "value": len(left), "tone": "", "note": "PR merged or closed"},
    ]

    cards = []
    for r in repos:
        mine = [p for p in prs if p["repo_id"] == r["id"]]
        rs = [b for b in stale if b["repo_id"] == r["id"]]
        rl = [b for b in left if b["repo_id"] == r["id"]]
        n_await = sum(1 for p in mine if "review" in p["cats"] and not p["draft"])
        n_changes = sum(1 for p in mine if p["state_key"] == "changes")
        n_ci = sum(1 for p in mine if "ci" in p["cats"])
        n_appr = sum(1 for p in mine if p["state_key"] == "approved")
        if not mine:
            status, tone = "No open PRs", "tone-muted"
        elif n_ci:
            status, tone = f"{len(mine)} open · {n_ci} CI failing", "tone-bad"
        elif n_await:
            status, tone = f"{len(mine)} open · {n_await} need{'s' if n_await == 1 else ''} review", "tone-warn"
        elif n_changes:
            status, tone = f"{len(mine)} open · {n_changes} changes requested", "tone-warn"
        elif n_appr:
            status, tone = f"{len(mine)} open · {n_appr} approved, unmerged", "tone-info"
        else:
            status, tone = f"{len(mine)} open · all in hand", "tone-good"
        cards.append({"id": r["id"], "name": r["name"], "full_name": r["full_name"], "status": status,
                      "tone": tone, "open": len(mine),
                      "meta": f"{len(rs)} stale branch{'es' if len(rs) != 1 else ''} · {len(rl)} left after a PR"})
    cards.sort(key=lambda c: (-c["open"], c["name"]))

    attention = sorted([p for p in prs if not p["draft"]], key=lambda p: -p["waiting_seconds"])[:6]
    for p in attention:
        p["marks"] = data.reviewer_marks(p)
    peak = max([b["prs"] for b in bars] + [1])
    for b in bars:
        b["height"] = max(4, int(100 * b["prs"] / peak))
    return render_template("index.html", kpis=kpis, cards=cards[:8], repo_total=len(cards), attention=attention,
                           pr_total=len(prs), channels=channels, bars=bars, watching=watching,
                           hooks_today=hooks_today, conns_label=" + ".join(conns_label) or "none set up")


# ─── Pull requests ──────────────────────────────────────────────────────────

SORTS = (("waiting", "Longest waiting first"), ("newest", "Newest first"), ("updated", "Recently updated"))


def _org_cache(conn, login: str, kind: str, values: dict, refresh: bool = False) -> dict:
    """Members and teams of an organisation, cached for an hour."""
    row = db.one(conn, "SELECT * FROM org_cache WHERE login = %s", (login,))
    if row and not refresh and (datetime.now(timezone.utc) - row["fetched_at"]).total_seconds() < ORG_CACHE_SECONDS:
        return row
    client = Connections(values).client_for(login)
    members, teams = [], []
    if kind == "org":
        members = client.org_members(login)
        try:
            teams = client.org_teams(login)
        except GitHubError as exc:
            log.info("Can't list teams of %s: %s", login, exc)
    db.execute(conn, """INSERT INTO org_cache (login, members, teams, fetched_at) VALUES (%s, %s, %s, NOW())
                        ON CONFLICT (login) DO UPDATE SET members = EXCLUDED.members, teams = EXCLUDED.teams,
                                                          fetched_at = NOW()""",
               (login, Json(members), Json(teams)))
    return {"members": members, "teams": teams}


def candidates(conn, pr: dict, values: dict) -> dict:
    """Who could review: suggestions (code owners, recent reviewers here) and everyone else."""
    author = (pr.get("author") or "").lower()
    current_users = set(pr["requested_users"])
    current_teams = set(pr["requested_teams"])
    load = {}
    for row in db.rows(conn, """SELECT x->>'user' AS login, COUNT(*) AS n
                                  FROM pull_requests p, jsonb_array_elements(p.requested) x
                                 WHERE p.state = 'open' AND x ? 'user' GROUP BY 1"""):
        load[row["login"]] = row["n"]
    recent = db.rows(conn, """SELECT r->>'login' AS login, COUNT(*) AS n
                                FROM pull_requests p, jsonb_array_elements(p.reviews) r
                               WHERE p.repo_id = %s AND p.created_at > NOW() - INTERVAL '120 days'
                               GROUP BY 1 ORDER BY 2 DESC LIMIT 5""", (pr["repo_id"],))
    people = {p["login"]: p for p in db.rows(conn, "SELECT * FROM people")}

    error = None
    members: list[str] = []
    teams: list[dict] = []
    try:
        if pr["target_kind"] == "org":
            cached = _org_cache(conn, pr["target_login"], "org", values)
            members, teams = cached["members"], cached["teams"]
        else:
            members = Connections(values).client_for(pr["target_login"], pr["target_via"]).collaborators(
                pr["full_name"])
    except GitHubError as exc:
        error = f"Couldn't list everyone who could review ({exc}). Suggestions come from this repo's history."

    def person(login: str, why: str) -> dict:
        p = people.get(login) or {}
        n = load.get(login, 0)
        return {"value": f"u:{login}", "login": login, "name": p.get("name") or login, "why": why,
                "team": False, "checked": login in current_users,
                "load": f"{n} open review{'s' if n != 1 else ''}"}

    def team(slug: str, name: str, why: str) -> dict:
        return {"value": f"t:{slug}", "login": slug, "name": f"@{slug}", "why": why or name, "team": True,
                "checked": slug in current_teams, "load": "team"}

    team_names = {t["slug"]: t["name"] for t in teams}
    suggested: list[dict] = []
    seen: set = set()
    owners = analysis.owners_for(pr.get("files") or [], pr.get("codeowners") or "")
    for o in owners:
        if o == author or o in seen:
            continue
        seen.add(o)
        suggested.append(team(o, team_names.get(o, o), "Code owner of files in this PR") if "/" in o
                         else person(o, "Code owner of files in this PR"))
    for r in recent:
        if r["login"] in seen or r["login"] == author:
            continue
        seen.add(r["login"])
        suggested.append(person(r["login"], f"Reviewed {r['n']} PR{'s' if r['n'] != 1 else ''} here recently"))
    for login in sorted(current_users | current_teams):
        if login not in seen:
            seen.add(login)
            suggested.append(team(login, team_names.get(login, login), "Requested now") if "/" in login
                             else person(login, "Requested now"))
    others = [team(t["slug"], t["name"], f"Team · {t['name']}") for t in teams if t["slug"] not in seen]
    others += [person(m, "Member") for m in members if m not in seen and m != author]
    return {"suggested": suggested[:8], "others": others, "error": error}


@app.get("/pulls")
def pulls():
    now = datetime.now(timezone.utc)
    f = request.args.get("state", "all")
    repo = request.args.get("repo", "")
    sort = request.args.get("sort", "waiting")
    q = request.args.get("q", "").strip().lower()
    assign = request.args.get("assign", type=int)
    with db.get_conn() as conn:
        values = settings.load(conn)
        prs = data.open_prs(conn, now=now)
        repos = sorted({(p["full_name"], p["repo_name"]) for p in prs}, key=lambda r: r[1])
        last_hook = db.one(conn, "SELECT MAX(received_at) AS at FROM webhook_log WHERE status = 202")["at"]
        last_scan = db.one(conn, "SELECT MAX(finished_at) AS at FROM scan_log")["at"]
        snoozed = {r["pr_id"]: r for r in db.rows(conn, "SELECT * FROM snoozes WHERE until > NOW()")}
        assign_rule = rules.load(conn)["assign"]
        sel = None
        picker = None
        if assign:
            found = [p for p in prs if p["id"] == assign]
            if found:
                sel = found[0]
                if can_act():
                    picker = candidates(conn, sel, values)
    shown = prs
    if repo:
        shown = [p for p in shown if p["full_name"] == repo]
    if q:
        shown = [p for p in shown if q in p["title"].lower() or q in p["full_name"] or q in (p["author"] or "")
                 or q in (p["head_ref"] or "").lower() or str(p["number"]) == q.lstrip("#")]
    chips = [{"key": k, "label": label,
              "count": len(shown) if k == "all" else sum(1 for p in shown if k in p["cats"])} for k, label in analysis.FILTERS]
    if f != "all":
        shown = [p for p in shown if f in p["cats"]]
    key = {"newest": lambda p: p["created_at"], "updated": lambda p: p["updated_at"] or p["created_at"]}.get(sort)
    shown = sorted(shown, key=key, reverse=True) if key else sorted(shown, key=lambda p: -p["waiting_seconds"])
    for p in shown:
        p["marks"] = data.reviewer_marks(p)
        p["snooze"] = snoozed.get(p["id"])
    if sel:
        sel["marks"] = data.reviewer_marks(sel)
        sel["snooze"] = snoozed.get(sel["id"])
    return render_template("pulls.html", prs=shown, total=len(prs), chips=chips, state=f, repo=repo,
                           repos=repos, sort=sort, sorts=SORTS, q=q, sel=sel, picker=picker,
                           repo_count=len({p["repo_id"] for p in prs}), last_hook=last_hook, last_scan=last_scan,
                           assign_rule=assign_rule)


@app.post("/pulls/<int:pr_id>/reviewers")
def assign_reviewers(pr_id: int):
    if not can_act():
        abort(403)
    chosen = set(request.form.getlist("who"))
    shown = set(request.form.getlist("shown"))
    tell = bool(request.form.get("tell"))
    with db.get_conn() as conn:
        values = settings.load(conn)
        found = data.open_prs(conn, pr_id=pr_id)
        if not found:
            abort(404)
        pr = found[0]
        current = {f"u:{u}" for u in pr["requested_users"]} | {f"t:{t}" for t in pr["requested_teams"]}
        add = chosen - current
        remove = (current & shown) - chosen
        split = lambda items, p: sorted(x[2:] for x in items if x.startswith(p))  # noqa: E731
        try:
            client = Connections(values).client_for(pr["target_login"], pr["target_via"])
            if add:
                client.request_reviewers(pr["full_name"], pr["number"], split(add, "u:"), split(add, "t:"))
            if remove:
                client.remove_reviewers(pr["full_name"], pr["number"], split(remove, "u:"), split(remove, "t:"))
        except GitHubError as exc:
            flash(f"GitHub refused the change: {exc}", "bad")
            return redirect(url_for("pulls", assign=pr_id))
        if not add and not remove:
            flash("Nothing changed.", "info")
            return redirect(url_for("pulls", assign=pr_id))
        new = [x for x in pr["requested"] if f"u:{x.get('user')}" not in remove and f"t:{x.get('team')}" not in remove]
        new += [{"user": x[2:]} if x.startswith("u:") else {"team": x[2:]} for x in sorted(add)]
        db.execute(conn, "UPDATE pull_requests SET requested = %s, review_requested_at = NOW() WHERE id = %s",
                   (Json(new), pr_id))
        db.request_scan(conn, pr["full_name"], "reviewers changed")
        actor = g.user["name"]
        for x in sorted(add):
            db.execute(conn, "INSERT INTO assignments (pr_id, reviewer, by_rule, by_name) VALUES (%s, %s, FALSE, %s)",
                       (pr_id, x[2:], actor))
        settings.history_add(conn, actor, f"{pr['full_name']} #{pr['number']}: requested review from "
                             f"{', '.join(split(add, 'u:') + split(add, 't:')) or 'nobody new'}"
                             + (f"; removed {', '.join(split(remove, 'u:') + split(remove, 't:'))}" if remove else ""))
        names = split(add, "u:") + split(add, "t:")
        if tell and add:
            pr = data.enrich_pr({**pr, "requested": new}, datetime.now(timezone.utc))
            msg = {"heading": f"Review requested by {actor}", "tone": "info", "title": pr["title"],
                   "title_url": pr["url"], "facts": rules._facts(pr), "pr": pr["ref"],
                   "text": f"{', '.join(f'@{{{u}}}' for u in split(add, 'u:'))}"
                           f"{', ' if split(add, 'u:') and split(add, 't:') else ''}"
                           f"{', '.join(split(add, 't:'))}: please review this.",
                   "buttons": rules._buttons(pr, values)}
            chans = notify.channels_for(conn, "routed", pr["full_name"], pr["requested_teams"])
            notify.enqueue(conn, chans, "assigned", msg, pr_id)
            told = f" A notice went to {', '.join(c['name'] for c in chans)}." if chans else \
                " No channel is routed for this repo, so no notice went out."
        else:
            told = ""
    what = f"Review requested on GitHub from {', '.join(names)}." if names else "Reviewers removed on GitHub."
    flash(what + told, "ok")
    return redirect(url_for("pulls", **{k: v for k, v in request.args.items() if k != "assign"}))


SNOOZE_DAYS = (1, 3, 7)


@app.post("/pulls/<int:pr_id>/snooze")
def snooze(pr_id: int):
    if not can_act():
        abort(403)
    days = request.form.get("days", type=int)
    with db.get_conn() as conn:
        pr = db.one(conn, "SELECT p.number, r.full_name FROM pull_requests p JOIN repos r ON r.id = p.repo_id "
                          "WHERE p.id = %s AND p.state = 'open'", (pr_id,))
        if not pr:
            abort(404)
        if days in SNOOZE_DAYS:
            until = datetime.now(timezone.utc) + timedelta(days=days)
            db.execute(conn, """INSERT INTO snoozes (pr_id, until, by_name) VALUES (%s, %s, %s)
                                ON CONFLICT (pr_id) DO UPDATE SET until = EXCLUDED.until, by_name = EXCLUDED.by_name,
                                    at = NOW()""", (pr_id, until, g.user["name"]))
            settings.history_add(conn, g.user["name"], f"{pr['full_name']} #{pr['number']}: reminders snoozed for {days} day{'s' if days != 1 else ''}")
            flash(f"No reminders about this PR until {until:%a %d %b %H:%M} UTC.", "ok")
        else:
            db.execute(conn, "DELETE FROM snoozes WHERE pr_id = %s", (pr_id,))
            flash("Reminders about this PR are back on.", "ok")
    return redirect(url_for("pulls", **{k: v for k, v in request.args.items()}) + "#snooze")


# ─── Metrics ────────────────────────────────────────────────────────────────

@app.get("/metrics")
def metrics_page():
    days = request.args.get("days", type=int)
    days = days if days in dict(metrics.PERIODS) else 30
    repo = request.args.get("repo", "")
    with db.get_conn() as conn:
        m = metrics.compute(conn, days, repo)
        repos = db.rows(conn, "SELECT full_name, name FROM repos WHERE included ORDER BY name")
    return render_template("metrics.html", m=m, periods=metrics.PERIODS, days=days, repo=repo, repos=repos)


# ─── My work ────────────────────────────────────────────────────────────────

@app.get("/me")
def my_work():
    login = (g.user.get("github_login") or "").lower()
    work = None
    if login:
        with db.get_conn() as conn:
            work = data.my_work(conn, login, settings.load(conn))
    return render_template("me.html", login=login, w=work, via_github=g.user.get("via_github"))


@app.post("/me/login")
def my_login():
    if g.user.get("via_github"):
        abort(400, "Your GitHub login comes from signing in with GitHub.")
    login = request.form.get("login", "").strip().lstrip("@")
    if login and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", login):
        flash("That isn't a GitHub login.", "bad")
        return redirect(url_for("my_work"))
    with db.get_conn() as conn:
        settings.set_my_login(conn, g.user["id"], login)
    flash(f"My work now follows {login}." if login else "GitHub login removed.", "ok")
    return redirect(url_for("my_work"))


# ─── MVP ────────────────────────────────────────────────────────────────────

@app.get("/mvp")
def mvp_page():
    period = "prev" if request.args.get("period") == "prev" else "this"
    with db.get_conn() as conn:
        result = mvp.compute(conn, period)
        names = {p["login"]: p["name"] for p in db.rows(conn, "SELECT login, name FROM people WHERE name <> ''")}
        app_on = db.one(conn, "SELECT COUNT(*) AS n FROM webhook_log WHERE event = 'delete' AND status = 202 "
                              "AND received_at > NOW() - INTERVAL '30 days'")["n"] > 0
        repo_count = db.one(conn, "SELECT COUNT(*) AS n FROM repos WHERE included")["n"]
        post = rules.load(conn)["mvp"]
    return render_template("mvp.html", r=result, period=period, names=names, app_on=app_on,
                           repo_count=repo_count, post=post)


# ─── Branches ───────────────────────────────────────────────────────────────

@app.get("/branches")
def branches():
    now = datetime.now(timezone.utc)
    tab = "left" if request.args.get("tab") == "left" else "nopr"
    q = request.args.get("q", "").strip().lower()
    repo = request.args.get("repo", "")
    with db.get_conn() as conn:
        values = settings.load(conn)
        default_days = settings.number(values, "STALE_DAYS", 30)
        days = request.args.get("days", type=int) or default_days
        view = {**values, "STALE_DAYS": str(days)}
        found = data.classified_branches(conn, view, now=now)
        total = db.one(conn, "SELECT COUNT(*) AS n FROM branches b JOIN repos r ON r.id = b.repo_id WHERE r.included")["n"]
        ignored = db.rows(conn, """SELECT i.*, r.full_name, r.name AS repo_name FROM ignored_branches i
                                     JOIN repos r ON r.id = i.repo_id ORDER BY r.name, i.name""")
        digest = rules.load(conn)["digest"]
        chan_names = {c["id"]: c["name"] for c in db.rows(conn, "SELECT id, name FROM channels")}
        digest_to = [c["name"] for c in notify.channels_for(conn, str(digest["params"].get("target", "all")))]
    repos = sorted({(b["full_name"], b["repo_name"]) for b in found}, key=lambda r: r[1])
    if repo:
        found = [b for b in found if b["full_name"] == repo]
    if q:
        found = [b for b in found if q in b["name"].lower() or q in b["full_name"] or q in (b["last_committer"] or "").lower()]
    nopr = [b for b in found if b["status"] in ("stale", "active")]
    left = [b for b in found if b["status"] in ("left_merged", "left_closed", "pushed_after_merge")]
    stale = [b for b in nopr if b["status"] == "stale"]
    nopr.sort(key=lambda b: (b["status"] != "stale", -(b["age_days"] or 0)))
    left.sort(key=lambda b: b["pr_merged_at"] or b["pr_closed_at"] or now)
    return render_template("branches.html", tab=tab, rows=nopr if tab == "nopr" else left, nopr=nopr, left=left,
                           stale=stale, days=days, default_days=default_days, total=total, q=q, repo=repo,
                           repos=repos, ignored=ignored, never=settings.split_list(settings.get(values, "NEVER_FLAG")),
                           left_merged=sum(1 for b in left if b["status"] != "left_closed"),
                           left_closed=sum(1 for b in left if b["status"] == "left_closed"),
                           digest=digest, digest_to=digest_to,
                           digest_target=rules.target_label(str(digest["params"].get("target", "all")), chan_names))


@app.post("/branches/<int:branch_id>/ignore")
def ignore_branch(branch_id: int):
    if not can_act():
        abort(403)
    with db.get_conn() as conn:
        b = db.one(conn, """SELECT b.repo_id, b.name, r.full_name FROM branches b JOIN repos r ON r.id = b.repo_id
                             WHERE b.id = %s""", (branch_id,))
        if not b:
            abort(404)
        db.execute(conn, """INSERT INTO ignored_branches (repo_id, name, ignored_by) VALUES (%s, %s, %s)
                            ON CONFLICT DO NOTHING""", (b["repo_id"], b["name"], g.user["name"]))
        settings.history_add(conn, g.user["name"], f"Stopped flagging {b['full_name']}:{b['name']}")
    flash(f"{b['name']} won't be flagged again. It's listed under Ignored branches if you change your mind.", "ok")
    return redirect(request.form.get("back") or url_for("branches"))


@app.post("/branches/ignored/<int:repo_id>/<path:name>/remove")
def unignore_branch(repo_id: int, name: str):
    if not can_act():
        abort(403)
    with db.get_conn() as conn:
        db.execute(conn, "DELETE FROM ignored_branches WHERE repo_id = %s AND name = %s", (repo_id, name))
        settings.history_add(conn, g.user["name"], f"Flagging branch {name} again")
    flash(f"{name} is flagged again when it's stale or left behind.", "ok")
    return redirect(url_for("branches", show="ignored") + "#ignored")


# ─── Deliveries and scans ───────────────────────────────────────────────────

@app.get("/deliveries")
def deliveries():
    status = request.args.get("status", "all")
    with db.get_conn() as conn:
        where = "" if status == "all" else "WHERE n.status = %s"
        rows = db.rows(conn, f"""SELECT n.*, c.name AS channel_name, c.kind AS channel_kind
                                   FROM notifications n JOIN channels c ON c.id = n.channel_id {where}
                                  ORDER BY n.id DESC LIMIT 200""", (status,) if where else None)
        counts = {r["status"]: r["n"] for r in db.rows(
            conn, "SELECT status, COUNT(*) AS n FROM notifications GROUP BY 1")}
        channels = notify.channel_health(conn)
        scans = db.rows(conn, "SELECT * FROM scan_log ORDER BY id DESC LIMIT 30")
        hooks = db.rows(conn, "SELECT * FROM webhook_log ORDER BY id DESC LIMIT 30")
    for r in rows:
        p = r["payload"]
        r["subject"] = p.get("pr") or p.get("title") or ""
        r["heading"] = p.get("heading", "")
    return render_template("deliveries.html", rows=rows, status=status, counts=counts, channels=channels,
                           scans=scans, hooks=hooks)


@app.post("/scan")
def scan_now():
    if not can_act():
        abort(403)
    with db.get_conn() as conn:
        db.request_scan(conn, "*", f"asked for by {g.user['name']}")
    flash("A full scan starts within a few seconds. Reload in a minute to see the results.", "ok")
    return redirect(request.form.get("back") or url_for("index"))


# ─── GitHub webhooks ────────────────────────────────────────────────────────

@app.post("/github/webhook")
def github_webhook():
    body = request.get_data()
    event = request.headers.get("X-GitHub-Event", "")
    with db.get_conn() as conn:
        secret = settings.load(conn).get("GITHUB_WEBHOOK_SECRET", "")
        if not verify_signature(secret, body, request.headers.get("X-Hub-Signature-256", "")):
            db.execute(conn, "INSERT INTO webhook_log (event, status) VALUES (%s, 401)", (event or "?",))
            return {"error": "bad or missing signature"}, 401
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return {"error": "not JSON"}, 400
        repo = ((payload.get("repository") or {}).get("full_name") or "").lower()
        action = payload.get("action")
        status = 204
        if event == "ping":
            status = 200
        elif event in WEBHOOK_EVENTS and repo:
            if event == "delete" and payload.get("ref_type") == "branch" and payload.get("ref"):
                db.record_deletion(conn, repo, payload["ref"], (payload.get("sender") or {}).get("login"))
            db.request_scan(conn, repo, f"webhook {event}{'.' + action if action else ''}")
            status = 202
        db.execute(conn, "INSERT INTO webhook_log (event, action, repo, status) VALUES (%s, %s, %s, %s)",
                   (event or "?", action, repo or None, status))
        db.execute(conn, "DELETE FROM webhook_log WHERE received_at < NOW() - INTERVAL '14 days'")
    return ({"ok": True}, status) if status != 204 else ("", 204)


@app.get("/healthz")
def healthz():
    try:
        with db.get_conn() as conn:
            db.one(conn, "SELECT 1 AS x")
    except Exception:
        return {"ok": False, "version": VERSION}, 503
    return {"ok": True, "version": VERSION}


def configure_app() -> None:
    problem = settings.secret_key_problem()
    if problem:
        raise SystemExit(problem)
    app.secret_key = settings.session_key()
    for _ in range(30):
        try:
            db.init_schema()
            break
        except Exception as exc:  # the database may still be starting
            log.info("Waiting for the database (%s)", type(exc).__name__)
            time.sleep(2)
    settings.bootstrap()
    with db.get_conn() as conn:
        rules.seed(conn)
        settings.reseal(conn)


if __name__ == "__main__":
    configure_app()
    app.run(host="0.0.0.0", port=int(os.getenv("DASHBOARD_PORT", "8080")))
