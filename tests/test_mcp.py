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
    reads = [n for n, t in tools.items() if t.annotations.readOnlyHint]
    writes = [n for n, t in tools.items() if not t.annotations.readOnlyHint]
    assert set(writes) == {"request_reviewers", "snooze_reminders", "ignore_branch", "unignore_branch", "start_scan"}
    assert tools["request_reviewers"].annotations.openWorldHint is True          # it reaches GitHub
    assert all(not tools[n].annotations.openWorldHint for n in reads)
    schema = tools["request_reviewers"].inputSchema["properties"]
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
    assert not result.isError, result.content
    if result.structuredContent is not None:
        sc = result.structuredContent
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
    assert bad.isError and "owner/name" in bad.content[0].text


def test_a_read_only_token_cant_act(live):
    async def calls(s):
        return await s.call_tool("start_scan", {}), await s.call_tool("overview", {})
    scan, ov = session(live["env"], live["read"], calls)
    assert scan.isError and "read-only" in scan.content[0].text
    assert payload(ov)["open_prs"] == 16
