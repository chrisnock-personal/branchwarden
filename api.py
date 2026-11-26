"""
api.py
~~~~~~
Branchwarden's JSON API, under /api/v1: everything the dashboard shows or does,
for scripts, CI and AI agents (and the branchwarden CLI).

- Auth: a bearer token (Authorization: Bearer bwt_…), personal or service;
  never cookies, so there's nothing for a cross-site form to ride on. HTTPS only.
- Access: the token's role and scope, through access.allowed(): the same rule
  as the dashboard. Changes go through actions.py: the same checks and history
  lines as the dashboard, with the token named in the history.
- Shape: JSON in and out; errors are {"error": {"status", "code", "message"}};
  lists are {"items": [...], "next_cursor": ...} (pass ?cursor= to go on).
- Each endpoint is declared once, with @endpoint, which also builds the
  OpenAPI document served at /api/v1/openapi.json.
"""

from __future__ import annotations

import base64
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from functools import wraps
from typing import Any, Callable, Optional

from flask import Blueprint, Response, g, jsonify, request
from werkzeug.datastructures import MultiDict
from werkzeug.exceptions import HTTPException

import access
import actions
import analysis
import apitokens
import data
import db
import metrics
import mvp
import notify
import repos as repos_mod
import rules
import settings
import tls
import workflows
from version import VERSION

bp = Blueprint("api", __name__, url_prefix="/api/v1")

RATE_LIMIT = int(os.getenv("BRANCHWARDEN_API_RATE", "300"))      # requests per token per minute
FAILED_LIMIT = 30                                                 # bad tokens per client address per minute
_calls: dict[int, deque] = defaultdict(deque)
_failed: dict[str, deque] = defaultdict(deque)


def failures_exceeded(addr: str, add: bool = False) -> bool:
    """Too many bad tokens from one address in the last minute? (add=True records one more.)
    Tokens can't be guessed, but each try costs a lookup: this keeps that cheap."""
    now = time.monotonic()
    q = _failed[addr or "?"]
    while q and now - q[0] > 60:
        q.popleft()
    if add:
        q.append(now)
    if not q:
        _failed.pop(addr or "?", None)
    return len(q) > FAILED_LIMIT


# ─── Declaring endpoints ────────────────────────────────────────────────────

@dataclass
class Endpoint:
    method: str
    path: str                     # OpenAPI style: /pulls/{pr_id}
    need: str                     # read | act | admin
    summary: str
    tag: str
    params: list = field(default_factory=list)     # [(name, in, type, description)]
    body: Optional[dict] = None                     # {field: (type, description)}
    paged: bool = False
    status: int = 200


ENDPOINTS: list[Endpoint] = []


def endpoint(method: str, path: str, need: str, summary: str, tag: str, *, params=(), body=None, paged=False,
             status=200):
    """Register a route, enforce its access, and describe it for OpenAPI."""
    spec = Endpoint(method, path, need, summary, tag, list(params), body, paged, status)
    ENDPOINTS.append(spec)
    rule = path
    for name, where, typ, _ in spec.params:
        if where == "path":
            rule = rule.replace("{" + name + "}", f"<{'int:' if typ == 'integer' else ''}{name}>")

    def wrap(fn: Callable):
        @wraps(fn)
        def guarded(**kw):
            if not access.allowed(g.api["role"], need, g.api["scope"]):
                why = "This token is read-only." if g.api["scope"] == "read" and need != "read" else \
                    f"This needs the {access.NEEDS[need]} role or higher."
            # (a read-only token can't make or revoke tokens either: "self" needs a write token)
                return error(403, "forbidden", why)
            return fn(**kw)
        bp.add_url_rule(rule, endpoint=f"{method.lower()}_{fn.__name__}", view_func=guarded, methods=[method])
        return guarded
    return wrap


# ─── Plumbing ───────────────────────────────────────────────────────────────

def jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        items = [jsonable(x) for x in v]
        return sorted(items) if isinstance(v, (set, frozenset)) else items
    return v


def ok(body: Any, status: int = 200) -> Response:
    resp = jsonify(jsonable(body))
    resp.status_code = status
    return resp


CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
         409: "conflict", 413: "too_large", 415: "unsupported_media_type", 429: "rate_limited", 500: "server_error",
         502: "github_error", 503: "unavailable"}


def error(status: int, code: Optional[str] = None, message: str = "") -> Response:
    resp = jsonify({"error": {"status": status, "code": code or CODES.get(status, "error"), "message": message}})
    resp.status_code = status
    return resp


def result(r: actions.Result) -> Response:
    if not r.ok:
        return error(r.status, None, r.message)
    return ok({"ok": True, "message": r.message, **r.data}, r.status)


def body() -> dict:
    if not request.content_length:
        return {}
    if not request.is_json:        # (a form post's body is in request.form, not request.data: check the length)
        raise _Bad(415, "Send JSON (Content-Type: application/json).")
    got = request.get_json(silent=True)
    if not isinstance(got, dict):
        raise _Bad(400, "The body must be a JSON object.")
    return got


def flag(b: dict, key: str, default: Optional[bool] = False) -> Optional[bool]:
    """A JSON true or false. Anything else is refused rather than guessed: in Python, "false" is true."""
    v = b.get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    raise _Bad(400, f"{key} must be true or false.")


def flat(d: dict, keep_lists=("days",)) -> dict:
    """JSON for actions.py: lists (repos, teams, role lists) become comma-separated, as typed in the dashboard."""
    return {k: (",".join(str(x) for x in v) if isinstance(v, list) and k not in keep_lists else v)
            for k, v in d.items() if v is not None}


