"""
Tests against a real Postgres (TEST_DATABASE_URL; skipped without it): the
scanner end to end with a stand-in for GitHub, the events it produces, the
delivery queue, workflows, skipping unchanged repos, the demo data, and every
page of the dashboard rendering with it.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest

import actions
import db
import demo
import notify
import rules
import scanner
import settings
import worker
import workflows
from github import GitHubError, RateLimit

NOW = datetime.now(timezone.utc).replace(microsecond=0)
iso = lambda d: (NOW - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")   # noqa: E731


def pr_node(n, *, draft=False, reviews=(), title=None, updated=0.5):
    return {"number": n, "title": title or f"PR {n}", "url": f"https://github.com/acme/api/pull/{n}", "isDraft": draft,
            "state": "OPEN", "createdAt": iso(3), "updatedAt": iso(updated), "author": {"login": "dana"},
            "headRefName": f"feat/{n}", "headRefOid": f"oid{n}", "baseRefName": "main", "mergeable": "MERGEABLE",
            "reviewDecision": "APPROVED" if any(s == "APPROVED" for _, s in reviews) else "REVIEW_REQUIRED",
            "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": "User", "login": "lee"}}]},
            "latestReviews": {"nodes": [{"author": {"login": who}, "state": s, "submittedAt": iso(0.2)} for who, s in reviews]},
            "commits": {"nodes": [{"commit": {"committedDate": iso(1), "statusCheckRollup": {"state": "SUCCESS"}}}]}}


class FakeGitHub:
    """Answers the scanner's queries from self.prs, self.branches, self.ended,
    self.workflows and self.runs, so a test can step through a repo's life."""

    label = "token"
    token_expires = None

    def __init__(self):
        self.rate = RateLimit()
        self.requests = 0
        self.pushed = iso(1)
        self.prs: list[dict] = []
        self.branches = ["main"]
        self.ended: dict[int, str] = {}
        self.workflows: list[dict] = []
        self.runs: list[dict] = []
        self.files = ""

    def graphql(self, query, variables):
        self.requests += 1
        if "organization(" in query:
            return {"organization": {"repositories": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{
                "name": "api", "nameWithOwner": "acme/api", "url": "https://github.com/acme/api", "isArchived": False,
                "isFork": False, "isPrivate": True, "owner": {"login": "acme"},
                "defaultBranchRef": {"name": "main", "branchProtectionRule": None}, "deleteBranchOnMerge": False,
                "pushedAt": self.pushed,
                "pullRequests": {"totalCount": len(self.prs), "nodes": [{"updatedAt": max((p["updatedAt"] for p in self.prs), default=None)}]}}]}}}
        if "states: [MERGED, CLOSED]" in query:
            return {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}}
        if "co1:" in query:
            return {"repository": {"co1": {"text": "* @acme/core\n"}, "co2": None, "co3": None,
                                   "pullRequests": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self.prs}}}
        if "refs(" in query:
            return {"repository": {"refs": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [
                {"name": b, "target": {"oid": f"head-{b}", "committedDate": iso(40 if b.startswith("old") else 1),
                                       "author": {"name": "Dana", "user": {"login": "dana"}}},
                 "associatedPullRequests": {"nodes": []}} for b in self.branches]}}}
        if "pullRequest(number:" in query:
            return {"repository": {f"p{n}": {"state": st, "mergedAt": iso(0.1) if st == "MERGED" else None,
                                             "closedAt": iso(0.1), "mergedBy": {"login": "sasha"} if st == "MERGED" else None,
                                             "author": {"login": "dana"}, "firstReviews": {"nodes": []}}
                                   for n, st in self.ended.items()}}
        if "HEAD:.github/workflows" in query:
            return {"repository": {"object": {"entries": [
                {"name": "nightly.yml", "type": "blob", "object": {"text": self.files, "byteSize": len(self.files)}}]}}}
        raise AssertionError(f"unexpected query: {query[:80]}")

    def paged(self, path, limit=1000, key="installations"):
        self.requests += 1
        return self.workflows if key == "workflows" else self.runs if key == "workflow_runs" else []

    def rest(self, method, path, **kw):
        self.requests += 1
        if "/rules/" in path:
            raise GitHubError("Upgrade to GitHub Pro", 403)

        class R:
            def json(self_inner):
                return {"protected": False}
        return R()


class Conns:
    configured = True

    def __init__(self, gh):
        self.gh = gh

    def client_for(self, login, via="auto"):
        return self.gh


