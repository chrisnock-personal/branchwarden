"""
data.py
~~~~~~~
Reading PRs and branches back out of the database, with their state worked
out (see analysis.py). Shared by the dashboard, the reminders and the digest.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import analysis
import db
import settings


def open_prs(conn, *, repo_id: Optional[int] = None, pr_id: Optional[int] = None,
             now: Optional[datetime] = None) -> list[dict]:
    """Open PRs in included repos, each with its state, filters and waiting time."""
    now = now or datetime.now(timezone.utc)
    where = ["p.state = 'open'", "r.included"]
    params: list = []
    if repo_id:
        where.append("r.id = %s")
        params.append(repo_id)
    if pr_id:
        where.append("p.id = %s")
        params.append(pr_id)
    found = db.rows(conn, f"""
        SELECT p.*, r.full_name, r.name AS repo_name, r.owner AS repo_owner, r.url AS repo_url,
               r.codeowners, r.default_branch, t.login AS target_login, t.kind AS target_kind, t.via AS target_via
          FROM pull_requests p JOIN repos r ON r.id = p.repo_id JOIN targets t ON t.id = r.target_id
         WHERE {' AND '.join(where)}
         ORDER BY p.created_at""", params)
    for pr in found:
        enrich_pr(pr, now)
    return found


def enrich_pr(pr: dict, now: datetime) -> dict:
    state = analysis.pr_state(pr)
    pr["state_key"] = state
    pr["state_label"], pr["tone"] = analysis.STATES[state]
    pr["cats"] = analysis.pr_categories(pr)
    pr["since"] = analysis.state_since(pr)
    pr["waiting"] = analysis.ago(pr["since"], now)
    pr["waiting_seconds"] = (now - pr["since"]).total_seconds()
    pr["waiting_on"] = analysis.waiting_on(pr)
    pr["ci_label"], pr["ci_tone"] = analysis.CI_LABELS.get(pr.get("ci_state") or "", ("none", "muted"))
    pr["requested_users"] = analysis.requested_users(pr)
    pr["requested_teams"] = analysis.requested_teams(pr)
    pr["ref"] = f"{pr['repo_name']} #{pr['number']}"
    return pr


def reviewer_marks(pr: dict) -> list[dict]:
    """Small avatars: each reviewer with what they did (approved, changes or pending)."""
    marks = []
    seen = set()
    for r in sorted(pr.get("reviews") or [], key=lambda r: r["at"], reverse=True):
        if r["login"] in seen or r["state"] not in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
            continue
        seen.add(r["login"])
        state = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes"}.get(r["state"], "commented")
        verb = {"approved": "Approved", "changes": "Changes requested", "commented": "Commented"}[state]
        marks.append({"login": r["login"], "s": state, "title": f"{verb}: {r['login']}", "team": False})
    for login in analysis.requested_users(pr):
        if login not in seen:
            marks.append({"login": login, "s": "pending", "title": f"Requested: {login}", "team": False})
    for team in analysis.requested_teams(pr):
        marks.append({"login": team, "s": "pending", "title": f"Requested: team {team}", "team": True})
    for m in marks:
        name = m["login"].split("/")[-1]
        parts = [p for p in name.replace("_", "-").split("-") if p]
        m["initials"] = ((parts[0][0] + parts[1][0]) if len(parts) > 1 else name[:2]).upper()
    return marks


def classified_branches(conn, values: dict, *, now: Optional[datetime] = None,
                        repo_ids: Optional[list[int]] = None) -> list[dict]:
    """Every flagged branch in included repos, with its status."""
    now = now or datetime.now(timezone.utc)
    stale_days = settings.number(values, "STALE_DAYS", 30)
    never = settings.split_list(settings.get(values, "NEVER_FLAG"))
    params: list = []
    extra = ""
    if repo_ids is not None:
        extra = "AND r.id = ANY(%s)"
        params.append(repo_ids)
    found = db.rows(conn, f"""
        SELECT b.*, r.full_name, r.name AS repo_name, r.url AS repo_url, r.default_branch,
               (i.name IS NOT NULL) AS ignored
          FROM branches b JOIN repos r ON r.id = b.repo_id
          LEFT JOIN ignored_branches i ON i.repo_id = b.repo_id AND i.name = b.name
         WHERE r.included {extra}
         ORDER BY b.last_commit_at NULLS FIRST""", params)
    out = []
    for b in found:
        status = analysis.branch_status(b, default_branch=b["default_branch"], never_flag=never,
                                        ignored=b["ignored"], stale_days=stale_days, now=now)
        if status is None:
            continue
        b["status"] = status
        b["status_label"], b["tone"] = analysis.BRANCH_STATUS[status]
        b["age"] = analysis.ago(b["last_commit_at"], now)
        b["age_days"] = int((now - b["last_commit_at"]).total_seconds() // 86400) if b["last_commit_at"] else None
        b["url"] = f"{b['repo_url']}/tree/{b['name']}" if b.get("repo_url") else None
        if status == "stale":
            b["status_label"] = f"Stale · {b['age_days']} d"
            b["tone"] = "bad" if b["age_days"] and b["age_days"] >= 3 * stale_days else "warn"
        out.append(b)
    return out


def ignored_count(conn) -> int:
    return db.one(conn, "SELECT COUNT(*) AS n FROM ignored_branches")["n"]
