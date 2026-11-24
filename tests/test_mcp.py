"""
The MCP server (branchwarden mcp): its tools and their annotations, and the real
protocol end to end. `branchwarden mcp` runs as a subprocess, the MCP SDK's own
client talks to it over stdio, and it talks to the running app with the demo data
(needs TEST_DATABASE_URL and the mcp extra).
"""

import asyncio
import json
import os
import sys

import pytest

mcp = pytest.importorskip("mcp")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli"))

from branchwarden_cli import mcp_server  # noqa: E402


def test_tools_and_their_annotations():
    tools = {t.name: t for t in asyncio.run(mcp_server.build().list_tools())}
    assert {"my_work", "list_pull_requests", "request_reviewers", "list_branches", "workflows", "start_scan"} <= set(tools)
    reads = [n for n, t in tools.items() if t.annotations.read_only_hint]
    writes = [n for n, t in tools.items() if not t.annotations.read_only_hint]
    assert set(writes) == {"request_reviewers", "snooze_reminders", "ignore_branch", "unignore_branch", "start_scan"}
    assert tools["request_reviewers"].annotations.open_world_hint is True          # it reaches GitHub
    assert all(not tools[n].annotations.open_world_hint for n in reads)
    schema = tools["request_reviewers"].input_schema["properties"]
    assert schema["dry_run"]["default"] is True                                  # nothing sent by accident
    for t in tools.values():
        assert t.description and len(t.description) > 40, t.name                 # enough for an agent to use


