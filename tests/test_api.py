"""
The API against a real Postgres (TEST_DATABASE_URL; skipped without it), with
the demo data: tokens, every endpoint's role check, and the main calls end to end.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest

import api
import apitokens
import db
import demo
import settings


@pytest.fixture
def env(db_url, monkeypatch):
    import dashboard
    api._calls.clear()
    api._failed.clear()
    with db.get_conn() as conn:
        demo.load(conn, settings.load(conn))
        ids = {}
        for role in ("admin", "member", "viewer"):
            new = settings.create_user(conn, f"{role}@example.com", role.capitalize(), role, "test",
                                       password="a-long-test-password")
            ids[role] = new.user_id
        settings.set_my_login(conn, ids["member"], "dana")
        tokens = {}
        for role, uid in ids.items():
            tokens[role] = apitokens.create(conn, f"{role} token", "write", "test", user_id=uid)[1]
        tokens["admin_read"] = apitokens.create(conn, "read-only", "read", "test", user_id=ids["admin"])[1]
        tokens["service_viewer"] = apitokens.create(conn, "grafana", "read", "test", role="viewer")[1]
    client = dashboard.app.test_client()
    return {"client": client, "tokens": tokens, "ids": ids}


def call(env, method, path, who="admin", json=None, token=None, https=True, **kw):
    headers = {"Authorization": f"Bearer {token or env['tokens'][who]}"}
    return env["client"].open("/api/v1" + path, method=method, json=json, headers=headers,
                              base_url="https://localhost" if https else "http://localhost", **kw)


# ─── Tokens ─────────────────────────────────────────────────────────────────

def test_token_life(db_url):
    with db.get_conn() as conn:
        uid = settings.create_user(conn, "a@example.com", "A", "member", "test", password="a-long-test-password").user_id
        row, token = apitokens.create(conn, "cli", "write", "A", user_id=uid, expires_days=1)
        assert token.startswith("bwt_") and row["prefix"] == token[:12]
        stored = db.one(conn, "SELECT token_hash FROM api_tokens")["token_hash"]
        assert token not in stored and len(stored) == 64                       # only a hash
        who = apitokens.authenticate(conn, token)
        assert (who["role"], who["scope"], who["user_name"]) == ("member", "write", "A")
        assert who["actor"] == f"A (API token {token[:12]}…)"
        settings.set_role(conn, uid, "viewer", "test")                          # the token follows its owner's role
        assert apitokens.authenticate(conn, token)["role"] == "viewer"
        assert apitokens.authenticate(conn, token, now=datetime.now(timezone.utc) + timedelta(days=2)) is None
        assert apitokens.revoke(conn, row["id"], "A", user_id=uid)
        assert apitokens.authenticate(conn, token) is None
        _, other = apitokens.create(conn, "second", "read", "A", user_id=uid)
        settings.create_user(conn, "b@example.com", "B", "admin", "test", password="a-long-test-password")
        settings.delete_user(conn, uid, "test")
        assert apitokens.authenticate(conn, other) is None                     # its owner has gone
        assert apitokens.create(conn, "", "write", "x", user_id=1)[0] is None
        assert apitokens.create(conn, "x", "admin", "x", user_id=1)[0] is None
        assert apitokens.create(conn, "x", "write", "x", role="viewer", expires_days=0)[0] is None


# ─── Every endpoint enforces the same role as the dashboard ─────────────────

FILL = {"pr_id": "999999", "branch_id": "999999", "target_id": "999999", "channel_id": "999999",
        "user_id": "999999", "token_id": "999999", "owner": "demo-org", "name": "billing-api",
        "key": "stalled", "section": "scanning", "login": "dana"}


@pytest.mark.parametrize("who", ["viewer", "member", "admin", "admin_read", "service_viewer"])
def test_every_endpoint_checks_the_role(env, who):
    import access
    role, scope = {"viewer": ("viewer", "write"), "member": ("member", "write"), "admin": ("admin", "write"),
                   "admin_read": ("admin", "read"), "service_viewer": ("viewer", "read")}[who]
    for e in api.ENDPOINTS:
        path = re.sub(r"\{(\w+)\}", lambda m: FILL[m.group(1)], e.path)
        resp = call(env, e.method, path, who, json={} if e.method != "GET" else None)
        should = access.allowed(role, e.need, scope)
        if should:
            assert resp.status_code != 403, (who, e.method, path, resp.get_json())
        else:
            assert resp.status_code == 403, (who, e.method, path, resp.status_code)
            assert resp.get_json()["error"]["code"] == "forbidden"


# ─── The main calls, end to end ─────────────────────────────────────────────

def test_reads(env):
    me = call(env, "GET", "/me", "member").get_json()
    assert me["role"] == "member" and me["can"] == {"read": True, "act": True, "admin": False, "self": True}
    ov = call(env, "GET", "/overview", "viewer").get_json()
    assert ov["open_prs"] == 16 and ov["demo"] is True and ov["stale_branches"] == 7

    first = call(env, "GET", "/pulls?limit=5", "viewer").get_json()
    assert len(first["items"]) == 5 and first["total"] == 16 and first["next_cursor"]
    second = call(env, "GET", f"/pulls?limit=5&cursor={first['next_cursor']}", "viewer").get_json()
    assert {p["id"] for p in first["items"]}.isdisjoint({p["id"] for p in second["items"]})
    stalled = call(env, "GET", "/pulls?filter=stalled", "viewer").get_json()["items"]
    assert {p["number"] for p in stalled} == {55, 74}
    assert call(env, "GET", "/pulls?filter=nope", "viewer").status_code == 400
    waiting_on_dana = call(env, "GET", "/pulls?reviewer=dana", "viewer").get_json()["items"]
    assert len(waiting_on_dana) == 3
    one = call(env, "GET", f"/pulls/{stalled[0]['id']}", "viewer").get_json()
    assert one["repo"].startswith("demo-org/") and "snoozed" in one

    branches = call(env, "GET", "/branches?status=stale", "viewer").get_json()
    assert branches["total"] == 7 and branches["items"][0]["age_days"] == 212
    assert call(env, "GET", "/repos/demo-org/billing-api", "viewer").get_json()["health"][0]["key"] == "auto_delete"
    assert call(env, "GET", "/repos/demo-org/nope", "viewer").status_code == 404
    wf = call(env, "GET", "/workflows?days=7", "viewer").get_json()
    assert any(w["failing_since"] for w in wf["items"])
    work = call(env, "GET", "/my-work", "member").get_json()                     # the member follows dana
    assert len(work["reviews_waiting"]) == 3 and work["login"] == "dana"
    assert call(env, "GET", "/my-work", "service_viewer").status_code == 409
    assert call(env, "GET", "/metrics?days=90", "viewer").get_json()["by_size"]
    assert call(env, "GET", "/mvp", "viewer").get_json()["boards"][0]["key"] == "reviews"
    spec = call(env, "GET", "/openapi.json", "viewer").get_json()
    assert spec["openapi"].startswith("3.") and "/pulls" in spec["paths"]


def test_writes_are_recorded_with_the_token(env):
    pr = call(env, "GET", "/pulls?filter=review&limit=1", "member").get_json()["items"][0]
    r = call(env, "POST", f"/pulls/{pr['id']}/snooze", "member", json={"days": 3})
    assert r.status_code == 200 and r.get_json()["until"]
    assert call(env, "POST", f"/pulls/{pr['id']}/snooze", "member", json={"days": 2}).status_code == 400
    assert call(env, "DELETE", f"/pulls/{pr['id']}/snooze", "member").get_json()["until"] is None
    plan = call(env, "POST", f"/pulls/{pr['id']}/reviewers", "member", json={"add": ["sam"], "dry_run": True}).get_json()
    assert plan["dry_run"] and plan["add"] == ["sam"]
    assert call(env, "POST", f"/pulls/{pr['id']}/reviewers", "member", json={"add": "sam"}).status_code == 400

    stale = call(env, "GET", "/branches?status=stale&q=tmp/backfill", "member").get_json()["items"][0]
    assert call(env, "POST", f"/branches/{stale['id']}/ignore", "member").status_code == 200
    ignored = call(env, "GET", "/branches/ignored", "viewer").get_json()["items"]
    assert any(i["name"] == "tmp/backfill-2026" for i in ignored)
    assert call(env, "DELETE", "/branches/ignored?repo=demo-org/data-pipeline&branch=tmp/backfill-2026",
                "member").status_code == 200
    assert call(env, "POST", "/scan", "member").status_code == 202

    with db.get_conn() as conn:
        lines = [h["actor"] + ": " + h["what"] for h in settings.history(conn, 20)]
    prefix = env["tokens"]["member"][:12]
    assert any(f"(API token {prefix}…)" in line and "snoozed" in line for line in lines), lines


def test_admin_calls(env):
    s = call(env, "GET", "/admin/settings").get_json()["settings"]
    assert s["STALE_DAYS"]["value"] == "30" and s["GITHUB_TOKEN"] == {**s["GITHUB_TOKEN"], "secret": True, "set": False}
    assert "value" not in s["GITHUB_TOKEN"]
    r = call(env, "PATCH", "/admin/settings/scanning", json={"STALE_DAYS": 45})
    assert r.status_code == 200 and "Branches are stale after" in r.get_json()["message"]
    assert call(env, "PATCH", "/admin/settings/scanning", json={"STALE_DAYS": "soon"}).status_code == 400
    assert call(env, "PATCH", "/admin/settings/github-token", json={"GITHUB_TOKEN": "x"}).status_code == 409   # demo
    r = call(env, "PATCH", "/admin/rules/stalled", json={"enabled": True, "after": 4, "unit": "days", "repeat": 4, "max": 2})
    assert r.status_code == 200
    rules = {x["key"]: x for x in call(env, "GET", "/admin/rules").get_json()["items"]}
    assert rules["stalled"]["enabled"] and rules["stalled"]["params"]["after"] == 4
    ch = call(env, "POST", "/admin/channels", json={"kind": "slack", "name": "#api", "repos": ["api-*", "web"],
                                                     "url": "https://hooks.slack.com/services/T/B/x"})
    assert ch.status_code == 201
    listed = {c["name"]: c for c in call(env, "GET", "/admin/channels").get_json()["items"]}
    assert listed["#api"]["repos"] == ["api-*", "web"] and listed["#api"]["webhook_set"] is True
    assert "hooks.slack.com" not in str(listed)                                   # write-only
    assert call(env, "POST", "/admin/channels", json={"kind": "slack", "name": "x", "url": "https://evil.example/x"}).status_code == 400
    u = call(env, "POST", "/admin/users", json={"name": "New", "email": "new@example.com", "role": "viewer"})
    assert u.status_code == 201 and "/welcome/" in u.get_json()["setup_link"]
    st = call(env, "POST", "/admin/service-tokens", json={"name": "ci", "role": "member", "scope": "read"})
    assert st.status_code == 201 and st.get_json()["token"].startswith("bwt_")
    assert call(env, "GET", "/me", token=st.get_json()["token"]).get_json()["role"] == "member"
    assert call(env, "DELETE", f"/admin/service-tokens/{st.get_json()['id']}").status_code == 200
    assert call(env, "GET", "/me", token=st.get_json()["token"]).status_code == 401


def test_personal_tokens_over_the_api(env):
    made = call(env, "POST", "/tokens", "viewer", json={"name": "laptop", "scope": "read", "expires_days": 30})
    assert made.status_code == 201
    t = made.get_json()
    assert call(env, "GET", "/me", token=t["token"]).get_json()["scope"] == "read"
    assert [x["name"] for x in call(env, "GET", "/tokens", "viewer").get_json()["items"]] == ["laptop", "viewer token"]
    assert call(env, "DELETE", f"/tokens/{t['id']}", "admin").status_code == 404        # not the admin's
    assert call(env, "DELETE", f"/tokens/{t['id']}", "viewer").status_code == 200


def test_refusals(env, monkeypatch):
    c = env["client"]
    assert c.get("/api/v1/me", base_url="https://localhost").status_code == 401
    assert call(env, "GET", "/me", token="bwt_nope").status_code == 401
    assert call(env, "GET", "/me", https=False).status_code == 403                      # HTTPS only
    assert c.get("/api/v1/nothing-here", base_url="https://localhost").get_json()["error"]["status"] == 404
    pr = call(env, "GET", "/pulls?limit=1", "member").get_json()["items"][0]["id"]
    r = call(env, "POST", f"/pulls/{pr}/snooze", "member", data="days=3", content_type="application/x-www-form-urlencoded")
    assert r.status_code == 415
    monkeypatch.setattr(api, "RATE_LIMIT", 3)
    codes = [call(env, "GET", "/me", "viewer").status_code for _ in range(5)]
    assert codes[-1] == 429 and codes[:2] == [200, 200]


def test_token_pages_in_the_dashboard(env):
    import dashboard
    dashboard.app.secret_key = "s" * 32
    c = dashboard.app.test_client()
    page = c.get("/login", base_url="https://localhost").get_data(as_text=True)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    c.post("/login", base_url="https://localhost", data={"csrf": csrf, "email": "viewer@example.com",
                                                        "password": "a-long-test-password"})
    resp = c.post("/me/tokens", base_url="https://localhost", data={"csrf": csrf, "name": "From the page",
                                                                    "scope": "read", "expires_days": "30"})
    body = resp.get_data(as_text=True)
    token = re.search(r'id="new-token">(bwt_[^<]+)<', body).group(1)
    assert call(env, "GET", "/me", token=token).get_json()["name"] == "Viewer"
    page = c.get("/me/tokens", base_url="https://localhost").get_data(as_text=True)
    assert "From the page" in page
    # The box with ready-to-copy commands for the CLI and an MCP client, for this server.
    assert "branchwarden login --url https://localhost" in page and "claude mcp add branchwarden" in page
    assert token not in c.get("/me/tokens", base_url="https://localhost").get_data(as_text=True)   # shown once


def test_github_sign_in_tokens_need_a_recent_sign_in(db_url):
    with db.get_conn() as conn:
        settings.create_user(conn, "admin@example.com", "Admin", "admin", "test", password="a-long-test-password")
        uid = settings.github_user(conn, {"id": 4242, "login": "gh-user", "name": "GH User"}, "member",
                                   "4242+gh-user@users.noreply.github.com")
        _, token = apitokens.create(conn, "cli", "write", "GH User", user_id=uid)
        assert apitokens.authenticate(conn, token)["role"] == "member"
        db.execute(conn, "UPDATE users SET last_login = NOW() - INTERVAL '31 days' WHERE id = %s", (uid,))
        why: list = []
        assert apitokens.authenticate(conn, token, why=why) is None and "sign in to the dashboard" in why[0]
        db.execute(conn, "UPDATE users SET last_login = NOW() WHERE id = %s", (uid,))     # signed in again
        assert apitokens.authenticate(conn, token) is not None
        _, second = apitokens.create(conn, "agent", "read", "GH User", user_id=uid)
        assert apitokens.revoke_all(conn, uid, "Branchwarden", "left the teams") == 2
        assert apitokens.authenticate(conn, token) is None and apitokens.authenticate(conn, second) is None


def test_bad_tokens_are_throttled_per_address(env, monkeypatch):
    monkeypatch.setattr(api, "FAILED_LIMIT", 3)
    api._failed.clear()
    codes = [call(env, "GET", "/me", token=f"bwt_bad{i}").status_code for i in range(6)]
    assert codes[:4] == [401, 401, 401, 401] and codes[-1] == 429
    assert call(env, "GET", "/me", "viewer").status_code == 429          # the address waits a minute, whatever the token
    api._failed.clear()
    assert call(env, "GET", "/me", "viewer").status_code == 200