def scan(conn, gh):
    target = db.one(conn, "SELECT * FROM targets WHERE login = 'acme'")
    result = scanner.ScanResult()
    scanner.scan_target(conn, Conns(gh), target, settings.load(conn), result)
    return result


def add_target(conn):
    db.execute(conn, "INSERT INTO targets (login, kind, via) VALUES ('acme', 'org', 'token')")
    conn.commit()


# ─── The scanner, end to end ────────────────────────────────────────────────

def test_scan_events_and_branch_bookkeeping(db_url):
    gh = FakeGitHub()
    gh.prs = [pr_node(1, draft=True), pr_node(2)]
    gh.branches = ["main", "feat/1", "feat/2", "old/spike"]
    with db.get_conn() as conn:
        add_target(conn)
        first = scan(conn, gh)
        assert first.events == [] and first.repos == 1 and first.prs == 2      # a first scan is silent
        assert db.one(conn, "SELECT codeowners FROM repos")["codeowners"] == "* @acme/core\n"

        gh.pushed = iso(0.5)
        gh.prs = [pr_node(1), pr_node(2, reviews=[("lee", "APPROVED")]), pr_node(3)]
        kinds = sorted(e["kind"] for e in scan(conn, gh).events)
        assert kinds == ["opened", "ready", "review"]

        gh.pushed = iso(0.1)
        gh.prs = [pr_node(1)]
        gh.ended = {2: "MERGED", 3: "CLOSED"}
        gh.branches = ["main", "feat/1", "old/spike"]
        kinds = sorted(e["kind"] for e in scan(conn, gh).events)
        assert kinds == ["closed", "merged"]
        merged = db.one(conn, "SELECT state, merged_by FROM pull_requests WHERE number = 2")
        assert merged == {"state": "merged", "merged_by": "sasha"}
        assert [r["branch"] for r in db.rows(conn, "SELECT branch FROM branch_deletions")] == ["feat/2"]
        assert db.one(conn, "SELECT COUNT(*) AS n FROM branches")["n"] == 3


def test_unchanged_repo_is_skipped(db_url):
    gh = FakeGitHub()
    with db.get_conn() as conn:
        add_target(conn)
        scan(conn, gh)
        before = gh.requests
        again = scan(conn, gh)
        assert again.skipped == 1 and again.prs == 0
        assert gh.requests - before == 1                   # only the repo list
        gh.prs = [pr_node(1)]                               # an open PR is always read in full
        result = scan(conn, gh)
        assert result.skipped == 0 and result.prs == 1
        kinds = [e["kind"] for e in result.events]
        assert kinds == ["opened"]                          # a review or check can't be missed


def test_scan_threads_setting(db_url):
    with db.get_conn() as conn:
        assert not actions.scan_settings(conn, {"SCAN_THREADS": "9"}, "test").ok
        assert not actions.scan_settings(conn, {"SCAN_THREADS": "junk"}, "test").ok
        assert actions.scan_settings(conn, {"SCAN_THREADS": "2"}, "test").ok
        assert settings.load(conn)["SCAN_THREADS"] == "2"


def test_scan_log_records_timing_and_points(db_url, monkeypatch):
    gh = FakeGitHub()
    monkeypatch.setattr(worker, "Connections", lambda values: Conns(gh))
    with db.get_conn() as conn:
        add_target(conn)
        result = worker.run_scan(conn, settings.load(conn), "manual")
        row = db.one(conn, "SELECT * FROM scan_log ORDER BY id DESC LIMIT 1")
        assert row["elapsed_seconds"] >= 0
        assert row["api_points"] == result.api_points
        assert row["api_points"] >= result.requests        # one point per call at least


# ─── Messages: queued, sent, retried, failed ────────────────────────────────