class _Bad(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


def page(items: list, default: int = 50) -> Response:
    try:
        limit = max(1, min(200, int(request.args.get("limit", default))))
        start = int(base64.urlsafe_b64decode(request.args["cursor"].encode()).decode()) if request.args.get("cursor") else 0
    except (ValueError, TypeError, UnicodeDecodeError):
        return error(400, None, "limit is 1 to 200, and cursor is a value from next_cursor.")
    chunk = items[start:start + limit]
    nxt = start + limit
    return ok({"items": chunk, "next_cursor": base64.urlsafe_b64encode(str(nxt).encode()).decode()
               if nxt < len(items) else None, "total": len(items)})


def arg_int(name: str, choices, default):
    v = request.args.get(name, type=int)
    if v is None:
        return default
    if v not in choices:
        raise _Bad(400, f"{name} is one of {', '.join(str(c) for c in choices)}.")
    return v


@bp.errorhandler(_Bad)
def _bad(exc: _Bad):
    return error(exc.status, None, exc.message)


@bp.errorhandler(HTTPException)
def _http(exc: HTTPException):
    return error(exc.code or 500, None, exc.description if exc.code != 404 else "No such endpoint or item.")


@bp.before_request
def authenticate():
    insecure_ok = os.getenv("BRANCHWARDEN_INSECURE_HTTP", "").lower() in ("1", "true", "yes")
    if not request.is_secure and not insecure_ok:
        return error(403, "https_required", "Use HTTPS.")
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if not token:
        resp = error(401, None, "Send a token: Authorization: Bearer bwt_… (make one under your name › API tokens).")
        resp.headers["WWW-Authenticate"] = 'Bearer realm="branchwarden"'
        return resp
    if failures_exceeded(request.remote_addr):
        resp = error(429, None, "Too many bad tokens from this address: wait a minute.")
        resp.headers["Retry-After"] = "60"
        return resp
    why: list = []
    with db.get_conn() as conn:
        who = apitokens.authenticate(conn, token, why=why)
    if not who:
        failures_exceeded(request.remote_addr, add=True)
        resp = error(401, None, why[0] if why else
                     "That token isn't valid: unknown, revoked or expired, or its owner has gone.")
        resp.headers["WWW-Authenticate"] = 'Bearer realm="branchwarden", error="invalid_token"'
        return resp
    now = time.monotonic()
    q = _calls[who["token_id"]]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        resp = error(429, None, f"At most {RATE_LIMIT} requests a minute per token.")
        resp.headers["Retry-After"] = str(max(1, int(60 - (now - q[0]))))
        return resp
    q.append(now)
    g.api = who
    return None


@bp.after_request
def _headers(resp: Response):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Branchwarden-Version"] = VERSION
    return resp


def actor() -> str:
    return g.api["actor"]


# ─── Shapes ─────────────────────────────────────────────────────────────────

def pr_out(p: dict) -> dict:
    return {
        "id": p["id"], "repo": p["full_name"], "number": p["number"], "title": p["title"], "url": p["url"],
        "author": p.get("author"), "state": p["state_key"], "state_label": p["state_label"], "draft": p["draft"],
        "filters": sorted(p["cats"]), "ci": p.get("ci_state"), "waiting_on": p["waiting_on"],
        "waiting_since": p["since"], "waiting_seconds": int(p["waiting_seconds"]),
        "requested": {"users": p["requested_users"], "teams": p["requested_teams"]},
        "reviews": p.get("reviews") or [], "head": p.get("head_ref"), "base": p.get("base_ref"),
        "additions": p.get("additions"), "deletions": p.get("deletions"), "changed_files": p.get("changed_files"),
        "labels": p.get("labels") or [], "big": p.get("big"), "stalled": p.get("stalled"),
        "last_activity": p.get("active_at"), "tickets": p.get("tickets") or [],
        "created_at": p.get("created_at"), "updated_at": p.get("updated_at"),
    }


def branch_out(b: dict) -> dict:
    return {
        "id": b["id"], "repo": b["full_name"], "name": b["name"], "status": b["status"],
        "status_label": b["status_label"], "last_commit_at": b.get("last_commit_at"),
        "last_committer": b.get("last_committer"), "age_days": b.get("age_days"), "ahead": b.get("ahead"),
        "behind": b.get("behind"), "url": b.get("url"), "tickets": b.get("tickets") or [],
        "pr": {"number": b["pr_number"], "state": b["pr_state"], "title": b["pr_title"],
               "merged_at": b["pr_merged_at"], "closed_at": b["pr_closed_at"], "merged_by": b["pr_merged_by"]}
        if b.get("pr_number") else None,
    }


# ─── Who am I, and the overview ─────────────────────────────────────────────

@endpoint("GET", "/me", "read", "The token and who it acts as", "account")
def me():
    w = g.api
    return ok({"name": w["user_name"] or w["name"], "role": w["role"], "scope": w["scope"], "service": w["service"],
               "github_login": w["github_login"], "token": {"id": w["token_id"], "prefix": w["prefix"],
                                                            "name": w["name"]},
               "can": {need: access.allowed(w["role"], need, w["scope"]) for need in access.NEEDS},
               "version": VERSION})


@endpoint("GET", "/overview", "read", "Headline numbers, as on the overview", "overview")
def overview():
    now = datetime.now(timezone.utc)
    with db.get_conn() as conn:
        values = settings.load(conn)
        prs = data.open_prs(conn, now=now)
        branches = data.classified_branches(conn, values, now=now)
        last = db.one(conn, "SELECT * FROM scan_log WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1")
        overdue = data.worker_overdue(conn, now)
        repo_count = db.one(conn, "SELECT COUNT(*) AS n FROM repos WHERE included")["n"]
    cats = lambda k: sum(1 for p in prs if k in p["cats"] and not p["draft"])  # noqa: E731
    return ok({
        "open_prs": len(prs), "drafts": sum(1 for p in prs if p["draft"]), "needs_review": cats("review"),
        "changes_requested": sum(1 for p in prs if p["state_key"] == "changes"),
        "approved_not_merged": sum(1 for p in prs if p["state_key"] == "approved"),
        "ci_failing": cats("ci"), "big": cats("big"), "stalled": cats("stalled"),
        "stale_branches": sum(1 for b in branches if b["status"] == "stale"),
        "left_after_pr": sum(1 for b in branches if b["status"] in repos_mod.LEFT),
        "repos": repo_count, "demo": settings.flag(values, "DEMO_MODE"),
        "last_scan": {"finished_at": last["finished_at"], "trigger": last["trigger"], "error": last["error"],
                      "repos": last["repos"], "requests": last.get("requests")} if last else None,
        "scans_stopped": overdue is not None,
    })


# ─── Pull requests ──────────────────────────────────────────────────────────

PR_FILTERS = [k for k, _ in analysis.FILTERS]


@endpoint("GET", "/pulls", "read", "Open pull requests, longest waiting first", "pulls", paged=True, params=[
    ("filter", "query", "string", "One of: " + ", ".join(PR_FILTERS)),
    ("repo", "query", "string", "owner/name, or a pattern such as api-*"),
    ("author", "query", "string", "GitHub login"),
    ("reviewer", "query", "string", "A login or org/team asked to review"),
    ("q", "query", "string", "Search titles, repos, authors and branch names"),
    ("limit", "query", "integer", "1 to 200 (50)"), ("cursor", "query", "string", "From next_cursor")])
def pulls_list():
    f = request.args.get("filter", "all")
    if f not in PR_FILTERS:
        return error(400, None, "filter is one of: " + ", ".join(PR_FILTERS))
    with db.get_conn() as conn:
        prs = data.open_prs(conn)
    repo, author = request.args.get("repo", "").lower(), request.args.get("author", "").lower().lstrip("@")
    reviewer, q = request.args.get("reviewer", "").lower().lstrip("@"), request.args.get("q", "").lower()
    if f != "all":
        prs = [p for p in prs if f in p["cats"]]
    if repo:
        prs = [p for p in prs if analysis.matches(p["full_name"], [repo])]
    if author:
        prs = [p for p in prs if (p.get("author") or "") == author]
    if reviewer:
        prs = [p for p in prs if reviewer in p["requested_users"] or reviewer in p["requested_teams"]]
    if q:
        prs = [p for p in prs if q in p["title"].lower() or q in p["full_name"] or q in (p.get("author") or "")
               or q in (p.get("head_ref") or "").lower()]
    prs.sort(key=lambda p: -p["waiting_seconds"])
    return page([pr_out(p) for p in prs])


@endpoint("GET", "/pulls/{pr_id}", "read", "One open pull request", "pulls",
          params=[("pr_id", "path", "integer", "Branchwarden's id for the PR (the id field)")])
def pulls_get(pr_id: int):
    with db.get_conn() as conn:
        found = data.open_prs(conn, pr_id=pr_id)
        snooze = db.one(conn, "SELECT until, by_name FROM snoozes WHERE pr_id = %s AND until > NOW()", (pr_id,))
    if not found:
        return error(404, None, "No open pull request with that id.")
    return ok({**pr_out(found[0]), "snoozed": snooze})


def _who(entries) -> list[str]:
    if not isinstance(entries, list) or not all(isinstance(x, str) and x.strip() for x in entries):
        raise _Bad(400, "add and remove are lists of GitHub logins and org/team slugs.")
    return [("t:" if "/" in x else "u:") + x.strip().lstrip("@").lower() for x in entries]


@endpoint("POST", "/pulls/{pr_id}/reviewers", "act", "Request or withdraw reviewers on GitHub", "pulls",
          params=[("pr_id", "path", "integer", "Branchwarden's id for the PR")],
          body={"add": ("array", "Logins and org/team slugs to ask"), "remove": ("array", "Ones to withdraw"),
                "notify": ("boolean", "Also tell the PR's channels"),
                "dry_run": ("boolean", "Only say what would change; nothing is sent to GitHub")})
def pulls_reviewers(pr_id: int):
    b = body()
    with db.get_conn() as conn:
        r = actions.reviewers(conn, settings.load(conn), pr_id, add=_who(b.get("add", [])),
                              remove=_who(b.get("remove", [])), actor=actor(), tell=flag(b, "notify"),
                              dry_run=flag(b, "dry_run"))
    return result(r)


@endpoint("POST", "/pulls/{pr_id}/snooze", "act", "Snooze a PR's reminders for 1, 3 or 7 days", "pulls",
          params=[("pr_id", "path", "integer", "Branchwarden's id for the PR")], body={"days": ("integer", "1, 3 or 7")})
def pulls_snooze(pr_id: int):
    days = body().get("days")
    if days not in actions.SNOOZE_DAYS:
        return error(400, None, "days is 1, 3 or 7.")
    with db.get_conn() as conn:
        return result(actions.snooze(conn, pr_id, days, actor()))


@endpoint("DELETE", "/pulls/{pr_id}/snooze", "act", "Turn a PR's reminders back on", "pulls",
          params=[("pr_id", "path", "integer", "Branchwarden's id for the PR")])
def pulls_unsnooze(pr_id: int):
    with db.get_conn() as conn:
        return result(actions.snooze(conn, pr_id, None, actor()))


# ─── Branches ───────────────────────────────────────────────────────────────

BRANCH_GROUPS = {"stale": ("stale",), "left": repos_mod.LEFT, "active": ("active",), "open_pr": ("open_pr",),
                 "all": tuple(analysis.BRANCH_STATUS)}


@endpoint("GET", "/branches", "read", "Flagged branches: stale, left after a PR, and the rest", "branches",
          paged=True, params=[("status", "query", "string", "stale, left, active, open_pr or all (stale and left)"),
                              ("repo", "query", "string", "owner/name, or a pattern"),
                              ("q", "query", "string", "Search branch names and committers"),
                              ("limit", "query", "integer", "1 to 200 (50)"), ("cursor", "query", "string", "From next_cursor")])
def branches_list():
    status = request.args.get("status", "")
    if status and status not in BRANCH_GROUPS:
        return error(400, None, "status is one of: " + ", ".join(BRANCH_GROUPS))
    with db.get_conn() as conn:
        found = data.classified_branches(conn, settings.load(conn))
    wanted = BRANCH_GROUPS[status] if status else ("stale",) + repos_mod.LEFT
    found = [b for b in found if b["status"] in wanted]
    repo, q = request.args.get("repo", "").lower(), request.args.get("q", "").lower()
    if repo:
        found = [b for b in found if analysis.matches(b["full_name"], [repo])]
    if q:
        found = [b for b in found if q in b["name"].lower() or q in (b.get("last_committer") or "").lower()]
    found.sort(key=lambda b: -(b.get("age_days") or 0))
    return page([branch_out(b) for b in found])


@endpoint("GET", "/branches/ignored", "read", "Branches someone chose not to flag", "branches")
def branches_ignored():
    with db.get_conn() as conn:
        rows = db.rows(conn, """SELECT r.full_name AS repo, i.name, i.ignored_by, i.ignored_at FROM ignored_branches i
                                 JOIN repos r ON r.id = i.repo_id ORDER BY r.full_name, i.name""")
    return ok({"items": rows})


@endpoint("POST", "/branches/{branch_id}/ignore", "act", "Stop flagging a branch", "branches",
          params=[("branch_id", "path", "integer", "The branch's id field")])
def branches_ignore(branch_id: int):
    with db.get_conn() as conn:
        return result(actions.ignore_branch(conn, branch_id, actor()))


@endpoint("DELETE", "/branches/ignored", "act", "Flag an ignored branch again", "branches",
          params=[("repo", "query", "string", "owner/name"), ("branch", "query", "string", "The branch name")])
def branches_unignore():
    repo, branch = request.args.get("repo", "").lower(), request.args.get("branch", "")
    with db.get_conn() as conn:
        r = db.one(conn, "SELECT id FROM repos WHERE full_name = %s", (repo,))
        if not r or not branch:
            return error(404, None, "Give repo (owner/name) and branch of an ignored branch.")
        return result(actions.unignore_branch(conn, r["id"], branch, actor()))


# ─── Repositories, workflows, my work, metrics, MVP ─────────────────────────

@endpoint("GET", "/repos", "read", "Every repository with its counts and health flags", "repos")
def repos_list():
    with db.get_conn() as conn:
        rows = repos_mod.overview(conn, settings.load(conn))
    return ok({"items": rows})


@endpoint("GET", "/repos/{owner}/{name}", "read", "One repository: PRs, branches, health, reviewers, routing",
          "repos", params=[("owner", "path", "string", "Owner"), ("name", "path", "string", "Name"),
                           ("days", "query", "integer", "Metrics period: 30, 90 or 180 (90)")])
def repos_get(owner: str, name: str):
    days = arg_int("days", dict(metrics.PERIODS), 90)
    with db.get_conn() as conn:
        d = repos_mod.detail(conn, settings.load(conn), f"{owner}/{name}", days)
    if not d:
        return error(404, None, "No such repository (or it's skipped).")
    r = d["repo"]
    return ok({"repo": {"full_name": r["full_name"], "url": r["url"], "default_branch": r["default_branch"],
                        "private": r["private"], "last_scan_at": r["last_scan_at"], "last_error": r["last_error"]},
               "health": d["flags"], "pulls": [pr_out(p) for p in d["prs"]],
               "stale_branches": [branch_out(b) for b in d["stale"]], "left_after_pr": [branch_out(b) for b in d["left"]],
               "metrics": {"days": days, "tiles": d["metrics"]["tiles"]}, "reviewers": d["reviewers"],
               "codeowners": d["codeowners"], "routing": d["routing"]})


@endpoint("GET", "/workflows", "read", "GitHub Actions workflows: due times, runs and results per day", "workflows",
          params=[("days", "query", "integer", "7, 14 or 30 (14)"), ("repo", "query", "string", "owner/name")])
def workflows_list():
    days = arg_int("days", dict(workflows.PERIODS), 14)
    with db.get_conn() as conn:
        w = workflows.load(conn, settings.load(conn), days, request.args.get("repo", "").lower())
    items = []
    for r in w["rows"]:
        items.append({
            "id": r["id"], "repo": r["full_name"], "name": r["name"], "path": r["path"], "state": r["state"],
            "url": r["url"], "triggers": r["triggers"], "schedules": r["crons"], "runs": r["runs"],
            "passed": r["passed"], "finished": r["finished"], "pass_rate": r["pass_rate"],
            "failing_since": r["failing_since"], "missed": r["missed"], "next_due": r["next_due"],
            "usual_delay": r["usual_delay"], "problems": r["problems"],
            "days": [{"day": c["day"], "counts": {s["state"]: s["grow"] for s in c["segments"]} if not r["small"]
                      else _count(c["segments"])} for c in r["cells"]],
            "recent_runs": [{"id": x["id"], "event": x["event"], "branch": x["head_branch"], "outcome": x["outcome"],
                             "created_at": x["created_at"], "took": x["took"], "actor": x["actor"], "url": x["url"]}
                            for x in r["recent"]]})
    return ok({"days": days, "tiles": w["tiles"], "items": items,
               "no_access": [{"repo": x["full_name"], "error": x["workflows_error"]} for x in w["no_access"]]})


def _count(segments) -> dict:
    out: dict = {}
    for s in segments:
        out[s["state"]] = out.get(s["state"], 0) + 1
    return out


@endpoint("GET", "/my-work", "read", "What's waiting on the token's owner: reviews, their PRs, branches, snoozes",
          "me")
def my_work():
    login = (g.api.get("github_login") or "").lower()
    if not login:
        return error(409, "no_github_login", "This token's owner hasn't given a GitHub login (My work, in the dashboard)."
                     if not g.api["service"] else "A service token has no My work.")
    with db.get_conn() as conn:
        w = data.my_work(conn, login, settings.load(conn))
    with db.get_conn() as conn:
        morning = actions.morning_status(conn, login)
    return ok({"login": login, "morning_message": morning,
               "reviews_waiting": [pr_out(p) | {"asked_at": p["asked"], "you_reviewed_before": p["mine_reviewed"]}
                                   for p in w["to_review"]],
               "my_pulls": [pr_out(p) | {"next_step": p["next_step"]} for p in w["mine"]],
               "my_branches": [branch_out(b) for b in w["branches"]],
               "snoozed": [{"pr": p["id"], "until": p["snooze"]["until"], "by": p["snooze"]["by_name"]} for p in w["snoozed"]]})


@endpoint("PUT", "/my-work/morning", "self", "Turn the token owner's morning message on or off, and say where it goes",
          "me", body={"enabled": ("boolean", "On or off (left out: as it is)"),
                      "to": ("string", "slack (a DM from the bot) or teams (your own Workflows webhook)"),
                      "teams_url": ("string", "Your Teams Workflows webhook URL (stored encrypted, never shown)"),
                      "remove_teams": ("boolean", "Forget the stored Teams webhook URL")})
def my_morning():
    login = (g.api.get("github_login") or "").lower()
    if g.api["service"] or not login:
        return error(409, "no_github_login", "This needs a person's token, with their GitHub login set.")
    b = body()
    with db.get_conn() as conn:
        return result(actions.set_morning(conn, login, flag(b, "enabled", None), actor(),
                                          to=b.get("to") or None, teams_url=b.get("teams_url") or None,
                                          remove_teams=flag(b, "remove_teams"), user_id=g.api.get("user_id")))


@endpoint("POST", "/my-work/morning/test", "self", "Send the token owner's morning message now", "me")
def my_morning_test():
    login = (g.api.get("github_login") or "").lower()
    if g.api["service"] or not login:
        return error(409, "no_github_login", "This needs a person's token, with their GitHub login set.")
    with db.get_conn() as conn:
        return result(actions.morning_now(conn, settings.load(conn), login, user_id=g.api.get("user_id")))


@endpoint("GET", "/metrics", "read", "Review and merge times, merges per week, by repo and by size", "metrics",
          params=[("days", "query", "integer", "30, 90 or 180 (30)"), ("repo", "query", "string", "owner/name")])
def metrics_get():
    days = arg_int("days", dict(metrics.PERIODS), 30)
    with db.get_conn() as conn:
        m = metrics.compute(conn, days, request.args.get("repo", "").lower())
    series = lambda bars, key: [{"label": b["label"], key: b["value"]} for b in bars]  # noqa: E731
    return ok({"days": days, "tiles": m["tiles"], "by_repo": m["repos"], "by_size": m["by_size"],
               "reviewers_waiting": m["reviewers"], "assignments": m["assignments"],
               "merged_per_week": series(m["merged_bars"], "merged"),
               "first_review_hours_per_week": series(m["first_bars"], "median_hours"),
               "needing_review_per_day": series(m["review_bars"], "count"),
               "stale_branches_per_day": series(m["stale_bars"], "count")})


@endpoint("GET", "/mvp", "read", "The week's MVPs: reviews, merges, branch prunes, PRs raised", "metrics",
          params=[("period", "query", "string", "this (the last 7 days) or prev (the 7 before)")])
def mvp_get():
    period = request.args.get("period", "this")
    if period not in ("this", "prev"):
        return error(400, None, "period is this or prev.")
    with db.get_conn() as conn:
        r = mvp.compute(conn, period)
    return ok({"start": r["start"], "end": r["end"], "totals": r["totals"],
               "boards": [{"key": b["key"], "title": b["title"],
                           "ranking": ([{"login": b["top"][0], "count": b["top"][1]}] if b["top"] else [])
                           + [{"login": x["login"], "count": x["v"]} for x in b["rest"]]} for b in r["boards"]]})


@endpoint("POST", "/scan", "act", "Start a full scan now", "scans", status=202)
def scan():
    with db.get_conn() as conn:
        return result(actions.scan(conn, actor()))


# ─── Tokens ─────────────────────────────────────────────────────────────────

TOKEN_BODY = {"name": ("string", "What it's for"), "scope": ("string", "read or write (write)"),
              "expires_days": ("integer", "Days until it stops working, 1 to 3650; leave out for never")}


@endpoint("GET", "/tokens", "read", "Your personal API tokens (not the tokens themselves)", "account")
def tokens_list():
    if g.api["service"]:
        return error(409, None, "A service token has no personal tokens.")
    with db.get_conn() as conn:
        return ok({"items": apitokens.mine(conn, g.api["user_id"])})


@endpoint("POST", "/tokens", "self", "Make a personal API token (shown once)", "account", body=TOKEN_BODY, status=201)
def tokens_create():
    if g.api["service"]:
        return error(409, None, "A service token can't make personal tokens.")
    b = body()
    with db.get_conn() as conn:
        row, token = apitokens.create(conn, b.get("name", ""), b.get("scope", "write"), actor(),
                                      user_id=g.api["user_id"], expires_days=b.get("expires_days"))
    if not row:
        return error(400, None, token)
    return ok({**row, "token": token}, 201)


@endpoint("DELETE", "/tokens/{token_id}", "self", "Revoke one of your personal tokens", "account",
          params=[("token_id", "path", "integer", "The token's id")])
def tokens_revoke(token_id: int):
    with db.get_conn() as conn:
        done = apitokens.revoke(conn, token_id, actor(), user_id=g.api["user_id"]) if not g.api["service"] else False
    return ok({"ok": True, "message": "Revoked."}) if done else error(404, None, "No such token of yours.")


# ─── Admin ──────────────────────────────────────────────────────────────────

SECTIONS = {"github-app": actions.github_app, "github-token": actions.github_token,
            "scanning": actions.scan_settings, "pull-requests": actions.pr_settings,
            "work-hours": actions.work_hours, "github-sign-in": actions.github_login_settings}


@endpoint("GET", "/admin/settings", "admin", "Every setting: values, and which secrets are set (never their values)",
          "admin")
def admin_settings():
    with db.get_conn() as conn:
        values = settings.load(conn)
        meta = settings.secret_meta(conn)
    out = {}
    for key, (label, _) in settings.DEFAULTS.items():
        if settings.is_secret(key):
            out[key] = {"label": label, "secret": True, "set": key in meta,
                        "updated_at": meta[key]["updated_at"] if key in meta else None}
        else:
            out[key] = {"label": label, "value": settings.get(values, key)}
    return ok({"settings": out, "sections": sorted(SECTIONS)})


@endpoint("PATCH", "/admin/settings/{section}", "admin",
          "Change one section of settings, with the same fields and checks as its dashboard form", "admin",
          params=[("section", "path", "string", ", ".join(sorted(SECTIONS)))],
          body={"<FIELD>": ("string", "Setting keys as on GET /admin/settings, e.g. STALE_DAYS; secrets are write-only")})
def admin_settings_patch(section: str):
    fn = SECTIONS.get(section)
    if not fn:
        return error(404, None, "Sections: " + ", ".join(sorted(SECTIONS)))
    b = body()
    with db.get_conn() as conn:
        return result(fn(conn, flat(b), actor()))


@endpoint("GET", "/admin/targets", "admin", "Accounts and organisations to scan", "admin")
def admin_targets():
    with db.get_conn() as conn:
        return ok({"items": db.rows(conn, "SELECT * FROM targets ORDER BY login")})


@endpoint("POST", "/admin/targets", "admin", "Add an account or organisation to scan", "admin", status=201,
          body={"login": ("string", "GitHub login"), "kind": ("string", "org or user"),
                "via": ("string", "auto, app or token")})
def admin_targets_add():
    with db.get_conn() as conn:
        return result(actions.target_add(conn, flat(body()), actor()))


@endpoint("PATCH", "/admin/targets/{target_id}", "admin", "Switch scanning on or off, or change the connection",
          "admin", params=[("target_id", "path", "integer", "Its id")],
          body={"enabled": ("boolean", "Scan it"), "via": ("string", "auto, app or token")})
def admin_targets_patch(target_id: int):
    with db.get_conn() as conn:
        return result(actions.target_update(conn, target_id, flat(body()), actor()))


@endpoint("DELETE", "/admin/targets/{target_id}", "admin", "Stop scanning it, and remove its repos, PRs and branches",
          "admin", params=[("target_id", "path", "integer", "Its id")])
def admin_targets_delete(target_id: int):
    with db.get_conn() as conn:
        return result(actions.target_update(conn, target_id, MultiDict({"delete": "1"}), actor()))


def _channel_out(conn, c: dict, meta: dict) -> dict:
    return {"id": c["id"], "kind": c["kind"], "name": c["name"], "enabled": c["enabled"],
            "repos": settings.split_list(c["repos"]), "teams": settings.split_list(c["teams"]),
            "slack_channel": c["slack_channel"] or None, "webhook_set": settings.channel_url_key(c["id"]) in meta,
            "bot_token_set": settings.channel_token_key(c["id"]) in meta}


@endpoint("GET", "/admin/channels", "admin", "Teams and Slack channels (webhook URLs and tokens are never shown)",
          "admin")
def admin_channels():
    with db.get_conn() as conn:
        meta = settings.secret_meta(conn)
        health = {c["id"]: c for c in notify.channel_health(conn)}
        return ok({"items": [_channel_out(conn, c, meta) | {"status": health[c["id"]]["status"],
                                                            "detail": health[c["id"]]["detail"]}
                             for c in db.rows(conn, "SELECT * FROM channels ORDER BY id")]})


CHANNEL_BODY = {"kind": ("string", "teams or slack (when adding)"), "name": ("string", "Its name"),
                "url": ("string", "The webhook URL (write-only)"), "bot_token": ("string", "Slack bot token (write-only)"),
                "slack_channel": ("string", "Slack channel ID, with a bot token"),
                "repos": ("string", "Repo patterns, comma-separated"), "teams": ("string", "org/team slugs, comma-separated"),
                "enabled": ("boolean", "Send to it")}


@endpoint("POST", "/admin/channels", "admin", "Add a Teams or Slack channel", "admin", body=CHANNEL_BODY, status=201)
def admin_channels_add():
    with db.get_conn() as conn:
        return result(actions.channel_add(conn, flat(body()), actor()))


@endpoint("PATCH", "/admin/channels/{channel_id}", "admin", "Change a channel", "admin", body=CHANNEL_BODY,
          params=[("channel_id", "path", "integer", "Its id")])
def admin_channels_patch(channel_id: int):
    with db.get_conn() as conn:
        return result(actions.channel_update(conn, channel_id, flat(body()), actor()))


@endpoint("DELETE", "/admin/channels/{channel_id}", "admin", "Remove a channel and its queued messages", "admin",
          params=[("channel_id", "path", "integer", "Its id")])
def admin_channels_delete(channel_id: int):
    with db.get_conn() as conn:
        return result(actions.channel_update(conn, channel_id, MultiDict({"delete": "1"}), actor()))


@endpoint("POST", "/admin/channels/{channel_id}/test", "admin", "Send a test message to a channel", "admin",
          params=[("channel_id", "path", "integer", "Its id")])
def admin_channels_test(channel_id: int):
    with db.get_conn() as conn:
        public = (settings.get(settings.load(conn), "PUBLIC_URL") or request.url_root).rstrip("/")
        return result(actions.channel_test(conn, channel_id, actor(), public))


@endpoint("GET", "/admin/rules", "admin", "Event, reminder, digest and post rules", "admin")
def admin_rules():
    with db.get_conn() as conn:
        return ok({"items": [{"key": r["key"], "kind": r["kind"], "name": r["name"], "enabled": r["enabled"],
                              "params": r["params"], "description": rules.describe(r, {})}
                             for r in rules.load(conn).values()]})


@endpoint("PATCH", "/admin/rules/{key}", "admin", "Switch a rule on or off, or change its settings", "admin",
          params=[("key", "path", "string", "The rule's key")],
          body={"enabled": ("boolean", "On or off"), "<PARAM>": ("string", "Its params, as in GET /admin/rules")})
def admin_rules_patch(key: str):
    with db.get_conn() as conn:
        return result(actions.rule_update(conn, key, body(), actor()))


@endpoint("POST", "/admin/rules/{key}/send", "admin", "Send the weekly digest or MVP post now", "admin",
          params=[("key", "path", "string", "digest or mvp")])
def admin_rules_send(key: str):
    if key not in ("digest", "mvp"):
        return error(404, None, "Only digest and mvp can be sent now.")
    with db.get_conn() as conn:
        return result(actions.rule_update(conn, key, {"send_now": True}, actor()))


@endpoint("GET", "/admin/people", "admin", "GitHub logins matched to Teams and Slack identities", "admin")
def admin_people():
    with db.get_conn() as conn:
        return ok({"items": db.rows(conn, "SELECT login, name, teams_id, slack_id, updated_at FROM people ORDER BY login")})


@endpoint("PUT", "/admin/people/{login}", "admin", "Set a person's name, Teams user and Slack member ID", "admin",
          params=[("login", "path", "string", "GitHub login")],
          body={"name": ("string", "Their name"), "teams_id": ("string", "Teams user principal name"),
                "slack_id": ("string", "Slack member ID, U…")})
def admin_people_put(login: str):
    b = body()
    with db.get_conn() as conn:
        r = actions.person_save(conn, login, b.get("name", ""), b.get("teams_id", ""), b.get("slack_id", ""))
        if r.ok and r.data.get("changed"):
            settings.history_add(conn, actor(), f"People: updated {login.lower()}")
        return result(r)


@endpoint("GET", "/admin/users", "admin", "Accounts, and how each signs in", "admin")
def admin_users():
    with db.get_conn() as conn:
        rows = settings.users(conn)
    return ok({"items": [{"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"],
                          "active": u["active"], "via_github": u.get("via_github"), "github_login": u.get("github_login"),
                          "last_login": u.get("last_login")} for u in rows]})


