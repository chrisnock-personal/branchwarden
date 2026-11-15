"""
repos.py
~~~~~~~~
The Repositories pages: every repo with its counts and health flags, and one
page per repo bringing together its pull requests, branches, metrics,
workflows, reviewers, CODEOWNERS, health and the channels it's routed to.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import analysis
import data
import db
import metrics
import repohealth
import settings
import workflows

LEFT = ("left_merged", "left_closed", "pushed_after_merge")


def overview(conn, values: dict, now: Optional[datetime] = None) -> list[dict]:
    """Every included repo with its open PRs, branches to tidy, failing
    workflows and health flags; repos with something to look at first."""
    now = now or datetime.now(timezone.utc)
    prs = data.open_prs(conn, now=now)
    branches = data.classified_branches(conn, values, now=now)
    # Workflows whose latest passed-or-failed run on the default branch (or on its schedule) failed.
    failing = {r["repo_id"]: r["n"] for r in db.rows(conn, """
        SELECT w.repo_id, COUNT(*) AS n
          FROM workflows w JOIN repos r ON r.id = w.repo_id
         CROSS JOIN LATERAL (
               SELECT y.conclusion FROM workflow_runs y
                WHERE y.workflow_id = w.id AND y.status = 'completed'
                  AND y.conclusion IN ('success', 'neutral', 'failure', 'timed_out', 'startup_failure')
                  AND (y.event = 'schedule' OR y.head_branch = r.default_branch)
                ORDER BY y.created_at DESC LIMIT 1) last
         WHERE w.state = 'active' AND last.conclusion IN ('failure', 'timed_out', 'startup_failure')
         GROUP BY 1""")}
    out = []
    for r in db.rows(conn, "SELECT * FROM repos WHERE included ORDER BY name"):
        mine = [p for p in prs if p["repo_id"] == r["id"]]
        rb = [b for b in branches if b["repo_id"] == r["id"]]
        flags = repohealth.flags(r)
        out.append({
            "id": r["id"], "name": r["name"], "full_name": r["full_name"], "url": r["url"],
            "private": r["private"], "open": len(mine),
            "review": sum(1 for p in mine if "review" in p["cats"] and not p["draft"]),
            "ci": sum(1 for p in mine if "ci" in p["cats"]),
            "stale": sum(1 for b in rb if b["status"] == "stale"),
            "left": sum(1 for b in rb if b["status"] in LEFT),
            "failing": failing.get(r["id"], 0), "flags": flags,
            "error": r["last_error"], "scanned": r["last_scan_at"],
        })
    out.sort(key=lambda x: (-(len(x["flags"]) + x["failing"] + (1 if x["stale"] or x["left"] else 0)), x["name"]))
    return out


def reviewers(conn, repo_id: int, requested: Counter, now: datetime, days: int = 90) -> list[dict]:
    """Who reviews here: PRs reviewed for others in the last `days`, and their
    open review requests in this repo now."""
    done: Counter = Counter()
    for r in db.rows(conn, """
            SELECT p.author, x->>'login' AS login
              FROM pull_requests p, jsonb_array_elements(p.reviews) x
             WHERE p.repo_id = %s AND (x->>'at')::timestamptz >= %s""", (repo_id, now - timedelta(days=days))):
        if r["login"] and r["login"] != r["author"] and not r["login"].endswith("[bot]"):
            done[r["login"]] += 1
    people = set(done) | set(requested)
    return sorted(({"login": p, "reviewed": done.get(p, 0), "waiting": requested.get(p, 0)} for p in people),
                  key=lambda x: (-x["reviewed"], -x["waiting"], x["login"]))[:10]


def codeowners_rules(text: Optional[str]) -> list[dict]:
    """The CODEOWNERS file's rules, as written (comments and blanks left out)."""
    out = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        out.append({"pattern": parts[0], "owners": [o for o in parts[1:] if o.startswith("@")]})
    return out


def routing(conn, full_name: str) -> list[dict]:
    """Channels this repo's messages go to: matched by its name, sent everything,
    or only for PRs that ask one of the channel's teams to review."""
    out = []
    for c in db.rows(conn, "SELECT * FROM channels ORDER BY id"):
        repos = settings.split_list(c["repos"])
        teams = settings.split_list(c["teams"])
        if not repos and not teams:
            how = "everything"
        elif repos and analysis.matches(full_name, repos):
            how = "this repo, by name"
        elif teams:
            how = "PRs asking " + ", ".join(teams) + " to review"
        else:
            continue
        out.append({"name": c["name"], "kind": "Teams" if c["kind"] == "teams" else "Slack",
                    "enabled": c["enabled"], "how": how})
    return out


def detail(conn, values: dict, full_name: str, days: int = 90, now: Optional[datetime] = None) -> Optional[dict]:
    now = now or datetime.now(timezone.utc)
    repo = db.one(conn, "SELECT * FROM repos WHERE full_name = %s AND included", (full_name.lower(),))
    if not repo:
        return None
    prs = data.open_prs(conn, repo_id=repo["id"], now=now)
    for p in prs:
        p["marks"] = data.reviewer_marks(p)
    prs.sort(key=lambda p: (p["draft"], -p["waiting_seconds"]))
    branches = data.classified_branches(conn, values, now=now, repo_ids=[repo["id"]])
    stale = sorted((b for b in branches if b["status"] == "stale"), key=lambda b: -(b["age_days"] or 0))
    left = [b for b in branches if b["status"] in LEFT]
    requested = Counter(u for p in prs if not p["draft"] for u in p["requested_users"])
    m = metrics.compute(conn, days, repo["full_name"], now)
    wf = workflows.load(conn, values, 14, repo["full_name"], now)
    flags = repohealth.flags(repo)
    tiles = [
        {"label": "Open pull requests", "value": len(prs),
         "note": f"{sum(1 for p in prs if 'review' in p['cats'] and not p['draft'])} need review"
                 f" · {sum(1 for p in prs if p['draft'])} draft{'s' if sum(1 for p in prs if p['draft']) != 1 else ''}",
         "tone": ""},
        {"label": "Stale branches", "value": len(stale), "tone": "tone-warn" if stale else "",
         "note": f"no PR, idle over {settings.number(values, 'STALE_DAYS', 30)} days"},
        {"label": "Left after a PR", "value": len(left), "tone": "tone-warn" if left else "",
         "note": "PR merged or closed"},
        {"label": "Health", "value": len(flags) or "OK", "tone": "tone-warn" if flags else "tone-good",
         "note": "settings to look at" if flags else "nothing to flag"},
    ]
    return {"repo": repo, "prs": prs, "stale": stale, "left": left, "tiles": tiles, "metrics": m,
            "workflows": wf, "flags": flags, "health_read": bool(repo.get("health")),
            "reviewers": reviewers(conn, repo["id"], requested, now), "codeowners": codeowners_rules(repo["codeowners"]),
            "routing": routing(conn, repo["full_name"]), "days": days}