def test_delivery_retries_and_failures(db_url, monkeypatch):
    with db.get_conn() as conn:
        for name, enabled in (("ok", True), ("busy", True), ("gone", True), ("off", False)):
            cid = db.one(conn, "INSERT INTO channels (kind, name, enabled) VALUES ('slack', %s, %s) RETURNING id",
                         (name, enabled))["id"]
            settings.save(conn, {settings.channel_url_key(cid): f"https://hooks.slack.com/services/{name}"}, "test")
        chans = db.rows(conn, "SELECT * FROM channels ORDER BY id")
        notify.enqueue(conn, chans, "test", {"heading": "Hello"})
        conn.commit()

        class Resp:
            def __init__(self, code):
                self.status_code, self.text = code, ""
        codes = {"ok": 200, "busy": 429, "gone": 404}
        monkeypatch.setattr(notify.requests, "post",
                            lambda url, **kw: Resp(codes[url.rsplit("/", 1)[1]]))
        sent, failed = notify.deliver(conn)
        assert (sent, failed) == (1, 2)
        rows = {r["name"]: r for r in db.rows(conn, """SELECT c.name, n.status, n.attempts, n.last_error, n.next_attempt_at
                                                         FROM notifications n JOIN channels c ON c.id = n.channel_id""")}
        assert rows["ok"]["status"] == "sent"
        assert rows["busy"]["status"] == "pending" and rows["busy"]["attempts"] == 1
        assert rows["busy"]["next_attempt_at"] > datetime.now(timezone.utc) + timedelta(seconds=50)   # back-off: 1 min
        assert rows["gone"]["status"] == "failed" and rows["gone"]["last_error"].startswith("HTTP 404")
        assert rows["off"]["status"] == "failed" and "switched off" in rows["off"]["last_error"]
        assert "hooks.slack.com" not in " ".join(r["last_error"] or "" for r in rows.values())   # the URL is the secret


def test_events_route_to_channels(db_url):
    gh = FakeGitHub()
    with db.get_conn() as conn:
        add_target(conn)
        scan(conn, gh)
        db.execute(conn, "INSERT INTO channels (kind, name, repos) VALUES ('teams', 'api', 'acme/api'), ('teams', 'web', 'acme/web')")
        gh.pushed = iso(0.2)
        gh.prs = [pr_node(5, title="ABC-1 Add search")]
        events = scan(conn, gh).events
        assert rules.handle_events(conn, events, settings.load(conn)) == 1
        n = db.one(conn, "SELECT c.name, n.kind, n.payload FROM notifications n JOIN channels c ON c.id = n.channel_id")
        assert n["name"] == "api" and n["kind"] == "opened" and n["payload"]["heading"] == "New pull request"


# ─── Workflows ──────────────────────────────────────────────────────────────

def test_workflow_scan_and_failing_event(db_url):
    gh = FakeGitHub()
    gh.files = "on:\n  schedule:\n    - cron: '0 3 * * *'\n"
    gh.workflows = [{"id": 7, "name": "Nightly", "path": ".github/workflows/nightly.yml", "state": "active",
                     "created_at": iso(30)}]

    def run(rid, days, conclusion):
        return {"id": rid, "workflow_id": 7, "name": "Nightly", "event": "schedule", "status": "completed",
                "conclusion": conclusion, "head_branch": "main", "head_sha": "abc", "run_number": rid,
                "created_at": iso(days), "run_started_at": iso(days), "updated_at": iso(days),
                "html_url": f"https://github.com/acme/api/actions/runs/{rid}"}
    gh.runs = [run(1, 2, "success")]
    with db.get_conn() as conn:
        add_target(conn)
        scan(conn, gh)
        w = db.one(conn, "SELECT * FROM workflows")
        assert w["crons"] == ["0 3 * * *"] and w["triggers"] == ["schedule"]
        gh.runs = [run(1, 2, "success"), run(2, 1, "failure")]
        gh.pushed = iso(0.05)
        events = scan(conn, gh).events
        assert [e["kind"] for e in events if e["kind"].startswith("wf_")] == ["wf_failed"]
        page = workflows.load(conn, settings.load(conn), 7, "acme/api")
        assert page["rows"][0]["failing_since"] is not None


# ─── Demo data ──────────────────────────────────────────────────────────────

def test_demo_load_and_remove(db_url):
    with db.get_conn() as conn:
        found = demo.load(conn, settings.load(conn))
        assert found["repos"] == 6 and found["open"] == 16 and found["runs"] > 100
        values = settings.load(conn)
        assert demo.loaded(values) and settings.get(values, "TICKET_URL") == demo.TICKETS
        dana = [p for p in db.rows(conn, "SELECT requested FROM pull_requests WHERE state = 'open' AND NOT draft")
                if {"user": "dana"} in p["requested"]]
        assert len(dana) == 3                                       # My work: three reviews waiting on Dana
        assert demo.load(conn, values)["open"] == 16                # loading again replaces it
        assert demo.remove(conn, settings.load(conn))
        assert db.one(conn, "SELECT COUNT(*) AS n FROM repos")["n"] == 0
        assert not demo.loaded(settings.load(conn)) and not settings.get(settings.load(conn), "TICKET_URL")


