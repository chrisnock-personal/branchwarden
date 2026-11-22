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
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

import access
import actions
import apitokens
import analysis
import db
import ghlogin
import notify
import rules
import settings
import tls
from github import App, Connections, GitHubError

log = logging.getLogger("branchwarden.dashboard")

bp = Blueprint("admin", __name__)

OPEN_ENDPOINTS = {"admin.login", "admin.setup", "admin.welcome", "admin.github_login", "admin.github_callback"}
ALWAYS_OPEN = {"static", "healthz", "github_webhook", "prometheus"}   # the last checks its own token

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
    if request.endpoint in ALWAYS_OPEN or request.blueprint == "api" or request.path.startswith("/api/"):
        return None             # the API checks its own bearer tokens (see api.py)
    if request.endpoint is None:
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
    if request.blueprint == "admin" and not access.allowed(g.user["role"], "admin"):
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
    nxt = pending.get("next") or "/"
    return redirect(url_for("my_work") if nxt == "/" else nxt)


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


def _flash(r) -> None:
    flash(r.message, r.tone)


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
    with db.get_conn() as conn:
        _flash(actions.github_app(conn, request.form, _actor()))
    return redirect(url_for("admin.github") + "#app")


@bp.post("/settings/github/token")
def github_token():
    with db.get_conn() as conn:
        _flash(actions.github_token(conn, request.form, _actor()))
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


@bp.post("/settings/github/prs")
def github_prs():
    with db.get_conn() as conn:
        _flash(actions.pr_settings(conn, request.form, _actor()))
    return redirect(url_for("admin.github") + "#prs")


@bp.post("/settings/github/scan")
def github_scan():
    with db.get_conn() as conn:
        _flash(actions.scan_settings(conn, request.form, _actor()))
    return redirect(url_for("admin.github") + "#scan")


@bp.post("/settings/targets/new")
def target_new():
    with db.get_conn() as conn:
        _flash(actions.target_add(conn, request.form, _actor()))
    return redirect(url_for("admin.github") + "#targets")


@bp.post("/settings/targets/<int:target_id>")
def target_save(target_id: int):
    with db.get_conn() as conn:
        r = actions.target_update(conn, target_id, request.form, _actor())
    if r.status == 404:
        abort(404)
    _flash(r)
    return redirect(url_for("admin.github") + "#targets")


# ─── Settings: channels and reminders ───────────────────────────────────────


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
                           dm_token=_stored(meta, "SLACK_DM_TOKEN", unreadable),
                           get=lambda k: settings.get(values, k), units=rules.UNITS, days=analysis.DAYS,
                           wf_scopes=rules.WF_SCOPES)


@bp.post("/settings/channels/new")
def channel_new():
    with db.get_conn() as conn:
        r = actions.channel_add(conn, request.form, _actor())
    _flash(r)
    if not r.ok:
        return redirect(url_for("admin.notifications", open=f"new-{request.form.get('kind')}") + "#new")
    return redirect(url_for("admin.notifications", open=r.data["id"]) + f"#ch-{r.data['id']}")


@bp.post("/settings/channels/<int:cid>")
def channel_save(cid: int):
    with db.get_conn() as conn:
        r = actions.channel_update(conn, cid, request.form, _actor())
    if r.status == 404:
        abort(404)
    _flash(r)
    if request.form.get("delete") and r.ok:
        return redirect(url_for("admin.notifications") + "#channels")
    if request.form.get("toggle"):
        return redirect(url_for("admin.notifications") + f"#ch-{cid}")
    return redirect(url_for("admin.notifications", open=cid) + f"#ch-{cid}")


@bp.post("/settings/channels/<int:cid>/test")
def channel_test(cid: int):
    with db.get_conn() as conn:
        public = (settings.get(settings.load(conn), "PUBLIC_URL") or request.url_root).rstrip("/")
        r = actions.channel_test(conn, cid, _actor(), public)
    if r.status == 404:
        abort(404)
    _flash(r)
    return redirect(url_for("admin.notifications", open=cid) + f"#ch-{cid}")


@bp.post("/settings/workhours")
def workhours():
    with db.get_conn() as conn:
        _flash(actions.work_hours(conn, request.form, _actor()))
    return redirect(url_for("admin.notifications") + "#hours")


@bp.post("/settings/rules/<key>")
def rule_save(key: str):
    with db.get_conn() as conn:
        r = actions.rule_update(conn, key, request.form, _actor())
    if r.status == 404:
        abort(404)
    _flash(r)
    if not r.ok:
        return redirect(url_for("admin.notifications", open=key) + f"#rule-{key}")
    return redirect(url_for("admin.notifications") + f"#rule-{key}")


# ─── Settings: deliveries ───────────────────────────────────────────────────

