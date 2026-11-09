"""
scanner.py
~~~~~~~~~~
Reads each account's repositories, open pull requests and branches from
GitHub's GraphQL API and stores them. Comparing a scan with the one before
gives the events the notifiers announce: a PR opened or marked ready, a review
submitted, a PR merged or closed. A repo's first scan only records what's
there, so adding an organisation doesn't flood the channels.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from psycopg2.extras import Json

import analysis
import db
import settings
from github import Client, Connections, GitHubError

log = logging.getLogger("branchwarden.scanner")

RATE = "rateLimit { remaining limit resetAt }"

REPO_FIELDS = """
  pageInfo { hasNextPage endCursor }
  nodes { name nameWithOwner url isArchived isFork isPrivate owner { login } defaultBranchRef { name } }
"""

ORG_REPOS = f"""
query($login: String!, $after: String) {{
  organization(login: $login) {{ repositories(first: 100, after: $after) {{ {REPO_FIELDS} }} }}
  {RATE}
}}"""

USER_REPOS = f"""
query($login: String!, $after: String) {{
  user(login: $login) {{
    repositories(first: 100, after: $after, ownerAffiliations: [OWNER]) {{ {REPO_FIELDS} }}
  }}
  {RATE}
}}"""

ONE_REPO = f"""
query($owner: String!, $name: String!) {{
  repository(owner: $owner, name: $name) {{
    name nameWithOwner url isArchived isFork isPrivate owner {{ login }} defaultBranchRef {{ name }}
  }}
  {RATE}
}}"""

PR_FIELDS = """
  number title url isDraft state createdAt updatedAt mergedAt closedAt
  author { login } mergedBy { login }
  headRefName headRefOid baseRefName reviewDecision mergeable additions deletions changedFiles
  labels(first: 20) { nodes { name } }
  files(first: 100) { nodes { path } }
  reviewRequests(first: 20) { nodes { requestedReviewer {
    __typename ... on User { login } ... on Team { combinedSlug } ... on Bot { login } ... on Mannequin { login } } } }
  latestReviews(first: 30) { nodes { author { login } state submittedAt } }
  firstReviews: reviews(first: 10, states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED]) { nodes { submittedAt author { login } } }
  commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
  timelineItems(last: 20, itemTypes: [READY_FOR_REVIEW_EVENT, REVIEW_REQUESTED_EVENT]) { nodes {
    __typename ... on ReadyForReviewEvent { createdAt } ... on ReviewRequestedEvent { createdAt } } }
"""

OPEN_PRS = f"""
query($owner: String!, $name: String!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    co1: object(expression: "HEAD:.github/CODEOWNERS") {{ ... on Blob {{ text }} }}
    co2: object(expression: "HEAD:CODEOWNERS") {{ ... on Blob {{ text }} }}
    co3: object(expression: "HEAD:docs/CODEOWNERS") {{ ... on Blob {{ text }} }}
    pullRequests(states: OPEN, first: 25, after: $after, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{ {PR_FIELDS} }}
    }}
  }}
  {RATE}
}}"""


def _branches_query(compare: bool) -> str:
    cmp = "compare(headRef: $base) { aheadBy behindBy }" if compare else ""
    return f"""