@endpoint("POST", "/admin/users", "admin", "Add an account; returns its one-time setup link", "admin", status=201,
          body={"name": ("string", "Their name"), "email": ("string", "Their email"),
                "role": ("string", "admin, member or viewer")})
def admin_users_add():
    b = body()
    with db.get_conn() as conn:
        r = actions.user_add(conn, b.get("name", ""), b.get("email", ""), b.get("role", ""), actor())
    if r.ok:
        r.data["setup_link"] = request.url_root.rstrip("/") + f"/welcome/{r.data.pop('token')}"
    return result(r)


@endpoint("PATCH", "/admin/users/{user_id}", "admin", "Change an account's role", "admin",
          params=[("user_id", "path", "integer", "Its id")], body={"role": ("string", "admin, member or viewer")})
def admin_users_patch(user_id: int):
    me_id = g.api["user_id"] or -1
    with db.get_conn() as conn:
        return result(actions.user_role(conn, me_id, user_id, body().get("role", ""), actor()))


@endpoint("POST", "/admin/users/{user_id}/setup-link", "admin", "A new one-time setup link for an account", "admin",
          params=[("user_id", "path", "integer", "Its id")])
def admin_users_link(user_id: int):
    with db.get_conn() as conn:
        r = actions.user_link(conn, user_id, actor())
    if r.ok:
        r.data["setup_link"] = request.url_root.rstrip("/") + f"/welcome/{r.data.pop('token')}"
    return result(r)