def test_demo_refuses_real_data(db_url):
    with db.get_conn() as conn:
        settings.save(conn, {"GITHUB_TOKEN": "github_pat_x"}, "test")
        with pytest.raises(demo.Refused):
            demo.load(conn, settings.load(conn))
        settings.save(conn, {"GITHUB_TOKEN": None}, "test")
        add_target(conn)
        with pytest.raises(demo.Refused):
            demo.load(conn, settings.load(conn))


# ─── The dashboard: every page renders, and forms post ──────────────────────

def _client(conn):
    import dashboard
    dashboard.app.secret_key = "s" * 32
    settings.create_user(conn, "dana@demo-org.example", "Dana", "admin", "test", password="a-long-test-password")
    conn.commit()
    c = dashboard.app.test_client()
    c.environ_base["wsgi.url_scheme"] = "https"
    page = c.get("/login", base_url="https://localhost").get_data(as_text=True)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    resp = c.post("/login", base_url="https://localhost",
                  data={"csrf": csrf, "email": "dana@demo-org.example", "password": "a-long-test-password"})
    assert resp.status_code == 302, resp.get_data(as_text=True)[:300]
    return c, csrf, dashboard.app


def test_worker_stale_banner_reaches_every_page(db_url, monkeypatch):
    import dashboard
    with db.get_conn() as conn:
        c, _, _ = _client(conn)
    overdue = {"since": NOW - timedelta(minutes=25), "interval": 10}
    monkeypatch.setattr(dashboard, "_worker_overdue", lambda: overdue)
    for path in ("/", "/pulls", "/branches", "/me"):
        body = c.get(path, base_url="https://localhost").get_data(as_text=True)
        assert "Scans have stopped." in body and "every 10 minutes" in body, path
    monkeypatch.setattr(dashboard, "_worker_overdue", lambda: None)
    assert "Scans have stopped." not in c.get("/", base_url="https://localhost").get_data(as_text=True)


def test_every_page_renders_with_demo_data(db_url):
    with db.get_conn() as conn:
        demo.load(conn, settings.load(conn))
        conn.commit()
        c, csrf, app = _client(conn)
        skip = {"admin.logout", "admin.login", "admin.setup", "admin.welcome", "admin.github_login",
                "admin.github_callback", "healthz", "static", "deliveries", "prometheus"}   # (its own test: test_prom.py)
        paths = sorted({r.rule for r in app.url_map.iter_rules()
                        if "GET" in r.methods and not r.arguments and r.endpoint not in skip
                        and not r.endpoint.startswith("api.")})      # the API has its own tests (test_api.py)
        assert "/repos" in paths and "/workflows" in paths and "/settings/github" in paths
        pr = db.one(conn, "SELECT id FROM pull_requests WHERE state = 'open' ORDER BY id LIMIT 1")["id"]
        paths += ["/repos/demo-org/billing-api", f"/pulls?assign={pr}", "/pulls?state=stalled", "/branches?tab=left",
                  "/workflows?days=30&show=problems", "/metrics?days=180", "/mvp?period=prev"]
        for path in paths:
            resp = c.get(path, base_url="https://localhost", follow_redirects=True)
            body = resp.get_data(as_text=True)
            assert resp.status_code == 200, (path, resp.status_code)
            assert "Demo data." in body, path                       # the banner, on every page
        assert c.get("/repos/demo-org/nope", base_url="https://localhost").status_code == 404

        # Forms post and land back with a message.
        resp = c.post("/scan", base_url="https://localhost", data={"csrf": csrf})
        assert resp.status_code == 302
        resp = c.post("/settings/rules/stalled", base_url="https://localhost",
                      data={"csrf": csrf, "after": "5", "repeat": "5", "max": "2", "unit": "days", "target": "all"})
        assert resp.status_code == 302 and rules.load(conn)["stalled"]["params"]["after"] == 5
        resp = c.post("/settings/github/token", base_url="https://localhost",
                      data={"csrf": csrf, "GITHUB_TOKEN": "github_pat_x", "GITHUB_TOKEN_ENABLED": "1"})
        assert resp.status_code == 302 and not settings.load(conn).get("GITHUB_TOKEN")   # refused while demo is loaded
        assert c.post("/scan", base_url="https://localhost", data={}).status_code == 400  # no CSRF token
