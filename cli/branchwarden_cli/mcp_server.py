"""
branchwarden mcp: Branchwarden's API as tools for AI agents, over MCP (stdio).

It signs in the same way as the CLI (the keychain, or BRANCHWARDEN_URL and
BRANCHWARDEN_TOKEN, with a pinned certificate) and only talks to the API, so the
token's role and scope decide what works: give an agent a read-only token and it
can look but not change anything.

Tools that only read are marked read-only. Requesting reviewers writes to
GitHub: it's marked as reaching outside, and it's a dry run unless the agent
asks otherwise (dry_run=false), so nothing is sent by accident.
"""

from __future__ import annotations

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


def _client():
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


__all__ = ["build", "run", "__version__"]