@endpoint("DELETE", "/admin/users/{user_id}", "admin", "Remove an account", "admin",
          params=[("user_id", "path", "integer", "Its id")])
def admin_users_delete(user_id: int):
    with db.get_conn() as conn:
        return result(actions.user_delete(conn, g.api["user_id"] or -1, user_id, actor()))


@endpoint("GET", "/admin/service-tokens", "admin", "Service tokens (not the tokens themselves)", "admin")
def admin_service_tokens():
    with db.get_conn() as conn:
        return ok({"items": apitokens.service(conn)})


@endpoint("POST", "/admin/service-tokens", "admin", "Make a service token with its own role (shown once)", "admin",
          status=201, body={**TOKEN_BODY, "role": ("string", "admin, member or viewer")})
def admin_service_tokens_create():
    b = body()
    with db.get_conn() as conn:
        row, token = apitokens.create(conn, b.get("name", ""), b.get("scope", "write"), actor(),
                                      role=b.get("role") or "invalid", expires_days=b.get("expires_days"))
    if not row:
        return error(400, None, token if b.get("role") in settings.ROLES else "role is admin, member or viewer.")
    return ok({**row, "token": token}, 201)


@endpoint("DELETE", "/admin/service-tokens/{token_id}", "admin", "Revoke a service token", "admin",
          params=[("token_id", "path", "integer", "Its id")])