query($owner: String!, $name: String!, $after: String{', $base: String!' if compare else ''}) {{
  repository(owner: $owner, name: $name) {{
    refs(refPrefix: "refs/heads/", first: 50, after: $after) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        name
        branchProtectionRule {{ pattern }}
        target {{ ... on Commit {{ oid committedDate author {{ name user {{ login }} }} }} }}
        {cmp}
        associatedPullRequests(first: 1, orderBy: {{field: CREATED_AT, direction: DESC}}) {{
          nodes {{ number state title headRefOid mergedAt closedAt mergedBy {{ login }} }} }}
      }}
    }}
  }}
  {RATE}
}}"""


def _ended_query(numbers: list[int]) -> str:
    fr = "firstReviews: reviews(first: 10, states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED]) " \
         "{ nodes { submittedAt author { login } } } author { login }"
    parts = " ".join(f"p{n}: pullRequest(number: {int(n)}) {{ state mergedAt closedAt mergedBy {{ login }} {fr} }}"
                     for n in numbers)
    return f"query($owner: String!, $name: String!) {{ repository(owner: $owner, name: $name) {{ {parts} }} {RATE} }}"


# ─── Turning GraphQL nodes into rows ────────────────────────────────────────

def _login(obj) -> Optional[str]:
    return (obj or {}).get("login", "").lower() or None


def first_review(node: dict) -> Optional[str]:
    """When someone other than the author first reviewed (authors' replies count as reviews on GitHub)."""
    author = _login(node.get("author"))
    times = [r["submittedAt"] for r in (node.get("firstReviews") or {}).get("nodes") or []
             if r and r.get("submittedAt") and _login(r.get("author")) != author]
    return min(times) if times else None


def pr_from_node(node: dict) -> dict:
    """A PR as stored, from its GraphQL node."""
    requested = []
    for rr in (node.get("reviewRequests") or {}).get("nodes") or []:
        who = (rr or {}).get("requestedReviewer") or {}
        if who.get("combinedSlug"):
            requested.append({"team": who["combinedSlug"].lower()})
        elif who.get("login"):
            requested.append({"user": who["login"].lower()})
    reviews = []
    for r in (node.get("latestReviews") or {}).get("nodes") or []:
        if r and r.get("author") and r.get("submittedAt"):
            reviews.append({"login": r["author"]["login"].lower(), "state": r["state"], "at": r["submittedAt"]})
    commit = (((node.get("commits") or {}).get("nodes") or [{}])[-1] or {}).get("commit") or {}
    ready_at = requested_at = None
    for t in (node.get("timelineItems") or {}).get("nodes") or []:
        if not t:
            continue
        if t.get("__typename") == "ReadyForReviewEvent":
            ready_at = max(ready_at or t["createdAt"], t["createdAt"])
        elif t.get("__typename") == "ReviewRequestedEvent":
            requested_at = max(requested_at or t["createdAt"], t["createdAt"])
    return {
        "number": node["number"], "title": node["title"], "url": node["url"],
        "author": _login(node.get("author")),
        "state": {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}.get(node.get("state"), "open"),
        "draft": bool(node.get("isDraft")),
        "head_ref": node.get("headRefName"), "head_oid": node.get("headRefOid"),
        "base_ref": node.get("baseRefName"),
        "review_decision": node.get("reviewDecision"), "mergeable": node.get("mergeable"),
        "ci_state": (commit.get("statusCheckRollup") or {}).get("state"),
        "additions": node.get("additions") or 0, "deletions": node.get("deletions") or 0,
        "changed_files": node.get("changedFiles") or 0,
        "files": [f["path"] for f in (node.get("files") or {}).get("nodes") or [] if f],
        "labels": [lbl["name"] for lbl in (node.get("labels") or {}).get("nodes") or [] if lbl],
        "requested": requested, "reviews": reviews,
        "created_at": node["createdAt"], "updated_at": node.get("updatedAt"),
        "ready_at": ready_at, "review_requested_at": requested_at,
        "last_commit_at": commit.get("committedDate"),
        "merged_at": node.get("mergedAt"), "merged_by": _login(node.get("mergedBy")),
        "closed_at": node.get("closedAt"),
        "first_review_at": first_review(node),
    }


def branch_from_node(node: dict) -> dict:
    target = node.get("target") or {}
    author = target.get("author") or {}
    pr = (((node.get("associatedPullRequests") or {}).get("nodes")) or [None])[0] or {}
    cmp = node.get("compare") or {}
    return {
        "name": node["name"], "head_oid": target.get("oid"),
        "last_commit_at": target.get("committedDate"),
        "last_committer": _login(author.get("user")) or author.get("name"),
        "protected": bool(node.get("branchProtectionRule")),
        # compare() runs with the branch as base and the default branch as head.
        "ahead": cmp.get("behindBy"), "behind": cmp.get("aheadBy"),
        "pr_number": pr.get("number"), "pr_state": pr.get("state"), "pr_title": pr.get("title"),
        "pr_head_oid": pr.get("headRefOid"), "pr_merged_at": pr.get("mergedAt"),
        "pr_closed_at": pr.get("closedAt"), "pr_merged_by": _login(pr.get("mergedBy")),
    }


def review_events(old_reviews: list[dict], new_reviews: list[dict]) -> list[dict]:
    """Approvals and change requests that weren't there last time."""
    seen = {(r["login"], r["state"], r["at"]) for r in old_reviews or []}
    return [r for r in new_reviews if r["state"] in ("APPROVED", "CHANGES_REQUESTED")
            and (r["login"], r["state"], r["at"]) not in seen]


# ─── Reading from GitHub ────────────────────────────────────────────────────

def _pages(client: Client, query: str, variables: dict, path: list[str]) -> list[dict]:
    nodes: list[dict] = []
    after = None
    while True:
        data = client.graphql(query, {**variables, "after": after})
        conn = data
        for key in path:
            conn = (conn or {}).get(key)
        if conn is None:
            raise GitHubError(f"{variables.get('login') or variables.get('name')}: not found or no access")
        nodes.extend(n for n in conn["nodes"] if n)
        if not conn["pageInfo"]["hasNextPage"]:
            return nodes
        after = conn["pageInfo"]["endCursor"]


def list_repos(client: Client, target: dict) -> list[dict]:
    if target["kind"] == "org":
        return _pages(client, ORG_REPOS, {"login": target["login"]}, ["organization", "repositories"])
    return _pages(client, USER_REPOS, {"login": target["login"]}, ["user", "repositories"])


def fetch_open_prs(client: Client, owner: str, name: str) -> tuple[list[dict], str]:
    prs: list[dict] = []
    codeowners = ""
    after = None
    while True:
        data = client.graphql(OPEN_PRS, {"owner": owner, "name": name, "after": after})
        repo = data.get("repository")
        if repo is None:
            raise GitHubError(f"{owner}/{name}: not found or no access")
        if after is None:
            for k in ("co1", "co2", "co3"):
                if (repo.get(k) or {}).get("text"):
                    codeowners = repo[k]["text"]
                    break
        conn = repo["pullRequests"]
        prs.extend(pr_from_node(n) for n in conn["nodes"] if n)
        if not conn["pageInfo"]["hasNextPage"]:
            return prs, codeowners
        after = conn["pageInfo"]["endCursor"]


def fetch_branches(client: Client, owner: str, name: str, default_branch: Optional[str]) -> list[dict]:
    if default_branch:
        try:
            nodes = _pages(client, _branches_query(True), {"owner": owner, "name": name, "base": default_branch},
                           ["repository", "refs"])
            return [branch_from_node(n) for n in nodes]
        except GitHubError as exc:
            if exc.status:      # a real HTTP failure: don't hide it
                raise
            log.info("%s/%s: branch comparison failed (%s); reading branches without it", owner, name, exc)
    nodes = _pages(client, _branches_query(False), {"owner": owner, "name": name}, ["repository", "refs"])
    return [branch_from_node(n) for n in nodes]


def fetch_ended(client: Client, owner: str, name: str, numbers: list[int]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for i in range(0, len(numbers), 50):
        chunk = numbers[i:i + 50]
        data = client.graphql(_ended_query(chunk), {"owner": owner, "name": name})
        repo = data.get("repository") or {}
        for n in chunk:
            if repo.get(f"p{n}"):
                out[n] = repo[f"p{n}"]
    return out


# ─── Storing ────────────────────────────────────────────────────────────────

PR_COLUMNS = ("title", "url", "author", "state", "draft", "head_ref", "head_oid", "base_ref",
              "review_decision", "mergeable", "ci_state", "additions", "deletions", "changed_files",
              "files", "labels", "requested", "reviews", "created_at", "updated_at", "ready_at",
              "review_requested_at", "last_commit_at", "merged_at", "merged_by", "closed_at", "first_review_at")
JSON_COLUMNS = {"files", "labels", "requested", "reviews"}

BRANCH_COLUMNS = ("head_oid", "last_commit_at", "last_committer", "protected", "ahead", "behind",
                  "pr_number", "pr_state", "pr_title", "pr_head_oid", "pr_merged_at", "pr_closed_at",
                  "pr_merged_by")


def _upsert_pr(conn, repo_id: int, pr: dict) -> int:
    values = [Json(pr[c]) if c in JSON_COLUMNS else pr[c] for c in PR_COLUMNS]
    cols = ", ".join(PR_COLUMNS)
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in PR_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO pull_requests (repo_id, number, {cols}, seen_at)
            VALUES (%s, %s, {', '.join(['%s'] * len(PR_COLUMNS))}, NOW())
            ON CONFLICT (repo_id, number) DO UPDATE SET {sets}, seen_at = NOW()
            RETURNING id""", [repo_id, pr["number"], *values])
        return cur.fetchone()[0]


def _store_branches(conn, repo_id: int, branches: list[dict]) -> None:
    cols = ", ".join(BRANCH_COLUMNS)
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in BRANCH_COLUMNS)
    with conn.cursor() as cur:
        for b in branches:
            cur.execute(f"""
                INSERT INTO branches (repo_id, name, {cols}, seen_at)
                VALUES (%s, %s, {', '.join(['%s'] * len(BRANCH_COLUMNS))}, NOW())
                ON CONFLICT (repo_id, name) DO UPDATE SET {sets}, seen_at = NOW()
            """, [repo_id, b["name"], *[b[c] for c in BRANCH_COLUMNS]])
        cur.execute("DELETE FROM branches WHERE repo_id = %s AND NOT (name = ANY(%s))",
                    (repo_id, [b["name"] for b in branches]))
        cur.execute("DELETE FROM ignored_branches WHERE repo_id = %s AND NOT (name = ANY(%s))",
                    (repo_id, [b["name"] for b in branches]))


def scan_repo(conn, client: Client, repo: dict) -> tuple[list[dict], int, int]:
    """Scan one repo; returns (events, open PRs, branches)."""
    owner, name = repo["owner"], repo["name"]
    first = not repo["scanned_once"]
    prs, codeowners = fetch_open_prs(client, owner, name)
    branches = fetch_branches(client, owner, name, repo["default_branch"])

    before = {r["number"]: r for r in db.rows(
        conn, "SELECT id, number, draft, reviews FROM pull_requests WHERE repo_id = %s AND state = 'open'",
        (repo["id"],))}
    events: list[dict] = []
    for pr in prs:
        pr_id = _upsert_pr(conn, repo["id"], pr)
        old = before.get(pr["number"])
        if first:
            continue
        if old is None:
            if not pr["draft"]:
                events.append({"kind": "opened", "pr_id": pr_id})
        else:
            if old["draft"] and not pr["draft"]:
                events.append({"kind": "ready", "pr_id": pr_id})
            for r in review_events(old["reviews"], pr["reviews"]):
                events.append({"kind": "review", "pr_id": pr_id, "review": r})

    gone = [n for n in before if n not in {p["number"] for p in prs}]
    if gone:
        ended = fetch_ended(client, owner, name, gone)
        for n in gone:
            info = ended.get(n) or {"state": "CLOSED"}
            state = {"MERGED": "merged", "OPEN": "open"}.get(info.get("state"), "closed")
            if state == "open":      # still open: outside the page we read, leave it be
                continue
            db.execute(conn, """UPDATE pull_requests SET state = %s, merged_at = %s, merged_by = %s,
                                       closed_at = %s, first_review_at = COALESCE(first_review_at, %s),
                                       seen_at = NOW() WHERE id = %s""",
                       (state, info.get("mergedAt"), _login(info.get("mergedBy")),
                        info.get("closedAt") or datetime.now(timezone.utc), first_review(info), before[n]["id"]))
            if not first:
                events.append({"kind": state, "pr_id": before[n]["id"]})

    _store_branches(conn, repo["id"], branches)
    db.execute(conn, """UPDATE repos SET codeowners = %s, scanned_once = TRUE, last_scan_at = NOW(),
                               last_error = NULL WHERE id = %s""", (codeowners or None, repo["id"]))
    return events, len(prs), len(branches)


def _store_repo(conn, target: dict, node: dict, values: dict) -> dict:
    full = node["nameWithOwner"].lower()
    included = analysis.repo_included(
        full, archived=node["isArchived"], fork=node["isFork"],
        include=settings.split_list(settings.get(values, "REPO_INCLUDE")),
        exclude=settings.split_list(settings.get(values, "REPO_EXCLUDE")),
        with_archived=settings.flag(values, "INCLUDE_ARCHIVED"),
        with_forks=settings.flag(values, "INCLUDE_FORKS"))
    return db.one(conn, """
        INSERT INTO repos (target_id, owner, name, full_name, url, default_branch, archived, fork, private, included)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (full_name) DO UPDATE SET target_id = EXCLUDED.target_id, owner = EXCLUDED.owner,
            name = EXCLUDED.name, url = EXCLUDED.url, default_branch = EXCLUDED.default_branch,
            archived = EXCLUDED.archived, fork = EXCLUDED.fork, private = EXCLUDED.private,
            included = EXCLUDED.included
        RETURNING *""", (target["id"], node["owner"]["login"], node["name"], full, node["url"],
                         (node.get("defaultBranchRef") or {}).get("name"), node["isArchived"],
                         node["isFork"], node["isPrivate"], included))


class ScanResult:
    def __init__(self):
        self.events: list[dict] = []
        self.repos = 0
        self.prs = 0
        self.branches = 0
        self.errors: list[str] = []
        self.rate = None


def scan_target(conn, conns: Connections, target: dict, values: dict, result: ScanResult,
                only: Optional[set] = None) -> None:
    """Scan one account: all its repos, or just those named in `only`."""
    try:
        client = conns.client_for(target["login"], target["via"])
    except GitHubError as exc:
        db.execute(conn, "UPDATE targets SET last_error = %s, last_scan_at = NOW() WHERE id = %s",
                   (str(exc), target["id"]))
        result.errors.append(f"{target['login']}: {exc}")
        conn.commit()
        return
    try:
        if only is None:
            nodes = list_repos(client, target)
            repos = [_store_repo(conn, target, n, values) for n in nodes]
            db.execute(conn, "DELETE FROM repos WHERE target_id = %s AND NOT (full_name = ANY(%s))",
                       (target["id"], [r["full_name"] for r in repos]))
        else:
            repos = []
            for full in sorted(only):
                owner, _, name = full.partition("/")
                data = client.graphql(ONE_REPO, {"owner": owner, "name": name})
                if data.get("repository"):
                    repos.append(_store_repo(conn, target, data["repository"], values))
        conn.commit()
    except GitHubError as exc:
        conn.rollback()
        db.execute(conn, "UPDATE targets SET last_error = %s, last_scan_at = NOW(), used = %s WHERE id = %s",
                   (str(exc), client.label, target["id"]))
        result.errors.append(f"{target['login']}: {exc}")
        conn.commit()
        return

    for repo in repos:
        if not repo["included"]:
            continue
        try:
            events, n_prs, n_branches = scan_repo(conn, client, repo)
            conn.commit()
            result.events.extend(events)
            result.repos += 1
            result.prs += n_prs
            result.branches += n_branches
        except GitHubError as exc:
            conn.rollback()
            db.execute(conn, "UPDATE repos SET last_error = %s, last_scan_at = NOW() WHERE id = %s",
                       (str(exc), repo["id"]))
            conn.commit()
            result.errors.append(f"{repo['full_name']}: {exc}")
    result.rate = client.rate
    if client.label == "token" and client.token_expires:
        settings.save(conn, {"GITHUB_TOKEN_EXPIRES": client.token_expires}, "Branchwarden", record=False)
    if only is None:
        counts = db.one(conn, """SELECT COUNT(*) FILTER (WHERE included) AS inc,
                                        COUNT(*) FILTER (WHERE NOT included) AS skip
                                   FROM repos WHERE target_id = %s""", (target["id"],))
        db.execute(conn, """UPDATE targets SET repo_count = %s, skipped_count = %s, used = %s,
                                   last_scan_at = NOW(), last_error = NULL WHERE id = %s""",
                   (counts["inc"], counts["skip"], client.label, target["id"]))
    conn.commit()
