"""
admin.py
~~~~~~~~
Sign-in, accounts and the settings pages.

Everyone signs in. Admins see and change everything; members can also request
reviewers and ignore branches; viewers only read. The first visit with no
accounts asks for the setup code the dashboard prints to its log, so whoever
reaches it first can't simply claim the admin account.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import psycopg2
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from psycopg2.extras import Json

import analysis
import db
import ghlogin
from metrics import compact as metrics_compact
import notify
import rules
import settings
import tls
from github import App, Connections, GitHubError

log = logging.getLogger("branchwarden.dashboard")

bp = Blueprint("admin", __name__)

OPEN_ENDPOINTS = {"admin.login", "admin.setup", "admin.welcome", "admin.github_login", "admin.github_callback"}
ALWAYS_OPEN = {"static", "healthz", "github_webhook"}

_setup_code: list[str] = []
_failures: dict[str, deque] = defaultdict(deque)
FAILURE_LIMIT = 5
FAILURE_WINDOW = 15 * 60


# ─── Guard: every request ───────────────────────────────────────────────────

def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@bp.app_context_processor
def _template_globals():
    return {"csrf_token": csrf_token, "current_user": g.get("user")}


@bp.before_app_request
def guard():
    g.user = None
    if request.endpoint in ALWAYS_OPEN or request.endpoint is None:
        return None
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "The form has expired. Go back, reload the page and try again.")
    with db.get_conn() as conn:
        if settings.user_count(conn) == 0:
            if request.endpoint != "admin.setup":
                return redirect(url_for("admin.setup"))
            return None
        uid = session.get("uid")
        g.user = settings.user(conn, uid) if uid else None
    if request.endpoint in OPEN_ENDPOINTS:
        return None
    if g.user is None:
        session.pop("uid", None)
        return redirect(url_for("admin.login", next=_safe_next(request.full_path)))
    if request.blueprint == "admin" and g.user["role"] != "admin":
        abort(403)
    return None


def _safe_next(target: Optional[str]) -> str:
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target.rstrip("?")


def _sign_in(user_id: int) -> None:
    csrf = session.get("csrf")
    session.clear()
    session.permanent = True
    session["uid"] = user_id
    if csrf:
        session["csrf"] = csrf


def _actor() -> str:
    return g.user["name"] if g.get("user") else "Branchwarden"


# ─── Signing in ─────────────────────────────────────────────────────────────

def _throttled(key: str) -> bool:
    now = time.monotonic()
    q = _failures[key]
    while q and now - q[0] > FAILURE_WINDOW:
        q.popleft()
    return len(q) >= FAILURE_LIMIT


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect("/")
    error = None
    email = request.form.get("email", "").strip()
    nxt = _safe_next(request.values.get("next"))
    if request.method == "POST":
        key = f"{request.remote_addr}|{email.lower()}"
        if _throttled(key):
            error = "Too many attempts. Wait 15 minutes, or ask an admin for a setup link."
        else:
            with db.get_conn() as conn:
                user = settings.check_login(conn, email, request.form.get("password", ""))
            if user:
                _failures.pop(key, None)
                _sign_in(user["id"])
                return redirect(nxt)
            _failures[key].append(time.monotonic())
            error = "That email and password don't match an account."
    with db.get_conn() as conn:
        github_on = _github_login_ready(settings.load(conn))
    return render_template("login.html", error=error or request.args.get("error"), email=email, next=nxt,
                           github_on=github_on)


# ─── Sign in with GitHub ────────────────────────────────────────────────────

def _github_login_ready(values: dict) -> bool:
    return settings.flag(values, "GITHUB_LOGIN_ENABLED") and bool(values.get("GITHUB_OAUTH_CLIENT_ID")) \
        and bool(values.get("GITHUB_OAUTH_CLIENT_SECRET"))


def _callback_url(values: dict) -> str:
    base = settings.get(values, "PUBLIC_URL").rstrip("/") or request.url_root.rstrip("/")
    return base + url_for("admin.github_callback")


@bp.get("/login/github")
def github_login():
    with db.get_conn() as conn:
        values = settings.load(conn)
    if not _github_login_ready(values):
        abort(404)
    verifier, challenge = ghlogin.pkce_pair()
    state = secrets.token_urlsafe(24)
    session["gh_oauth"] = {"state": state, "verifier": verifier, "next": _safe_next(request.args.get("next"))}
    return redirect(ghlogin.authorize_url(settings.get(values, "GITHUB_API_URL"), values["GITHUB_OAUTH_CLIENT_ID"],
                                          _callback_url(values), state, challenge))


@bp.get("/login/github/callback")
def github_callback():
    pending = session.pop("gh_oauth", None)
    with db.get_conn() as conn:
        values = settings.load(conn)
    if not _github_login_ready(values) or not pending:
        return redirect(url_for("admin.login", error="That sign-in has expired. Try again."))
    if not secrets.compare_digest(request.args.get("state", ""), pending["state"]):
        return redirect(url_for("admin.login", error="That sign-in didn't come from here. Try again."))
    if request.args.get("error"):
        return redirect(url_for("admin.login", error="GitHub sign-in was cancelled."))
    key = f"{request.remote_addr}|github"
    if _throttled(key):
        return redirect(url_for("admin.login", error="Too many attempts. Wait 15 minutes."))
    api = settings.get(values, "GITHUB_API_URL")
    try:
        token = ghlogin.exchange(api, values["GITHUB_OAUTH_CLIENT_ID"], values["GITHUB_OAUTH_CLIENT_SECRET"],
                                 request.args.get("code", ""), _callback_url(values), pending["verifier"])
        who = ghlogin.identity(api, token)
    except GitHubError as exc:
        _failures[key].append(time.monotonic())
        log.warning("GitHub sign-in failed: %s", exc)
        return redirect(url_for("admin.login", error=f"GitHub sign-in failed: {exc}"))
    mapping = {r: settings.split_list(settings.get(values, f"GITHUB_ROLE_{r.upper()}")) for r in settings.ROLES}
    role = ghlogin.role_for(who, mapping)
    if not role:
        _failures[key].append(time.monotonic())
        log.info("GitHub sign-in refused for %s: in none of the role lists", who["login"])
        return redirect(url_for("admin.login", error=f"{who['login']} isn't allowed in. Ask an admin to add you "
                                                     "or one of your GitHub teams under Users and access."))
    with db.get_conn() as conn:
        user_id = settings.github_user(conn, who, role, ghlogin.noreply_email(who["id"], who["login"]))
    _sign_in(user_id)
    return redirect(pending.get("next") or "/")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


def _the_setup_code() -> str:
    if not _setup_code:
        code = "-".join(secrets.token_hex(3) for _ in range(3))
        _setup_code.append(code)
        log.warning("First run: no accounts yet. Open the dashboard and enter this "
                    "setup code to create the first admin: %s", code)
    return _setup_code[0]


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    with db.get_conn() as conn:
        if settings.user_count(conn):
            return redirect(url_for("admin.login"))
        summary = settings.imported_summary(conn)
    code = _the_setup_code()
    error = None
    form = request.form
    if request.method == "POST":
        problem = settings.password_problem(form.get("password", ""))
        if not secrets.compare_digest(form.get("code", "").strip().lower(), code):
            error = "That isn't the setup code. It's in the dashboard's log."
        elif not form.get("name", "").strip() or "@" not in form.get("email", ""):
            error = "Enter your name and email."
        elif problem:
            error = problem
        else:
            with db.get_conn() as conn:
                if settings.user_count(conn):
                    return redirect(url_for("admin.login"))
                new = settings.create_user(conn, form["email"], form["name"], "admin", "Branchwarden",
                                           password=form["password"])
            _setup_code.clear()
            _sign_in(new.user_id)
            flash("Your admin account is ready. Next, connect GitHub.", "ok")
            return redirect(url_for("admin.github"))
    return render_template("setup.html", error=error, summary=summary, form=form)


@bp.route("/welcome/<token>", methods=["GET", "POST"])
def welcome(token: str):
    with db.get_conn() as conn:
        person = settings.user_by_token(conn, token)
    if not person:
        return render_template("welcome.html", expired=True, person=None, error=None), 404
    error = None
    if request.method == "POST":
        error = settings.password_problem(request.form.get("password", ""), request.form.get("confirm", ""))
        if not error:
            with db.get_conn() as conn:
                settings.set_password(conn, person["id"], request.form["password"])
                settings.history_add(conn, person["name"], "Set a password from a setup link")
            _sign_in(person["id"])
            return redirect("/")
    return render_template("welcome.html", expired=False, person=person, error=error)


def _saved(lines: list[str]) -> None:
    if lines:
        flash(f"Saved {len(lines)} change{'s' if len(lines) != 1 else ''}. "
              "The worker uses them from its next scan.", "ok")
    else:
        flash("Nothing changed.", "info")


# ─── Settings: GitHub ───────────────────────────────────────────────────────

@bp.get("/settings")
def settings_home():
    return redirect(url_for("admin.github"))


def _stored(meta: dict, key: str, unreadable: set) -> Optional[str]:
    m = meta.get(key)
    if not m:
        return None
    if key in unreadable:
        return "can't be read: the secret key has changed since it was set. Enter it again"
    return f"set {m['updated_at']:%d %b %Y} by {m['updated_by'] or 'Branchwarden'}"


def _token_expiry(value: str) -> Optional[dict]:
    if not value:
        return None
    try:
        when = datetime.strptime(value.replace(" UTC", "").strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return {"text": value, "days": None, "tone": "muted"}
    days = (when - datetime.now(timezone.utc)).days
    return {"text": f"{when:%d %b %Y}", "days": days,
            "tone": "bad" if days < 0 else "warn" if days <= 14 else "good"}


@bp.get("/settings/github")
def github():
    unreadable: set = set()
    with db.get_conn() as conn:
        values = settings.load(conn, unreadable)
        meta = settings.secret_meta(conn)
        targets = db.rows(conn, "SELECT * FROM targets ORDER BY login")
        last_hook = db.one(conn, "SELECT * FROM webhook_log ORDER BY id DESC LIMIT 1")
    app_info, installs, app_error = None, {}, None
    if values.get("GITHUB_APP_ID") and values.get("GITHUB_APP_PRIVATE_KEY"):
        try:
            app = App(settings.get(values, "GITHUB_API_URL"), values["GITHUB_APP_ID"],
                      values["GITHUB_APP_PRIVATE_KEY"])
            app_info = app.details()
            installs = app.installations(refresh=True)
        except GitHubError as exc:
            app_error = str(exc)
        except Exception as exc:  # a malformed private key fails inside PyJWT
            app_error = f"the private key can't be used ({type(exc).__name__})"
    public = settings.get(values, "PUBLIC_URL").rstrip("/") or request.url_root.rstrip("/")
    return render_template(
        "settings_github.html", values=values, get=lambda k: settings.get(values, k),
        flag=lambda k: settings.flag(values, k), stored=lambda k: _stored(meta, k, unreadable),
        targets=targets, installs=installs, app_info=app_info, app_error=app_error,
        webhook_url=f"{public}/github/webhook", last_hook=last_hook,
        token_expiry=_token_expiry(values.get("GITHUB_TOKEN_EXPIRES", "")),
        intervals=settings.SCAN_INTERVALS, suggested_secret=secrets.token_hex(24))


@bp.post("/settings/github/app")
def github_app():
    form = request.form
    changes = {"GITHUB_APP_ENABLED": "true" if form.get("GITHUB_APP_ENABLED") else "false",
               "GITHUB_APP_ID": form.get("GITHUB_APP_ID", "").strip()}
    key = form.get("GITHUB_APP_PRIVATE_KEY", "").strip()
    if key:
        if "PRIVATE KEY-----" not in key:
            flash("The private key should be the whole .pem file GitHub gave you, BEGIN and END lines included.", "bad")
            return redirect(url_for("admin.github"))
        changes["GITHUB_APP_PRIVATE_KEY"] = key
    elif form.get("remove_key"):
        changes["GITHUB_APP_PRIVATE_KEY"] = None
    secret = form.get("GITHUB_WEBHOOK_SECRET", "").strip()
    if secret and len(secret) < 20:
        flash("Use a webhook secret of at least 20 random characters.", "bad")
        return redirect(url_for("admin.github") + "#app")
    if secret:
        changes["GITHUB_WEBHOOK_SECRET"] = secret
    if changes["GITHUB_APP_ID"] and not changes["GITHUB_APP_ID"].isdigit():
        flash("The App ID is a number, shown on the App's settings page.", "bad")
        return redirect(url_for("admin.github"))
    with db.get_conn() as conn:
        _saved(settings.save(conn, changes, _actor()))
    return redirect(url_for("admin.github") + "#app")


@bp.post("/settings/github/token")
def github_token():
    form = request.form
    changes = {"GITHUB_TOKEN_ENABLED": "true" if form.get("GITHUB_TOKEN_ENABLED") else "false"}
    token = form.get("GITHUB_TOKEN", "").strip()
    if token:
        changes["GITHUB_TOKEN"] = token
        changes["GITHUB_TOKEN_EXPIRES"] = None
    elif form.get("remove_token"):
        changes["GITHUB_TOKEN"] = None
        changes["GITHUB_TOKEN_EXPIRES"] = None
    with db.get_conn() as conn:
        _saved(settings.save(conn, changes, _actor()))
    return redirect(url_for("admin.github") + "#token")


@bp.post("/settings/github/test")
def github_test():
    with db.get_conn() as conn:
        values = settings.load(conn)
    conns = Connections(values)
    results = []
    if conns.app:
        try:
            info = conns.app.details()
            n = len(conns.app.installations(refresh=True))
            results.append(f"GitHub App “{info.get('name')}” answered: installed on {n} account{'s' if n != 1 else ''}.")
        except GitHubError as exc:
            results.append(f"GitHub App: {exc}.")
        except Exception as exc:
            results.append(f"GitHub App: the private key can't be used ({type(exc).__name__}).")
    if conns.token:
        try:
            who = conns.token.viewer()
            exp = conns.token.token_expires
            results.append(f"Token works, as {who.get('login')}" + (f"; it expires {exp}." if exp else "."))
            if exp:
                with db.get_conn() as conn:
                    settings.save(conn, {"GITHUB_TOKEN_EXPIRES": exp}, "Branchwarden", record=False)
        except GitHubError as exc:
            results.append(f"Token: {exc}.")
    if not results:
        results.append("Neither a GitHub App nor a token is set up and switched on.")
    flash(" ".join(results), "info")
    return redirect(url_for("admin.github"))


@bp.post("/settings/github/scan")
def github_scan():
    form = request.form
    interval = form.get("SCAN_INTERVAL_MINUTES", "10")
    stale = form.get("STALE_DAYS", "30").strip()
    if not stale.isdigit() or not 1 <= int(stale) <= 3650:
        flash("Stale after must be a number of days.", "bad")
        return redirect(url_for("admin.github") + "#scan")
    keep_pr, keep_log = form.get("RETAIN_PR_DAYS", "365").strip(), form.get("RETAIN_LOG_DAYS", "90").strip()
    if not (keep_pr.isdigit() and 30 <= int(keep_pr) <= 3650 and keep_log.isdigit() and 7 <= int(keep_log) <= 3650):
        flash("Keep closed PRs for 30 to 3650 days, and messages and scans for 7 to 3650 days.", "bad")
        return redirect(url_for("admin.github") + "#scan")
    changes = {
        "REPO_INCLUDE": settings.join_list(settings.split_list(form.get("REPO_INCLUDE", ""))) or "*",
        "REPO_EXCLUDE": settings.join_list(settings.split_list(form.get("REPO_EXCLUDE", ""))),
        "INCLUDE_ARCHIVED": "true" if form.get("INCLUDE_ARCHIVED") else "false",
        "INCLUDE_FORKS": "true" if form.get("INCLUDE_FORKS") else "false",
        "STALE_DAYS": stale,
        "RETAIN_PR_DAYS": keep_pr,
        "RETAIN_LOG_DAYS": keep_log,
        "NEVER_FLAG": settings.join_list(settings.split_list(form.get("NEVER_FLAG", ""))),
        "PUBLIC_URL": form.get("PUBLIC_URL", "").strip().rstrip("/"),
        "GITHUB_API_URL": form.get("GITHUB_API_URL", "").strip().rstrip("/") or "https://api.github.com",
    }
    if interval in dict(settings.SCAN_INTERVALS):
        changes["SCAN_INTERVAL_MINUTES"] = interval
    for key in ("PUBLIC_URL", "GITHUB_API_URL"):
        if changes[key] and urlsplit(changes[key]).scheme != "https":
            flash(f"{settings.label(key)} must start with https://", "bad")
            return redirect(url_for("admin.github") + "#scan")
    with db.get_conn() as conn:
        lines = settings.save(conn, changes, _actor())
        if any(k in " ".join(lines) for k in ("Only repos", "Skip repos", "archived", "forks")):
            db.request_scan(conn, "*", "scan settings changed")
    _saved(lines)
    return redirect(url_for("admin.github") + "#scan")


LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


@bp.post("/settings/targets/new")
def target_new():
    login = request.form.get("login", "").strip().lstrip("@").lower()
    kind = request.form.get("kind", "org")
    via = request.form.get("via", "auto")
    if not LOGIN.match(login) or kind not in ("org", "user") or via not in ("auto", "app", "token"):
        flash("Enter a GitHub organisation or account name.", "bad")
        return redirect(url_for("admin.github") + "#targets")
    with db.get_conn() as conn:
        try:
            db.execute(conn, "INSERT INTO targets (login, kind, via) VALUES (%s, %s, %s)", (login, kind, via))
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            flash(f"{login} is already on the list.", "info")
            return redirect(url_for("admin.github") + "#targets")
        settings.history_add(conn, _actor(), f"Added {kind} {login} to scan")
        db.request_scan(conn, "*", f"{login} added")
    flash(f"{login} added. Its first scan starts within a few seconds; it records what's there "
          "without sending any messages.", "ok")
    return redirect(url_for("admin.github") + "#targets")


@bp.post("/settings/targets/<int:target_id>")
def target_save(target_id: int):
    form = request.form
    with db.get_conn() as conn:
        t = db.one(conn, "SELECT * FROM targets WHERE id = %s", (target_id,))
        if not t:
            abort(404)
        if form.get("delete"):
            db.execute(conn, "DELETE FROM targets WHERE id = %s", (target_id,))
            settings.history_add(conn, _actor(), f"Stopped scanning {t['login']}")
            flash(f"{t['login']} removed, with its repos, PRs and branches.", "ok")
        elif form.get("toggle"):
            db.execute(conn, "UPDATE targets SET enabled = NOT enabled WHERE id = %s", (target_id,))
            settings.history_add(conn, _actor(), f"{t['login']}: scanning {'off' if t['enabled'] else 'on'}")
            flash(f"Scanning {t['login']} switched {'off' if t['enabled'] else 'on'}.", "ok")
        elif form.get("via") in ("auto", "app", "token"):
            db.execute(conn, "UPDATE targets SET via = %s WHERE id = %s", (form["via"], target_id))
            settings.history_add(conn, _actor(), f"{t['login']}: reached through {form['via']}")
            flash(f"{t['login']} is now reached through {form['via']}.", "ok")
    return redirect(url_for("admin.github") + "#targets")


# ─── Settings: channels and reminders ───────────────────────────────────────

def _channel_url_problem(kind: str, url: str) -> Optional[str]:
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


@bp.get("/settings/notifications")
def notifications():
    open_id = request.args.get("open", "")
    unreadable: set = set()
    with db.get_conn() as conn:
        values = settings.load(conn, unreadable)
        meta = settings.secret_meta(conn)
        chans = notify.channel_health(conn)
        all_rules = rules.load(conn)
    names = {c["id"]: c["name"] for c in chans}
    for c in chans:
        key = settings.channel_url_key(c["id"])
        c["stored"] = _stored(meta, key, unreadable)
        c["url_host"] = urlsplit(values.get(key, "")).hostname or ""
        c["bot"] = bool(c.get("slack_channel")) and settings.channel_token_key(c["id"]) in values
        c["bot_stored"] = _stored(meta, settings.channel_token_key(c["id"]), unreadable)
        c["routes"] = ", ".join(filter(None, [c["repos"], c["teams"]])) or "every repo"
    rule_list = []
    for r in all_rules.values():
        rule_list.append({**r, "desc": rules.describe(r, names),
                          "to": rules.target_label(str(r["params"].get("target", "routed")), names)})
    return render_template("settings_notifications.html", channels=chans, rules=rule_list, open_id=open_id,
                           get=lambda k: settings.get(values, k), units=rules.UNITS, days=analysis.DAYS)


SLACK_CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{6,}$")


def _slack_bot_problem(token: str, channel: str) -> Optional[str]:
    if not token.startswith("xoxb-"):
        return "A Slack bot token starts with xoxb- (OAuth & Permissions › Bot User OAuth Token)"
    if not SLACK_CHANNEL_ID.match(channel):
        return "The Slack channel ID looks like C01ABCDEF23 (channel details › About, at the bottom)"
    return None


@bp.post("/settings/channels/new")
def channel_new():
    form = request.form
    kind = form.get("kind")
    name = form.get("name", "").strip()
    url = form.get("url", "").strip()
    token = form.get("bot_token", "").strip()
    slack_channel = form.get("slack_channel", "").strip().upper()
    back = redirect(url_for("admin.notifications", open=f"new-{kind}") + "#new")
    if kind not in ("teams", "slack") or not name:
        flash("Give the channel a name and a webhook URL.", "bad")
        return back
    if kind == "slack" and (token or slack_channel):
        problem = _slack_bot_problem(token, slack_channel)
        url = ""
    else:
        problem = _channel_url_problem(kind, url)
    if problem:
        flash(problem, "bad")
        return back
    with db.get_conn() as conn:
        cid = db.one(conn, """INSERT INTO channels (kind, name, repos, teams, slack_channel) VALUES (%s, %s, %s, %s, %s)
                              RETURNING id""",
                     (kind, name, settings.join_list(settings.split_list(form.get("repos", ""))),
                      settings.join_list(t.lower().lstrip("@") for t in settings.split_list(form.get("teams", ""))),
                      slack_channel if token else ""))["id"]
        secret = {settings.channel_token_key(cid): token} if token else {settings.channel_url_key(cid): url}
        settings.save(conn, secret, _actor(), record=False)
        settings.history_add(conn, _actor(), f"Added {kind} channel {name}" + (" (bot token, threads)" if token else ""))
    flash(f"{name} added. Send a test message to check it.", "ok")
    return redirect(url_for("admin.notifications", open=cid) + f"#ch-{cid}")


def _channel_or_404(conn, cid: int) -> dict:
    c = db.one(conn, "SELECT * FROM channels WHERE id = %s", (cid,))
    if not c:
        abort(404)
    return c


@bp.post("/settings/channels/<int:cid>")
def channel_save(cid: int):
    form = request.form
    with db.get_conn() as conn:
        c = _channel_or_404(conn, cid)
        if form.get("delete"):
            db.execute(conn, "DELETE FROM channels WHERE id = %s", (cid,))
            settings.save(conn, {settings.channel_url_key(cid): None, settings.channel_token_key(cid): None},
                          _actor(), record=False)
            settings.history_add(conn, _actor(), f"Removed channel {c['name']}")
            flash(f"{c['name']} removed, with its queued messages.", "ok")
            return redirect(url_for("admin.notifications") + "#channels")
        if form.get("toggle"):
            db.execute(conn, "UPDATE channels SET enabled = NOT enabled WHERE id = %s", (cid,))
            settings.history_add(conn, _actor(), f"{c['name']}: switched {'off' if c['enabled'] else 'on'}")
            flash(f"{c['name']} switched {'off' if c['enabled'] else 'on'}.", "ok")
            return redirect(url_for("admin.notifications") + f"#ch-{cid}")
        url = form.get("url", "").strip()
        token = form.get("bot_token", "").strip()
        slack_channel = form.get("slack_channel", "").strip().upper()
        if url:
            problem = _channel_url_problem(c["kind"], url)
            if problem:
                flash(problem, "bad")
                return redirect(url_for("admin.notifications", open=cid) + f"#ch-{cid}")
            settings.save(conn, {settings.channel_url_key(cid): url, settings.channel_token_key(cid): None}, _actor())
            db.execute(conn, "UPDATE channels SET slack_channel = '' WHERE id = %s", (cid,))
        elif c["kind"] == "slack" and (token or slack_channel != c["slack_channel"]):
            has_token = bool(token) or settings.channel_token_key(cid) in settings.secret_meta(conn)
            problem = _slack_bot_problem(token or "xoxb-kept", slack_channel) if has_token else \
                "Enter the bot token too, or a webhook URL instead."
            if problem:
                flash(problem, "bad")
                return redirect(url_for("admin.notifications", open=cid) + f"#ch-{cid}")
            if token:
                settings.save(conn, {settings.channel_token_key(cid): token, settings.channel_url_key(cid): None},
                              _actor())
            db.execute(conn, "UPDATE channels SET slack_channel = %s WHERE id = %s", (slack_channel, cid))
        repos = settings.join_list(settings.split_list(form.get("repos", "")))
        teams = settings.join_list(t.lower().lstrip("@") for t in settings.split_list(form.get("teams", "")))
        name = form.get("name", "").strip() or c["name"]
        db.execute(conn, "UPDATE channels SET name = %s, repos = %s, teams = %s WHERE id = %s",
                   (name, repos, teams, cid))
        if (name, repos, teams) != (c["name"], c["repos"], c["teams"]):
            settings.history_add(conn, _actor(), f"Channel {name}: repos [{repos or 'all'}], teams [{teams or 'none'}]")
    flash(f"{name} saved.", "ok")
    return redirect(url_for("admin.notifications", open=cid) + f"#ch-{cid}")


@bp.post("/settings/channels/<int:cid>/test")
def channel_test(cid: int):
    with db.get_conn() as conn:
        c = _channel_or_404(conn, cid)
        values = settings.load(conn)
        login = None
        person = db.one(conn, "SELECT login FROM people WHERE teams_id <> '' OR slack_id <> '' LIMIT 1")
        if person:
            login = person["login"]
        msg = {"heading": "Test from Branchwarden", "tone": "info",
               "text": f"Sent by {_actor()} from Settings › Channels and reminders, to check this channel works."
                       + (f" Mention check: @{{{login}}}" if login else ""),
               "facts": [["Channel", c["name"]], ["Kind", "Teams Adaptive Card" if c["kind"] == "teams" else "Slack Block Kit"]],
               "buttons": [["Open Branchwarden", (settings.get(values, "PUBLIC_URL") or request.url_root).rstrip("/")]]}
        started = time.monotonic()
        try:
            notify.send(conn, c, msg)
            took = time.monotonic() - started
            flash(f"Test sent and accepted ({took * 1000:.0f} ms).", "ok")
        except (notify.PermanentFailure, notify.RetryLater) as exc:
            flash(f"The test failed: {exc}.", "bad")
    return redirect(url_for("admin.notifications", open=cid) + f"#ch-{cid}")


HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@bp.post("/settings/workhours")
def workhours():
    form = request.form
    days = [d for d in analysis.DAYS if form.get(f"day_{d}")]
    start, end, tz = form.get("WORK_START", ""), form.get("WORK_END", ""), form.get("WORK_TZ", "").strip()
    if not days or not HHMM.match(start) or not HHMM.match(end) or start >= end:
        flash("Pick at least one day, and a start time before the end time (HH:MM).", "bad")
        return redirect(url_for("admin.notifications") + "#hours")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:
        flash(f"{tz} isn't a time zone name like Europe/London.", "bad")
        return redirect(url_for("admin.notifications") + "#hours")
    with db.get_conn() as conn:
        _saved(settings.save(conn, {"WORK_DAYS": ",".join(days), "WORK_START": start, "WORK_END": end,
                                    "WORK_TZ": tz}, _actor()))
    return redirect(url_for("admin.notifications") + "#hours")


def _num(raw: str, lo: float, hi: float) -> Optional[float]:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


@bp.post("/settings/rules/<key>")
def rule_save(key: str):
    rule = rules.RULE_BY_KEY.get(key)
    if not rule:
        abort(404)
    form = request.form
    with db.get_conn() as conn:
        current = rules.load(conn)[key]
        if form.get("toggle"):
            db.execute(conn, "UPDATE rules SET enabled = NOT enabled, updated_at = NOW() WHERE key = %s", (key,))
            settings.history_add(conn, _actor(), f"Rule {rule.name}: switched {'off' if current['enabled'] else 'on'}")
            flash(f"{rule.name} switched {'off' if current['enabled'] else 'on'}.", "ok")
            return redirect(url_for("admin.notifications") + f"#rule-{key}")
        if form.get("send_now") and key == "mvp":
            n = rules.run_mvp_post(conn, settings.load(conn), force=True)
            flash(f"MVP post queued for {n} channel{'s' if n != 1 else ''}." if n
                  else "Nothing to post: no reviews, merges, prunes or new PRs in the last 7 days, or no channels.",
                  "ok" if n else "info")
            return redirect(url_for("admin.notifications") + f"#rule-{key}")
        if form.get("send_now") and key == "digest":
            n = rules.run_digest(conn, settings.load(conn), force=True)
            flash(f"Digest queued for {n} channel{'s' if n != 1 else ''}." if n
                  else "Nothing to put in a digest: no stale or left-behind branches in any channel's repos.",
                  "ok" if n else "info")
            return redirect(url_for("admin.notifications") + f"#rule-{key}")
        p = dict(current["params"])
        target = form.get("target", "routed")
        if target in ("routed", "all") or (target.isdigit() and db.one(conn, "SELECT 1 AS x FROM channels WHERE id = %s",
                                                                        (int(target),))):
            p["target"] = target
        p["mention"] = bool(form.get("mention"))
        if rule.kind == "reminder":
            after, repeat, mx = _num(form.get("after"), 0, 10000), _num(form.get("repeat"), 0.1, 10000), _num(form.get("max"), 1, 50)
            if after is None or repeat is None or mx is None or form.get("unit") not in dict(rules.UNITS):
                flash("Use numbers for the wait, the repeat and the maximum.", "bad")
                return redirect(url_for("admin.notifications", open=key) + f"#rule-{key}")
            p.update(after=metrics_compact(after), repeat=metrics_compact(repeat), max=int(mx), unit=form["unit"],
                     skip_label=form.get("skip_label", "").strip(), group=bool(form.get("group")))
        elif rule.kind == "assign":
            after, count = _num(form.get("after"), 0, 10000), _num(form.get("count"), 1, 5)
            if after is None or count is None or form.get("unit") not in dict(rules.UNITS):
                flash("Use a number for the wait, and 1 to 5 reviewers.", "bad")
                return redirect(url_for("admin.notifications", open=key) + f"#rule-{key}")
            p.update(after=metrics_compact(after), unit=form["unit"], count=int(count), codeowners=bool(form.get("codeowners")),
                     pool=settings.join_list(e.lower().lstrip("@") for e in settings.split_list(form.get("pool", ""))),
                     repos=settings.join_list(settings.split_list(form.get("repos", ""))),
                     skip_label=form.get("skip_label", "").strip())
        elif rule.kind in ("digest", "post"):
            if form.get("day") not in analysis.DAYS or not HHMM.match(form.get("time", "")):
                flash("Pick a day and a time (HH:MM).", "bad")
                return redirect(url_for("admin.notifications", open=key) + f"#rule-{key}")
            p.update(day=form["day"], time=form["time"])
        db.execute(conn, "UPDATE rules SET params = %s, updated_at = NOW() WHERE key = %s", (Json(p), key))
        settings.history_add(conn, _actor(), f"Rule {rule.name}: settings changed")
    flash(f"{rule.name} saved.", "ok")
    return redirect(url_for("admin.notifications") + f"#rule-{key}")


# ─── Settings: people ───────────────────────────────────────────────────────

@bp.get("/settings/people")
def people():
    with db.get_conn() as conn:
        known = db.rows(conn, """
            WITH logins AS (
                SELECT author AS login FROM pull_requests WHERE author IS NOT NULL
                UNION SELECT x->>'user' FROM pull_requests, jsonb_array_elements(requested) x WHERE x ? 'user'
                UNION SELECT r->>'login' FROM pull_requests, jsonb_array_elements(reviews) r
                UNION SELECT last_committer FROM branches WHERE last_committer IS NOT NULL AND last_committer !~ '\\s'
                UNION SELECT login FROM people)
            SELECT l.login, p.name, p.teams_id, p.slack_id,
                   (SELECT COUNT(*) FROM pull_requests pr WHERE pr.state = 'open' AND pr.author = l.login) AS open_prs
              FROM logins l LEFT JOIN people p ON p.login = l.login
             WHERE l.login IS NOT NULL AND l.login !~ '\\[bot\\]$'
             ORDER BY (p.login IS NULL), l.login""")
    q = request.args.get("q", "").strip().lower()
    if q:
        known = [k for k in known if q in k["login"] or q in (k["name"] or "").lower()]
    return render_template("settings_people.html", people=known, q=q,
                           matched=sum(1 for k in known if k["teams_id"] or k["slack_id"]))


SLACK_ID = re.compile(r"^[UW][A-Z0-9]{6,}$")


@bp.post("/settings/people")
def people_save():
    form = request.form
    logins = form.getlist("login")
    changed = 0
    bad = []
    with db.get_conn() as conn:
        for i, login in enumerate(logins):
            login = login.strip().lower()
            if not LOGIN.match(login.replace("[bot]", "")):
                continue
            name = form.getlist("name")[i].strip()
            teams_id = form.getlist("teams_id")[i].strip()
            slack_id = form.getlist("slack_id")[i].strip().upper()
            if slack_id and not SLACK_ID.match(slack_id):
                bad.append(login)
                slack_id = ""
            old = db.one(conn, "SELECT * FROM people WHERE login = %s", (login,))
            if old and (old["name"], old["teams_id"], old["slack_id"]) == (name, teams_id, slack_id):
                continue
            if not old and not (name or teams_id or slack_id):
                continue
            db.execute(conn, """INSERT INTO people (login, name, teams_id, slack_id, updated_at) VALUES (%s, %s, %s, %s, NOW())
                                ON CONFLICT (login) DO UPDATE SET name = EXCLUDED.name, teams_id = EXCLUDED.teams_id,
                                    slack_id = EXCLUDED.slack_id, updated_at = NOW()""",
                       (login, name, teams_id, slack_id))
            changed += 1
        if changed:
            settings.history_add(conn, _actor(), f"People: updated {changed}")
    if bad:
        flash(f"Slack member IDs look like U01ABCDEF2 (Profile › ⋮ › Copy member ID); left blank for {', '.join(bad)}.", "bad")
    flash(f"Saved {changed} {'person' if changed == 1 else 'people'}." if changed else "Nothing changed.",
          "ok" if changed else "info")
    return redirect(url_for("admin.people", q=request.args.get("q", "")))


# ─── Settings: TLS ──────────────────────────────────────────────────────────

@bp.get("/settings/tls")
def tls_page():
    info, error = None, None
    if tls.enabled():
        try:
            info = tls.describe()
        except (OSError, ValueError) as exc:
            error = f"Can't read the current certificate ({type(exc).__name__})."
    return render_template("settings_tls.html", enabled=tls.enabled(), info=info, error=error,
                           has_previous=tls.has_previous())


def _pem_field(name: str) -> bytes:
    f = request.files.get(name)
    if f and f.filename:
        return f.read(256 * 1024)
    return request.form.get(name + "_text", "").strip().encode()


@bp.post("/settings/tls")
def tls_save():
    if not tls.enabled():
        abort(400, "TLS is switched off (BRANCHWARDEN_TLS=off).")
    form = request.form
    if form.get("restore"):
        ok = tls.restore_previous()
        flash("The previous certificate is back. The dashboard reloads it in a few seconds."
              if ok else "There's no previous certificate.", "ok" if ok else "info")
        with db.get_conn() as conn:
            settings.history_add(conn, _actor(), "TLS: restored the previous certificate")
        return redirect(url_for("admin.tls_page"))
    if form.get("self_signed"):
        cert, key = tls.make_self_signed()
    else:
        cert, key = _pem_field("cert"), _pem_field("key")
        chain = _pem_field("chain")
        if chain:
            cert = cert.rstrip() + b"\n" + chain.strip() + b"\n"
        problem = tls.check(cert, key)
        if problem:
            flash(problem + " Nothing was changed.", "bad")
            return redirect(url_for("admin.tls_page"))
    tls.install(cert, key)
    info = tls.describe(cert)
    with db.get_conn() as conn:
        settings.history_add(conn, _actor(), f"TLS: installed {'a new self-signed' if form.get('self_signed') else 'an uploaded'} "
                             f"certificate ({info['fingerprint'][:23]}…)")
    flash("Certificate installed. The dashboard reloads it in a few seconds: reload this page then. "
          "If it doesn't come back, restore the previous one from the log's instructions or the certs volume.", "ok")
    return redirect(url_for("admin.tls_page"))


@bp.post("/settings/github-login")
def github_login_save():
    form = request.form
    changes = {"GITHUB_LOGIN_ENABLED": "true" if form.get("GITHUB_LOGIN_ENABLED") else "false",
               "GITHUB_OAUTH_CLIENT_ID": form.get("GITHUB_OAUTH_CLIENT_ID", "").strip()}
    if form.get("GITHUB_OAUTH_CLIENT_SECRET", "").strip():
        changes["GITHUB_OAUTH_CLIENT_SECRET"] = form["GITHUB_OAUTH_CLIENT_SECRET"].strip()
    for role in settings.ROLES:
        key = f"GITHUB_ROLE_{role.upper()}"
        changes[key] = settings.join_list(e.lower().lstrip("@") for e in settings.split_list(form.get(key, "")))
    if changes["GITHUB_LOGIN_ENABLED"] == "true" and not any(changes[f"GITHUB_ROLE_{r.upper()}"] for r in settings.ROLES):
        flash("Name at least one team, organisation or login in the role lists, or nobody could sign in with GitHub.", "bad")
        return redirect(url_for("admin.users") + "#github")
    with db.get_conn() as conn:
        _saved(settings.save(conn, changes, _actor()))
    return redirect(url_for("admin.users") + "#github")


# ─── Settings: users ────────────────────────────────────────────────────────

def _link(token: str) -> str:
    return request.url_root.rstrip("/") + url_for("admin.welcome", token=token)


@bp.get("/settings/users")
def users(new_link: Optional[dict] = None, error: Optional[str] = None, adding: bool = False,
          form: Optional[dict] = None):
    with db.get_conn() as conn:
        people_rows = settings.users(conn)
        history = settings.history(conn)
    now = datetime.now(timezone.utc)
    for p in people_rows:
        if p["active"]:
            p["last"] = f"{p['last_login']:%d %b %Y %H:%M}" if p["last_login"] else "Never"
        elif p["setup_expires"] and p["setup_expires"] > now:
            hours = max(1, int((p["setup_expires"] - now).total_seconds() // 3600))
            p["last"] = f"Not yet: setup link expires in {hours} h"
        else:
            p["last"] = "Not yet: setup link expired"
    with db.get_conn() as conn:
        unreadable: set = set()
        values = settings.load(conn, unreadable)
        meta = settings.secret_meta(conn)
    gh = {"enabled": settings.flag(values, "GITHUB_LOGIN_ENABLED"),
          "client_id": values.get("GITHUB_OAUTH_CLIENT_ID", ""),
          "secret": _stored(meta, "GITHUB_OAUTH_CLIENT_SECRET", unreadable),
          "callback": _callback_url(values),
          "roles": {r: settings.get(values, f"GITHUB_ROLE_{r.upper()}") for r in settings.ROLES}}
    return render_template("settings_users.html", people=people_rows, history=history, new_link=new_link,
                           error=error, adding=adding or request.args.get("add") == "1", form=form or {},
                           roles=settings.ROLES, gh=gh)


@bp.post("/settings/users/new")
def user_new():
    form = request.form
    name, email, role = form.get("name", "").strip(), form.get("email", "").strip(), form.get("role")
    if not name or "@" not in email or role not in settings.ROLES:
        return users(error="Enter a name, an email and a role.", adding=True, form=form)
    with db.get_conn() as conn:
        try:
            new = settings.create_user(conn, email, name, role, _actor())
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            return users(error=f"There's already an account for {email}.", adding=True, form=form)
    return users(new_link={"name": name, "role": role, "url": _link(new.token)})


@bp.post("/settings/users/<int:user_id>/role")
def user_role(user_id: int):
    if user_id == g.user["id"]:
        return users(error="You can't change your own role. Ask another admin.")
    with db.get_conn() as conn:
        target = settings.user(conn, user_id)
        if target and target["github_login"]:
            return users(error=f"{target['name']}'s role comes from their GitHub teams: change the role lists instead.")
        problem = settings.set_role(conn, user_id, request.form.get("role", ""), _actor())
    if problem:
        return users(error=problem)
    flash("Role changed.", "ok")
    return redirect(url_for("admin.users"))


@bp.post("/settings/users/<int:user_id>/link")
def user_link(user_id: int):
    with db.get_conn() as conn:
        person = settings.user(conn, user_id)
        if not person:
            abort(404)
        if person["github_login"]:
            return users(error=f"{person['name']} signs in with GitHub, so has no password to set.")
        new = settings.new_setup_link(conn, user_id, _actor())
    return users(new_link={"name": person["name"], "role": person["role"], "url": _link(new.token)})


@bp.post("/settings/users/<int:user_id>/delete")
def user_delete(user_id: int):
    if user_id == g.user["id"]:
        return users(error="You can't remove your own account.")
    with db.get_conn() as conn:
        problem = settings.delete_user(conn, user_id, _actor())
    if problem:
        return users(error=problem)
    flash("Account removed. They're signed out on their next click.", "ok")
    return redirect(url_for("admin.users"))