def admin_service_tokens_revoke(token_id: int):
    with db.get_conn() as conn:
        done = apitokens.revoke(conn, token_id, actor())
    return ok({"ok": True, "message": "Revoked."}) if done else error(404, None, "No such service token.")


@endpoint("GET", "/admin/tls", "admin", "The TLS certificate in use", "admin")
def admin_tls():
    if not tls.enabled():
        return ok({"enabled": False})
    try:
        info = tls.describe()
    except (OSError, ValueError) as exc:
        return error(500, None, f"Can't read the current certificate ({type(exc).__name__}).")
    return ok({"enabled": True, "certificate": info, "previous": tls.has_previous()})


@endpoint("PUT", "/admin/tls", "admin", "Install a certificate (checked first), a new self-signed one, or the previous one",
          "admin", body={"cert": ("string", "PEM certificate"), "key": ("string", "PEM private key (write-only)"),
                         "chain": ("string", "PEM intermediates"), "self_signed": ("boolean", "Make a new self-signed one"),
                         "restore_previous": ("boolean", "Put the previous certificate back")})
def admin_tls_put():
    b = body()
    with db.get_conn() as conn:
        return result(actions.tls_install(conn, str(b.get("cert", "")).encode(), str(b.get("key", "")).encode(),
                                          str(b.get("chain", "")).encode(), actor(),
                                          self_signed=flag(b, "self_signed"), restore=flag(b, "restore_previous")))


