"""
repohealth.py
~~~~~~~~~~~~~
The repository settings behind most branch and review problems, flagged per
repo (Branchwarden never changes them):

- head branches not deleted after merge ("Automatically delete head
  branches" off), the main reason branches are left behind,
- the default branch not protected,
- merging without an approving review,
- no CODEOWNERS file, so nobody is suggested or asked automatically.

"Delete head branches" and classic branch protection come with the repo list
(GraphQL). Whether the default branch is protected, and its rulesets, take two
REST calls, read at most once a day. GitHub may hide classic protection's
details from tokens without admin rights, so "no required reviews" is only
flagged when the requirement can actually be seen.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from psycopg2.extras import Json

import db
from github import Client, GitHubError

log = logging.getLogger("branchwarden.repohealth")

EVERY = timedelta(hours=24)

FLAGS = {
    # key: (title, why it matters)
    "auto_delete": ("Head branches aren't deleted after merge",
                    "Turn on “Automatically delete head branches”: it's the main reason branches are left behind."),
    "unprotected": ("The default branch isn't protected",
                    "Anyone with write access can push to it directly or force-push over it."),
    "no_reviews": ("Merging doesn't need an approving review",
                   "Protect the default branch with a rule that requires at least one approval."),
    "no_codeowners": ("No CODEOWNERS file",
                      "Without one, nobody is suggested or asked to review by the files a PR changes."),
}


def read(client: Client, full_name: str, branch: str) -> dict:
    """{protected, reviews (int, or None when it can't be seen), rules [types]}."""
    b = client.rest("GET", f"/repos/{full_name}/branches/{quote(branch, safe='')}").json()
    out = {"protected": bool(b.get("protected")), "reviews": None, "rules": []}
    try:
        rules = client.rest("GET", f"/repos/{full_name}/rules/branches/{quote(branch, safe='')}").json()
    except GitHubError as exc:
        if exc.status not in (403, 404):   # rulesets aren't available on free private repos
            raise
        rules = []
    for r in rules if isinstance(rules, list) else []:
        out["rules"].append(r.get("type"))
        if r.get("type") == "pull_request":
            n = int((r.get("parameters") or {}).get("required_approving_review_count") or 0)
            out["reviews"] = max(out["reviews"] or 0, n)
    if out["rules"]:
        out["protected"] = True
        if out["reviews"] is None:
            out["reviews"] = 0 if not b.get("protected") else None
    return out


def refresh(conn, client: Client, repo: dict, now: Optional[datetime] = None) -> None:
    """Read the default branch's protection and rulesets if it's been a day."""
    now = now or datetime.now(timezone.utc)
    branch = repo.get("default_branch")
    if not branch or (repo.get("health_at") and now - repo["health_at"] < EVERY):
        return
    try:
        found = read(client, repo["full_name"], branch)
    except GitHubError as exc:
        log.info("%s: repo health not read: %s", repo["full_name"], exc)
        return
    db.execute(conn, "UPDATE repos SET health = %s, health_at = %s WHERE id = %s", (Json(found), now, repo["id"]))


def flags(repo: dict) -> list[dict]:
    """What's wrong with a repo's settings, each with a link to fix it on GitHub."""
    url = (repo.get("url") or "").rstrip("/")
    branch = repo.get("default_branch") or "main"
    health = repo.get("health") or {}
    classic = repo.get("classic_protection") or {}
    out = []

    def add(key: str, link: str, extra: str = ""):
        title, why = FLAGS[key]
        out.append({"key": key, "title": title, "why": why + extra, "link": link})

    if repo.get("delete_branch_on_merge") is False:
        add("auto_delete", f"{url}/settings")
    if health:
        protected = health.get("protected") or bool(classic)
        if not protected:
            add("unprotected", f"{url}/settings/branches", "")
        else:
            reviews = health.get("reviews")
            if classic.get("requiresApprovingReviews") is not None:
                reviews = max(reviews or 0, int(classic.get("requiredApprovingReviewCount") or 0)
                              if classic.get("requiresApprovingReviews") else 0)
            if reviews == 0:
                add("no_reviews", f"{url}/settings/rules")
    if repo.get("scanned_once") and not repo.get("codeowners"):
        add("no_codeowners", f"{url}/new/{quote(branch)}?filename=.github/CODEOWNERS")
    return out
