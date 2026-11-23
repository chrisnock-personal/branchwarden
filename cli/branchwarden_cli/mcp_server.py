"""
branchwarden mcp: Branchwarden's API as tools for AI agents, over MCP.

Over stdio (the default) it signs in the same way as the CLI (the keychain, or
BRANCHWARDEN_URL and BRANCHWARDEN_TOKEN, with a pinned certificate). Over HTTP
(--http, for agents on other machines) it keeps no token of its own: each client
sends its own Branchwarden API token (Authorization: Bearer bwt_…), which is
checked with Branchwarden and used for that client's calls.

Either way it only talks to the API, so the token's role and scope decide what
works: give an agent a read-only token and it can look but not change anything.

Tools that only read are marked read-only. Requesting reviewers writes to
GitHub: it's marked as reaching outside, and it's a dry run unless the agent
asks otherwise (dry_run=false), so nothing is sent by accident.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import sys
import threading
import time
from collections import deque
from contextvars import ContextVar
from typing import Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError   # its message reaches the agent; other errors are hidden
from mcp.types import ToolAnnotations

from . import __version__
from . import main as cli

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
LOCAL = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
GITHUB = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

INSTRUCTIONS = """\
Branchwarden watches a GitHub organisation's pull requests and branches. Use these tools to find
what's waiting on someone (my_work), which pull requests need attention (list_pull_requests with a
filter such as review, stalled, ci or big), which branches to tidy (list_branches), how repositories
and their GitHub Actions workflows are doing, and review metrics.

