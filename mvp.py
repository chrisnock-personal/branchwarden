"""
mvp.py
~~~~~~
The MVP page: for a 7-day window, who submitted the most reviews, merged the
most PRs, deleted the most branches and opened the most PRs.

- Reviews: PRs someone reviewed (approved, asked for changes, commented or
  dismissed) in the window, other than their own. GitHub keeps each person's
  latest review per PR, so this counts PRs reviewed, not every comment.
- Merges: PRs merged in the window, by who pressed merge.
- Branch prunes: branches deleted in the window. Who deleted one is only known
  from the GitHub App's delete webhook; deletions only a scan noticed count in
  the team total but for nobody by name.
- PRs raised: PRs opened in the window, by author.

Bots ([bot] logins) are left out of every board.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

import db

TOP = 5
BOARDS = (
    ("reviews", "Most reviews", "PRs they reviewed for other people", ("review", "reviews")),
    ("merges", "Most merges", "PRs they pressed merge on", ("merge", "merges")),
    ("prunes", "Most branch prunes", "Branches they deleted", ("branch deleted", "branches deleted")),
    ("raised", "Most PRs raised", "PRs they opened", ("PR opened", "PRs opened")),
)


def amount(n: int, unit: tuple[str, str]) -> str:
    return f"{n} {unit[0] if n == 1 else unit[1]}"


def window(period: str, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    end = now if period != "prev" else now - timedelta(days=7)
    return end - timedelta(days=7), end


def _human(login: Optional[str]) -> bool:
    return bool(login) and not login.endswith("[bot]")


def rank(counter: Counter, top: int = TOP) -> list[tuple[str, int]]:
    """Highest first; ties in name order, so the board doesn't shuffle."""
    return sorted(((k, v) for k, v in counter.items() if v > 0), key=lambda kv: (-kv[1], kv[0]))[:top]


def compute(conn, period: str = "this", now: Optional[datetime] = None) -> dict:
    start, end = window(period, now)
    reviews: Counter = Counter()
    for r in db.rows(conn, """
            SELECT p.author, r->>'login' AS login
              FROM pull_requests p JOIN repos x ON x.id = p.repo_id, jsonb_array_elements(p.reviews) r
             WHERE x.included AND (r->>'at')::timestamptz >= %s AND (r->>'at')::timestamptz < %s""", (start, end)):
        if _human(r["login"]) and r["login"] != r["author"]:
            reviews[r["login"]] += 1
    merges: Counter = Counter()
    raised: Counter = Counter()
    for p in db.rows(conn, """
            SELECT p.author, p.merged_by, p.merged_at, p.created_at
              FROM pull_requests p JOIN repos x ON x.id = p.repo_id
             WHERE x.included AND ((p.merged_at >= %s AND p.merged_at < %s)
                                   OR (p.created_at >= %s AND p.created_at < %s))""", (start, end, start, end)):
        if p["merged_at"] and start <= p["merged_at"] < end and _human(p["merged_by"]):
            merges[p["merged_by"]] += 1
        if start <= p["created_at"] < end and _human(p["author"]):
            raised[p["author"]] += 1
    prunes: Counter = Counter()
    pruned_total = 0
    for d in db.rows(conn, """SELECT d.deleted_by FROM branch_deletions d
                               WHERE d.deleted_at >= %s AND d.deleted_at < %s
                                 AND EXISTS (SELECT 1 FROM repos x WHERE x.full_name = d.full_name AND x.included)""",
                     (start, end)):
        pruned_total += 1
        if _human(d["deleted_by"]):
            prunes[d["deleted_by"]] += 1

    counters = {"reviews": reviews, "merges": merges, "prunes": prunes, "raised": raised}
    totals = {"reviews": sum(reviews.values()), "merges": sum(merges.values()),
              "prunes": pruned_total, "raised": sum(raised.values())}
    people = {k: len(c) for k, c in counters.items()}
    boards = []
    for key, title, sub, unit in BOARDS:
        ranked = rank(counters[key])
        peak = ranked[0][1] if ranked else 1
        boards.append({"key": key, "title": title, "sub": sub, "unit": unit,
                       "top": ranked[0] if ranked else None,
                       "top_text": amount(ranked[0][1], unit) if ranked else "",
                       "rest": [{"n": i + 2, "login": k, "v": v, "w": round(100 * v / peak)}
                                for i, (k, v) in enumerate(ranked[1:])],
                       "unattributed": key == "prunes" and pruned_total > sum(prunes.values())})
    return {"start": start, "end": end, "boards": boards, "totals": totals, "people": people}


def post_message(result: dict, link: Optional[str], mention: bool = True) -> Optional[dict]:
    """The weekly post: each board's MVP and the next two."""
    if not any(b["top"] for b in result["boards"]):
        return None
    sections = []
    for b in result["boards"]:
        if not b["top"]:
            continue
        login, v = b["top"]
        lines = [{"text": f"{'@{' + login + '}' if mention else login}: {amount(v, b['unit'])}"}]
        if b["rest"][:2]:
            lines.append({"text": " · ".join(f"{r['login']} {r['v']}" for r in b["rest"][:2])})
        sections.append({"title": b["title"], "lines": lines})
    span = f"{result['start']:%d %b} to {(result['end'] - timedelta(seconds=1)):%d %b}"
    return {"heading": f"This week's MVPs ({span})", "tone": "good", "sections": sections,
            "buttons": [["See the full leaderboards", link]]}