@endpoint("GET", "/admin/deliveries", "admin", "Messages to Teams and Slack, newest first", "admin", paged=True,
          params=[("status", "query", "string", "pending, sent or failed"),
                  ("limit", "query", "integer", "1 to 200 (50)"), ("cursor", "query", "string", "From next_cursor")])
def admin_deliveries():
    status = request.args.get("status", "")
    if status and status not in ("pending", "sent", "failed"):
        return error(400, None, "status is pending, sent or failed.")
    with db.get_conn() as conn:
        rows = db.rows(conn, f"""SELECT n.id, c.name AS channel, n.kind, n.status, n.attempts, n.last_error,
                                        n.created_at, n.sent_at, n.payload->>'heading' AS heading
                                   FROM notifications n JOIN channels c ON c.id = n.channel_id
                                  {'WHERE n.status = %s' if status else ''}
                                  ORDER BY n.created_at DESC, n.id DESC LIMIT 1000""", (status,) if status else None)
    return page(rows)


@endpoint("GET", "/admin/scans", "admin", "Recent scans: what they read, skipped and cost", "admin")
def admin_scans():
    with db.get_conn() as conn:
        return ok({"items": db.rows(conn, "SELECT * FROM scan_log ORDER BY id DESC LIMIT 50")})


@endpoint("GET", "/admin/history", "admin", "Who changed what, from the dashboard and the API", "admin")
def admin_history():
    with db.get_conn() as conn:
        return ok({"items": settings.history(conn, 200)})


