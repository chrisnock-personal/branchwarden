"""
metrics.py
~~~~~~~~~~
How reviews are going, from the PRs Branchwarden has seen: time to first
review, time to merge, merges per week, and trends from the daily snapshot.

Times count from when a PR was ready for review (or opened, if it never was a
draft), in wall-clock hours.
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import db

PERIODS = ((30, "30 days"), (90, "90 days"), (180, "180 days"))


def duration(hours: Optional[float]) -> str:
    if hours is None:
        return "—"
    if hours < 1:
        return f"{max(1, round(hours * 60))} min"
    if hours < 48:
        return f"{hours:.1f} h" if hours < 10 else f"{round(hours)} h"
    return f"{hours / 24:.1f} days"


def _median(values: list[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def _hours(a, b) -> float:
    return max(0.0, (b - a).total_seconds() / 3600)


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def bars(points: list[tuple[str, Optional[float], str]]) -> list[dict]:
    """Bar heights as percentages of the tallest; (label, value, tooltip)."""
    peak = max([v for _, v, _ in points if v] + [1e-9])
    return [{"label": label, "value": v, "tip": tip,
             "height": 0 if not v else max(3, round(100 * v / peak))} for label, v, tip in points]


def compute(conn, days: int, repo: str = "", now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    params: list = [since, since]
    where = ""
    if repo:
        where = "AND r.full_name = %s"
        params.append(repo)
    prs = db.rows(conn, f"""
        SELECT p.id, p.state, p.created_at, p.ready_at, p.first_review_at, p.merged_at, p.closed_at,
               p.repo_id, r.name AS repo_name, r.full_name
          FROM pull_requests p JOIN repos r ON r.id = p.repo_id
         WHERE r.included AND (p.merged_at >= %s OR p.first_review_at >= %s) {where}""", params)

    def start(p):
        return max(p["created_at"], p["ready_at"]) if p["ready_at"] else p["created_at"]

    first = [(p, _hours(start(p), p["first_review_at"])) for p in prs
             if p["first_review_at"] and p["first_review_at"] >= since]
    merged = [(p, _hours(start(p), p["merged_at"])) for p in prs if p["merged_at"] and p["merged_at"] >= since]

    tiles = [
        {"label": "Median time to first review", "value": duration(_median([h for _, h in first])),
         "note": f"{len(first)} PR{'s' if len(first) != 1 else ''} reviewed"},
        {"label": "Reviewed within a day", "note": "share of first reviews",
         "value": f"{round(100 * sum(1 for _, h in first if h <= 24) / len(first))}%" if first else "—"},
        {"label": "Median time to merge", "value": duration(_median([h for _, h in merged])),
         "note": "from ready for review"},
        {"label": "Merged", "value": str(len(merged)), "note": f"in the last {days} days"},
    ]

    # Weekly series, oldest first, covering the period.
    weeks = []
    w = week_start(since.date())
    while w <= now.date():
        weeks.append(w)
        w += timedelta(days=7)
    per_week_merged = {wk: 0 for wk in weeks}
    per_week_first: dict = {wk: [] for wk in weeks}
    for p, _ in merged:
        wk = week_start(p["merged_at"].date())
        if wk in per_week_merged:
            per_week_merged[wk] += 1
    for p, h in first:
        wk = week_start(p["first_review_at"].date())
        if wk in per_week_first:
            per_week_first[wk].append(h)
    merged_bars = bars([(f"{wk:%d %b}", per_week_merged[wk],
                         f"Week of {wk:%d %b}: {per_week_merged[wk]} merged") for wk in weeks])
    first_bars = bars([(f"{wk:%d %b}", _median(per_week_first[wk]),
                        f"Week of {wk:%d %b}: median {duration(_median(per_week_first[wk]))} "
                        f"over {len(per_week_first[wk])} PR{'s' if len(per_week_first[wk]) != 1 else ''}")
                       for wk in weeks])

    daily = {d["day"]: d for d in db.rows(conn, "SELECT * FROM daily WHERE day >= %s ORDER BY day", (since.date(),))}
    days_list = [since.date() + timedelta(days=i) for i in range((now.date() - since.date()).days + 1)]
    stale_bars = bars([(f"{d:%d %b}", daily[d]["stale_branches"] if d in daily else None,
                        f"{d:%d %b}: {daily[d]['stale_branches']} stale, {daily[d]['left_branches']} left after a PR"
                        if d in daily else f"{d:%d %b}: no snapshot") for d in days_list])
    review_bars = bars([(f"{d:%d %b}", daily[d]["needs_review"] if d in daily else None,
                         f"{d:%d %b}: {daily[d]['needs_review']} needing review of {daily[d]['open_prs']} open"
                         if d in daily else f"{d:%d %b}: no snapshot") for d in days_list])

    by_repo: dict = {}
    for p, h in first:
        by_repo.setdefault(p["full_name"], {"name": p["repo_name"], "first": [], "merge": []})["first"].append(h)
    for p, h in merged:
        by_repo.setdefault(p["full_name"], {"name": p["repo_name"], "first": [], "merge": []})["merge"].append(h)
    repo_rows = sorted(({"full_name": k, "name": v["name"], "reviewed": len(v["first"]), "merged": len(v["merge"]),
                         "first": duration(_median(v["first"])), "merge": duration(_median(v["merge"]))}
                        for k, v in by_repo.items()), key=lambda r: (-r["merged"], r["name"]))

    reviewers = db.rows(conn, """
        SELECT x->>'user' AS login, COUNT(*) AS waiting,
               MIN(COALESCE(p.review_requested_at, p.ready_at, p.created_at)) AS oldest
          FROM pull_requests p JOIN repos r ON r.id = p.repo_id, jsonb_array_elements(p.requested) x
         WHERE p.state = 'open' AND NOT p.draft AND r.included AND x ? 'user'
         GROUP BY 1 ORDER BY 2 DESC, 3 LIMIT 10""")
    auto = db.one(conn, "SELECT COUNT(*) FILTER (WHERE by_rule) AS auto, COUNT(*) AS total FROM assignments "
                        "WHERE assigned_at >= %s", (since,))
    return {"tiles": tiles, "merged_bars": merged_bars, "first_bars": first_bars, "stale_bars": stale_bars,
            "review_bars": review_bars, "repos": repo_rows, "reviewers": reviewers, "assignments": auto,
            "days": days, "has_data": bool(prs or daily)}


def compact(n) -> object:
    """2.0 -> 2, for showing stored numbers in forms."""
    return int(n) if isinstance(n, float) and n.is_integer() else n