@pytest.fixture
def live(db_url, monkeypatch, tmp_path):
    import threading

    from werkzeug.serving import make_server

    import apitokens
    import dashboard
    import db
    import demo
    import settings
    with db.get_conn() as conn:
        demo.load(conn, settings.load(conn))
        uid = settings.create_user(conn, "m@example.com", "Member", "member", "test", password="a-long-test-password").user_id
        settings.set_my_login(conn, uid, "dana")
        write = apitokens.create(conn, "agent", "write", "test", user_id=uid)[1]
        read = apitokens.create(conn, "agent ro", "read", "test", user_id=uid)[1]
    dashboard.app.secret_key = "s" * 32
    monkeypatch.setenv("BRANCHWARDEN_INSECURE_HTTP", "1")
    srv = make_server("127.0.0.1", 0, dashboard.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {**os.environ, "BRANCHWARDEN_URL": f"http://127.0.0.1:{srv.server_port}", "BRANCHWARDEN_CLI_ALLOW_HTTP": "1",
           "XDG_CONFIG_HOME": str(tmp_path), "PYTHONPATH": os.path.join(os.path.dirname(os.path.dirname(
               os.path.abspath(__file__))), "cli")}
    yield {"env": env, "write": write, "read": read}
    srv.shutdown()


def session(env: dict, token: str, calls):
    """Start `branchwarden mcp` over stdio and run `calls(session)` against it."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=["-m", "branchwarden_cli.main", "mcp"],
                                   env={**env, "BRANCHWARDEN_TOKEN": token})

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await calls(s)
    return asyncio.run(go())


def payload(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc)
    return json.loads(result.content[0].text)


def test_over_stdio(live):
    async def calls(s):
        names = {t.name for t in (await s.list_tools()).tools}
        work = payload(await s.call_tool("my_work", {}))
        stalled = payload(await s.call_tool("list_pull_requests", {"filter": "stalled"}))["items"]
        dry = payload(await s.call_tool("request_reviewers", {"pr_id": stalled[0]["id"], "add": ["sam"]}))
        wf = payload(await s.call_tool("workflows", {"problems_only": True}))
        bad = await s.call_tool("get_repository", {"full_name": "nope"})
        return names, work, stalled, dry, wf, bad
    names, work, stalled, dry, wf, bad = session(live["env"], live["write"], calls)
    assert "list_branches" in names
    assert work["login"] == "dana" and len(work["reviews_waiting"]) == 3
    assert {p["number"] for p in stalled} == {55, 74}
    assert dry["dry_run"] is True and dry["add"] == ["sam"]            # a dry run unless asked otherwise
    assert {w["name"] for w in wf["items"]} == {"Nightly build", "Docs backup"}
    assert bad.is_error and "owner/name" in bad.content[0].text


def test_a_read_only_token_cant_act(live):
    async def calls(s):
        return await s.call_tool("start_scan", {}), await s.call_tool("overview", {})
    scan, ov = session(live["env"], live["read"], calls)
    assert scan.is_error and "read-only" in scan.content[0].text
    assert payload(ov)["open_prs"] == 16


# ─── Over HTTP (branchwarden mcp --http) ────────────────────────────────────

def _gate_answer(headers: list, client=("203.0.113.9", 5000), checks=None):
    """Send one request through TokenGate (no app behind it) and return (status, body)."""
    gate = mcp_server.TokenGate(None, "https://bw.example", None)
    if checks is not None:
        async def check(token):
            checks.append(token)
            return (3, "That token isn't valid.")
        gate._check = check
    out = []

    async def send(msg):
        out.append(msg)

    async def go():
        await gate({"type": "http", "headers": headers, "client": client}, None, send)
    asyncio.run(go())
    return out[0]["status"], json.loads(out[1]["body"]), dict(out[0]["headers"])


def test_http_needs_a_token_and_throttles_bad_ones():
    status, body, headers = _gate_answer([])
    assert status == 401 and "bwt_" in body["error"] and headers[b"www-authenticate"].startswith(b"Bearer")
    assert _gate_answer([(b"authorization", b"Basic eDp5")])[0] == 401
    gate = mcp_server.TokenGate(None, "https://bw.example", None)
    for _ in range(mcp_server.FAILS_PER_MINUTE):
        gate._failed("203.0.113.9")
    assert gate._throttled("203.0.113.9") and not gate._throttled("203.0.113.10")


def test_http_wont_send_tokens_in_the_clear():
    with pytest.raises(RuntimeError, match="in the clear"):
        mcp_server.run_http(host="0.0.0.0")
    with pytest.raises(RuntimeError, match="both"):
        mcp_server.run_http(host="0.0.0.0", certfile="c.pem")


def _serve_http(env: dict, monkeypatch):
    import socket
    import threading

    import uvicorn
    monkeypatch.setenv("BRANCHWARDEN_CLI_ALLOW_HTTP", "1")
    app = mcp_server.http_app(env["BRANCHWARDEN_URL"], None)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        __import__("time").sleep(0.05)
    return server, f"http://127.0.0.1:{port}/mcp"


def http_session(url: str, token: str, calls):
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async def go():
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
            async with streamable_http_client(url, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await s.initialize()
                    return await calls(s)
    return asyncio.run(go())


def test_over_http_each_client_with_its_own_token(live, monkeypatch):
    import urllib.error
    import urllib.request
    server, url = _serve_http(live["env"], monkeypatch)
    try:
        async def as_writer(s):
            return payload(await s.call_tool("my_work", {})), await s.call_tool("start_scan", {})
        work, scan = http_session(url, live["write"], as_writer)
        assert work["login"] == "dana" and not scan.is_error

        async def as_reader(s):
            return await s.call_tool("start_scan", {}), payload(await s.call_tool("overview", {}))
        scan, ov = http_session(url, live["read"], as_reader)
        assert scan.is_error and "read-only" in scan.content[0].text     # the reader's own token was used
        assert ov["open_prs"] == 16

        for token in (None, "bwt_not-a-real-token"):
            req = urllib.request.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req, timeout=10)
            assert err.value.code == 401
    finally:
        server.should_exit = True


def test_http_bad_tokens_in_all_stay_under_branchwarden_s_limit():
    # Branchwarden refuses an address after 30 bad tokens a minute, good tokens included, and every
    # check comes from this server: guesses spread over many addresses must stop short of that.
    import api
    gate = mcp_server.TokenGate(None, "https://bw.example", None)
    assert mcp_server.FAILS_IN_ALL_PER_MINUTE < api.FAILED_LIMIT
    for i in range(mcp_server.FAILS_IN_ALL_PER_MINUTE):
        gate._failed(f"203.0.113.{i}")
    assert gate._throttled(mcp_server.ALL) and not gate._throttled("203.0.113.1")
    known = "bwt_known"
    gate.good[__import__("hashlib").sha256(known.encode()).hexdigest()] = __import__("time").monotonic() + 60
    assert gate._known(known) and not gate._known("bwt_new")        # a known token still gets through