Pull requests and branches have an `id` (Branchwarden's own); use it with get_pull_request,
request_reviewers, snooze_reminders and ignore_branch. request_reviewers is a dry run unless you
pass dry_run=false: show the person what it would do first. What works depends on the token: a
read-only token can't change anything.
"""


# Over HTTP: where Branchwarden is, and the token of the request being served.
_upstream: dict = {}
_token: ContextVar[Optional[str]] = ContextVar("branchwarden_token", default=None)


def _client():
    token = _token.get()
    if token:
        return cli.Client(_upstream["url"], token, _upstream.get("fingerprint"))
    try:
        return cli.client(None)
    except cli.Fail as exc:
        raise ToolError(str(exc)) from None


def _call(method: str, path: str, body: Optional[dict] = None, params: Optional[dict] = None):
    try:
        return _client().request(method, path, body, params)
    except cli.Fail as exc:
        raise ToolError(str(exc)) from None


def _pages(path: str, params: dict, limit: int) -> dict:
    try:
        items = _client().all_pages(path, params, max(1, min(limit, 500)))
    except cli.Fail as exc:
        raise ToolError(str(exc)) from None
    return {"items": items, "count": len(items)}


def build() -> MCPServer:
    mcp = MCPServer("branchwarden", instructions=INSTRUCTIONS, version=__version__)

    @mcp.tool(annotations=READ)
    def whoami() -> dict:
        """Who the token acts as: name, role (viewer, member or admin), whether it's read-only, and what it can do."""
        return _call("GET", "/me")

    @mcp.tool(annotations=READ)
    def overview() -> dict:
        """Headline numbers: open PRs, needing review, changes requested, approved but not merged, CI failing,
        big, stalled, stale branches, branches left after a PR, and whether scans are running."""
        return _call("GET", "/overview")

    @mcp.tool(annotations=READ)
    def my_work() -> dict:
        """What's waiting on the token's owner: reviews asked of them (oldest first), their own open PRs with the
        next step each needs, their branches to tidy, and their snoozed reminders."""
        return _call("GET", "/my-work")

    @mcp.tool(annotations=READ)
    def list_pull_requests(filter: str = "all", repo: Optional[str] = None, author: Optional[str] = None,
                           reviewer: Optional[str] = None, search: Optional[str] = None, limit: int = 50) -> dict:
        """Open pull requests, longest waiting first.

        filter: all, review (needs review), changes (changes requested), approved, ci (CI failing), conflicts,
        draft, big, stalled, or noticket. repo: owner/name or a pattern like 'api-*'. author: a GitHub login.
        reviewer: a login or org/team asked to review. search: words in titles, repos, authors, branches.
        limit: at most this many (50; up to 500)."""
        return _pages("/pulls", {"filter": filter, "repo": repo, "author": author, "reviewer": reviewer,
                                 "q": search}, limit)

    @mcp.tool(annotations=READ)
    def get_pull_request(pr_id: int) -> dict:
        """One open pull request in full: state, who it's waiting on and for how long, reviewers asked and their
        reviews, size, tickets, and whether its reminders are snoozed. pr_id is the `id` from list_pull_requests."""
        return _call("GET", f"/pulls/{pr_id}")

    @mcp.tool(annotations=GITHUB)
    def request_reviewers(pr_id: int, add: Optional[list[str]] = None, remove: Optional[list[str]] = None,
                          notify: bool = False, dry_run: bool = True) -> dict:
        """Request (add) or withdraw (remove) reviewers on GitHub for a pull request. Entries are GitHub logins or
        org/team slugs. A DRY RUN by default: it only says what would change. Pass dry_run=false to send it to
        GitHub, after the person has agreed. notify=true also tells the PR's Teams and Slack channels."""
        return _call("POST", f"/pulls/{pr_id}/reviewers", {"add": add or [], "remove": remove or [],
                                                            "notify": notify, "dry_run": dry_run})

    @mcp.tool(annotations=LOCAL)
    def snooze_reminders(pr_id: int, days: Optional[int] = None) -> dict:
        """Put a pull request's reminders off for 1, 3 or 7 days, or turn them back on (days left out)."""
        if days is None:
            return _call("DELETE", f"/pulls/{pr_id}/snooze")
        return _call("POST", f"/pulls/{pr_id}/snooze", {"days": days})

    @mcp.tool(annotations=READ)
    def list_branches(status: Optional[str] = None, repo: Optional[str] = None, search: Optional[str] = None,
                      limit: int = 50) -> dict:
        """Flagged branches, oldest first. status: stale (no PR, idle), left (left after a merged or closed PR),
        active, open_pr, or all; by default stale and left. repo: owner/name or a pattern."""
        return _pages("/branches", {"status": status, "repo": repo, "q": search}, limit)

    @mcp.tool(annotations=LOCAL)
    def ignore_branch(branch_id: int) -> dict:
        """Stop flagging a branch (branch_id is the `id` from list_branches). Branchwarden never deletes branches."""
        return _call("POST", f"/branches/{branch_id}/ignore")

    @mcp.tool(annotations=LOCAL)
    def unignore_branch(repo: str, branch: str) -> dict:
        """Flag an ignored branch again. repo is owner/name."""
        return _call("DELETE", "/branches/ignored", params={"repo": repo, "branch": branch})

    @mcp.tool(annotations=READ)
    def list_repositories() -> dict:
        """Every repository with its open PRs, PRs needing review, stale and left-behind branches, failing
        workflows, and health flags (settings behind branch and review problems, with links to fix them)."""
        items = _call("GET", "/repos")["items"]
        return {"items": items, "count": len(items)}

    @mcp.tool(annotations=READ)
    def get_repository(full_name: str, days: int = 90) -> dict:
        """One repository (owner/name): health flags, open PRs, stale and left-behind branches, review metrics
        over `days` (30, 90 or 180), who reviews there, its CODEOWNERS rules, and where its messages go."""
        if "/" not in full_name:
            raise ToolError("Give the repository as owner/name.")
        return _call("GET", f"/repos/{full_name}", params={"days": days})

    @mcp.tool(annotations=READ)
    def workflows(days: int = 14, repo: Optional[str] = None, problems_only: bool = False) -> dict:
        """GitHub Actions workflows over 7, 14 or 30 days: runs and pass rate, failing since when, scheduled runs
        missed, workflows switched off, and recent runs. problems_only keeps the ones needing attention."""
        w = _call("GET", "/workflows", params={"days": days, "repo": repo})
        if problems_only:
            w["items"] = [x for x in w["items"] if x["problems"]]
        for x in w["items"]:
            x.pop("days", None)               # the day-by-day strip is for the dashboard; keep answers short
        return w

    @mcp.tool(annotations=READ)
    def metrics(days: int = 30, repo: Optional[str] = None) -> dict:
        """Review metrics over 30, 90 or 180 days: time to first review and to merge, merges per week, by repo
        and by PR size, and the reviewers with requests waiting."""
        return _call("GET", "/metrics", params={"days": days, "repo": repo})

    @mcp.tool(annotations=READ)
    def mvp(previous: bool = False) -> dict:
        """The last 7 days' (or, previous=true, the 7 before) most reviews, merges, branch prunes and PRs raised."""
        return _call("GET", "/mvp", params={"period": "prev" if previous else "this"})

    @mcp.tool(annotations=LOCAL)
    def start_scan() -> dict:
        """Ask Branchwarden to scan GitHub now (otherwise it scans every few minutes)."""
        return _call("POST", "/scan")

    return mcp


def run() -> None:
    try:
        cli.client(None)                       # fail now, with the reason, rather than on the first tool call
    except cli.Fail as exc:
        raise RuntimeError(str(exc)) from None
    build().run()                              # stdio


# ─── Over HTTP ──────────────────────────────────────────────────────────────

CHECK_SECONDS = 60          # a token that worked is trusted this long before it's checked again
FAILS_PER_MINUTE = 30       # bad tokens from one address, before it's told to wait


class TokenGate:
    """ASGI middleware: every HTTP request needs a Branchwarden API token, checked with Branchwarden's /me.

    Good answers are remembered for CHECK_SECONDS (by the token's hash), so a session doesn't cost a
    check per call; a revoked token stops working within that time. Bad tokens are counted per address,
    so guessing is throttled here rather than at Branchwarden, where every client shares this address."""

    def __init__(self, app, url: str, fingerprint: Optional[str]):
        self.app, self.url, self.fingerprint = app, url, fingerprint
        self.good: dict[str, float] = {}
        self.fails: dict[str, deque] = {}
        self.lock = threading.Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        who = (scope.get("client") or ("?", 0))[0]
        if self._throttled(who):
            return await _answer(send, 429, "Too many bad tokens from this address: try again in a minute.",
                                 {"retry-after": "60"})
        auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if not token:
            return await _answer(send, 401, "Send a Branchwarden API token: Authorization: Bearer bwt_…")
        problem = await self._check(token)
        if problem:
            self._failed(who)
            return await _answer(send, 401 if problem[0] == 3 else 502, problem[1])
        reset = _token.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            _token.reset(reset)

    async def _check(self, token: str) -> Optional[tuple[int, str]]:
        import anyio
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        with self.lock:
            if self.good.get(key, 0) > now:
                return None

        def ask():
            try:
                cli.Client(self.url, token, self.fingerprint, timeout=15).request("GET", "/me")
                return None
            except cli.Fail as exc:
                return (exc.code, str(exc) if exc.code == 3 else f"Can't check the token with Branchwarden: {exc}")
        problem = await anyio.to_thread.run_sync(ask)
        if problem is None:
            with self.lock:
                self.good = {k: t for k, t in self.good.items() if t > now}
                self.good[key] = now + CHECK_SECONDS
        return problem

    def _throttled(self, who: str) -> bool:
        with self.lock:
            q = self.fails.get(who)
            while q and q[0] < time.monotonic() - 60:
                q.popleft()
            return bool(q) and len(q) >= FAILS_PER_MINUTE

    def _failed(self, who: str) -> None:
        with self.lock:
            self.fails.setdefault(who, deque()).append(time.monotonic())


async def _answer(send, status: int, message: str, extra: Optional[dict] = None) -> None:
    body = json.dumps({"error": message}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if status == 401:
        headers.append((b"www-authenticate", b'Bearer realm="branchwarden"'))
    headers += [(k.encode(), v.encode()) for k, v in (extra or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def upstream() -> tuple[str, Optional[str]]:
    """Where Branchwarden is (and its pinned certificate), as the CLI would use: no token needed."""
    cfg = cli.load_config()
    url = os.environ.get("BRANCHWARDEN_URL") or cfg.get("url")
    if not url:
        raise RuntimeError("Where's Branchwarden? Set BRANCHWARDEN_URL (and BRANCHWARDEN_FINGERPRINT for its own "
                           "self-signed certificate), or run branchwarden login first.")
    url = cli.normalise_url(url)
    fp = os.environ.get("BRANCHWARDEN_FINGERPRINT") or (cfg.get("fingerprint") if cfg.get("url") == url else None)
    return url, (cli.clean_fingerprint(fp) if fp else None)


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def http_app(url: str, fingerprint: Optional[str], host: str = "127.0.0.1", path: str = "/mcp",
             allowed_hosts: Optional[list[str]] = None):
    """The MCP server as an ASGI app behind TokenGate. Stateless: any number of clients, nothing kept between calls."""
    from mcp.server.transport_security import TransportSecuritySettings
    _upstream.update(url=url, fingerprint=fingerprint)
    security = None
    if allowed_hosts:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts,
                                             allowed_origins=[])
    elif not _loopback(host):
        # Every request needs a token, which a page in someone's browser doesn't have, so a rebound
        # name gets nowhere; name the hosts with --allowed-host to check them as well.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = build().streamable_http_app(streamable_http_path=path, stateless_http=True, json_response=True,
                                      transport_security=security, host=host)
    return TokenGate(app, url, fingerprint)


def run_http(host: str = "127.0.0.1", port: int = 8765, path: str = "/mcp", certfile: Optional[str] = None,
             keyfile: Optional[str] = None, allow_http: bool = False, allowed_hosts: Optional[list[str]] = None) -> None:
    import uvicorn
    if not _loopback(host) and not certfile and not allow_http:
        raise RuntimeError(f"Listening on {host} without TLS would send API tokens in the clear. Give --certfile and "
                           "--keyfile, or --allow-http if a proxy in front of it does TLS.")
    if bool(certfile) != bool(keyfile):
        raise RuntimeError("Give both --certfile and --keyfile.")
    url, fp = upstream()
    app = http_app(url, fp, host, path, allowed_hosts)
    scheme = "https" if certfile else "http"
    print(f"Branchwarden MCP over HTTP at {scheme}://{host}:{port}{path}, for {url}. Clients send their own API "
          "token: Authorization: Bearer bwt_…", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, ssl_certfile=certfile, ssl_keyfile=keyfile, log_level="warning",
                server_header=False)


__all__ = ["build", "run", "run_http", "http_app", "__version__"]