# ─── The OpenAPI document ───────────────────────────────────────────────────

TYPES = {"string": {"type": "string"}, "integer": {"type": "integer"}, "boolean": {"type": "boolean"},
         "array": {"type": "array", "items": {"type": "string"}}}
ROLE_FOR = {"read": "viewer", "act": "member", "admin": "admin", "self": "viewer"}


def openapi() -> dict:
    paths: dict = {}
    for e in ENDPOINTS:
        op = {"operationId": f"{e.method.lower()}{e.path.replace('/', '_').replace('{', '').replace('}', '')}",
              "summary": e.summary, "tags": [e.tag],
              "description": f"Needs the {ROLE_FOR[e.need]} role or higher"
                             + ("" if e.need == "read" else ", and a token that isn't read-only") + ".",
              "parameters": [{"name": n, "in": w, "required": w == "path", "description": d,
                              "schema": TYPES[t]} for n, w, t, d in e.params],
              "responses": {str(e.status): {"description": "OK", "content": {"application/json": {"schema": {
                  "$ref": "#/components/schemas/Page"} if e.paged else {"type": "object"}}}},
                            "400": {"$ref": "#/components/responses/Error"},
                            "401": {"$ref": "#/components/responses/Error"},
                            "403": {"$ref": "#/components/responses/Error"},
                            "404": {"$ref": "#/components/responses/Error"},
                            "429": {"$ref": "#/components/responses/Error"}}}
        if e.body:
            props = {k: {**TYPES.get(t, {"type": "string"}), "description": d} for k, (t, d) in e.body.items()
                     if not k.startswith("<")}
            schema: dict = {"type": "object", "properties": props}
            if any(k.startswith("<") for k in e.body):
                schema["additionalProperties"] = True
            op["requestBody"] = {"required": False, "content": {"application/json": {"schema": schema}}}
        paths.setdefault(e.path, {})[e.method.lower()] = op
    return {
        "openapi": "3.0.3",
        "info": {"title": "Branchwarden API", "version": VERSION,
                 "description": "Everything the dashboard shows or does. Tokens act with their owner's role "
                                "(or a service token's own), and read-only tokens only read."},
        "servers": [{"url": "/api/v1"}],
        "security": [{"bearer": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer",
                                           "description": "A personal or service token: bwt_…"}},
            "schemas": {
                "Page": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}},
                                                          "next_cursor": {"type": "string", "nullable": True},
                                                          "total": {"type": "integer"}}},
                "Error": {"type": "object", "properties": {"error": {"type": "object", "properties": {
                    "status": {"type": "integer"}, "code": {"type": "string"}, "message": {"type": "string"}}}}}},
            "responses": {"Error": {"description": "An error", "content": {"application/json": {"schema": {
                "$ref": "#/components/schemas/Error"}}}}}},
    }