@bp.get("/settings/deliveries")
def deliveries():
    status = request.args.get("status", "all")
    with db.get_conn() as conn:
        where = "" if status == "all" else "WHERE n.status = %s"
        rows = db.rows(conn, f"""SELECT n.*, c.name AS channel_name, c.kind AS channel_kind
                                   FROM notifications n JOIN channels c ON c.id = n.channel_id {where}
                                  ORDER BY n.created_at DESC, n.id DESC LIMIT 200""", (status,) if where else None)
        counts = {r["status"]: r["n"] for r in db.rows(
            conn, "SELECT status, COUNT(*) AS n FROM notifications GROUP BY 1")}
        chans = notify.channel_health(conn)
        scans = db.rows(conn, "SELECT * FROM scan_log ORDER BY id DESC LIMIT 30")
        hooks = db.rows(conn, "SELECT * FROM webhook_log ORDER BY id DESC LIMIT 30")
    for r in rows:
        p = r["payload"]
        r["subject"] = p.get("pr") or p.get("title") or ""
        r["heading"] = p.get("heading", "")
    return render_template("deliveries.html", rows=rows, status=status, counts=counts, channels=chans,
                           scans=scans, hooks=hooks)


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


@bp.post("/settings/people")
def people_save():
    with db.get_conn() as conn:
        r = actions.people_save(conn, request.form, _actor())
    _flash(r)
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
    with db.get_conn() as conn:
        _flash(actions.tls_install(conn, _pem_field("cert"), _pem_field("key"), _pem_field("chain"), _actor(),
                                   self_signed=bool(form.get("self_signed")), restore=bool(form.get("restore"))))
    return redirect(url_for("admin.tls_page"))


@bp.post("/settings/github-login")
def github_login_save():
    with db.get_conn() as conn:
        _flash(actions.github_login_settings(conn, request.form, _actor()))
    return redirect(url_for("admin.users") + "#github")


# ─── Settings: users ────────────────────────────────────────────────────────

def _link(token: str) -> str:
    return request.url_root.rstrip("/") + url_for("admin.welcome", token=token)


@bp.get("/settings/users")
def users(new_link: Optional[dict] = None, error: Optional[str] = None, adding: bool = False,
          form: Optional[dict] = None, new_token: Optional[dict] = None):
    with db.get_conn() as conn:
        people_rows = settings.users(conn)
        history = settings.history(conn)
        service_tokens = apitokens.service(conn)
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
                           roles=settings.ROLES, gh=gh, service_tokens=service_tokens, new_token=new_token)


@bp.post("/settings/service-tokens")
def service_token_create():
    form = request.form
    role = form.get("role", "")
    if role not in settings.ROLES:
        return users(error="Pick a role for the service token.")
    with db.get_conn() as conn:
        row, token = apitokens.create(conn, form.get("name", ""), form.get("scope", "write"), _actor(), role=role,
                                      expires_days=form.get("expires_days", type=int))
    if not row:
        return users(error=token)
    return users(new_token={"name": row["name"], "token": token})


@bp.post("/settings/service-tokens/<int:token_id>/revoke")
def service_token_revoke(token_id: int):
    with db.get_conn() as conn:
        done = apitokens.revoke(conn, token_id, _actor())
    flash("Service token revoked." if done else "No such service token.", "ok" if done else "bad")
    return redirect(url_for("admin.users") + "#service-tokens")


@bp.post("/settings/users/new")
def user_new():
    form = request.form
    with db.get_conn() as conn:
        r = actions.user_add(conn, form.get("name", ""), form.get("email", ""), form.get("role", ""), _actor())
    if not r.ok:
        return users(error=r.message, adding=True, form=form)
    return users(new_link={"name": form.get("name", "").strip(), "role": form.get("role"), "url": _link(r.data["token"])})


@bp.post("/settings/users/<int:user_id>/role")
def user_role(user_id: int):
    with db.get_conn() as conn:
        r = actions.user_role(conn, g.user["id"], user_id, request.form.get("role", ""), _actor())
    if not r.ok:
        return users(error=r.message)
    flash(r.message, "ok")
    return redirect(url_for("admin.users"))


@bp.post("/settings/users/<int:user_id>/link")
def user_link(user_id: int):
    with db.get_conn() as conn:
        r = actions.user_link(conn, user_id, _actor())
    if r.status == 404:
        abort(404)
    if not r.ok:
        return users(error=r.message)
    return users(new_link={"name": r.data["name"], "role": r.data["role"], "url": _link(r.data["token"])})


@bp.post("/settings/users/<int:user_id>/delete")
def user_delete(user_id: int):
    with db.get_conn() as conn:
        r = actions.user_delete(conn, g.user["id"], user_id, _actor())
    if not r.ok:
        return users(error=r.message)
    flash(r.message, "ok")
    return redirect(url_for("admin.users"))