@endpoint("GET", "/openapi.json", "read", "This API, described in OpenAPI 3", "meta")
def openapi_json():
    return ok(openapi())


# ─── The reference page (docs/api.md), from the same declarations ───────────

def markdown() -> str:
    """docs/api.md: every endpoint, who may call it, and what it takes. `python api.py --markdown > docs/api.md`."""
    who = {"read": "viewer", "act": "member", "admin": "admin", "self": "anyone (not read-only)"}
    out = ["# Branchwarden API",
           "",
           "Generated from `api.py` (`python api.py --markdown > docs/api.md`); CI checks it's current.",
           "The same, machine-readable: `GET /api/v1/openapi.json`.",
           "",
           "- **Base:** `https://<branchwarden>/api/v1`, HTTPS only.",
           "- **Auth:** `Authorization: Bearer bwt_…`, a personal token (your menu › API tokens) or a service token",
           "  (Settings › Users and access). A token acts with its owner's role (a service token with its own);",
           "  a read-only token only reads. Changes are recorded in the change history with the token named.",
           "- **Errors:** `{\"error\": {\"status\": 404, \"code\": \"not_found\", \"message\": \"…\"}}`.",
           "- **Lists:** `{\"items\": [...], \"next_cursor\": \"…\", \"total\": n}`; pass `?cursor=` for the next page,",
           "  `?limit=` (1 to 200, 50 by default).",
           f"- **Rate limit:** {RATE_LIMIT} requests a minute per token (`BRANCHWARDEN_API_RATE`); then 429 with `Retry-After`.",
           ""]
    tags: dict = {}
    for e in ENDPOINTS:
        tags.setdefault(e.tag, []).append(e)
    for tag, items in tags.items():
        out += [f"## {tag.capitalize()}", "", "| | Path | Who | What |", "|---|---|---|---|"]
        for e in items:
            extra = []
            if e.params:
                extra.append("params: " + ", ".join(f"`{n}`" for n, w, _, _ in e.params if w != "path"))
            if e.body:
                extra.append("body: " + ", ".join(f"`{k}`" for k in e.body))
            detail = e.summary + ("" if not [x for x in extra if not x.endswith(": ")] else
                                  " (" + "; ".join(x for x in extra if not x.endswith(": ")) + ")")
            out.append(f"| {e.method} | `{e.path}` | {who[e.need]} | {detail} |")
        out.append("")
    out += ["## Examples", "",
            "```bash",
            "TOKEN=bwt_…   # your menu › API tokens",
            "curl -s -H \"Authorization: Bearer $TOKEN\" https://branchwarden.example.com/api/v1/my-work",
            "curl -s -H \"Authorization: Bearer $TOKEN\" 'https://branchwarden.example.com/api/v1/pulls?filter=stalled&repo=api-*'",
            "curl -s -X POST -H \"Authorization: Bearer $TOKEN\" -H 'Content-Type: application/json' \\",
            "     -d '{\"add\": [\"sasha\"], \"dry_run\": true}' https://branchwarden.example.com/api/v1/pulls/42/reviewers",
            "```", ""]
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    if "--markdown" in sys.argv:
        print(markdown(), end="")
