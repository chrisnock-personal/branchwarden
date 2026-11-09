"""
housekeeping.py
~~~~~~~~~~~~~~~
Keeping the database small, and a daily snapshot for the trend charts.

- Closed and merged PRs are kept for RETAIN_PR_DAYS (365 by default): the
  metrics need them. Their reminders, assignments and threads go with them.
- Sent and failed messages, scans and snoozes that have run out are kept for
  RETAIN_LOG_DAYS (90 by default).
- Webhook events are kept for 14 days (dashboard.py prunes those as they come in).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

import data
import db
import settings

log = logging.getLogger("branchwarden.housekeeping")


def prune(conn, values: dict) -> dict:
    pr_days = max(30, settings.number(values, "RETAIN_PR_DAYS", 365))
    log_days = max(7, settings.number(values, "RETAIN_LOG_DAYS", 90))
    removed = {
        "pull requests": db.execute(conn, """DELETE FROM pull_requests WHERE state <> 'open'
                                              AND COALESCE(merged_at, closed_at, seen_at) < NOW() - %s * INTERVAL '1 day'""",
                                    (pr_days,)),
        "messages": db.execute(conn, """DELETE FROM notifications WHERE status <> 'pending'
                                         AND created_at < NOW() - %s * INTERVAL '1 day'""", (log_days,)),
        "scans": db.execute(conn, "DELETE FROM scan_log WHERE started_at < NOW() - %s * INTERVAL '1 day'", (log_days,)),
        "assignments": db.execute(conn, "DELETE FROM assignments WHERE assigned_at < NOW() - %s * INTERVAL '1 day'",
                                  (pr_days,)),
        "snoozes": db.execute(conn, "DELETE FROM snoozes WHERE until < NOW() - INTERVAL '1 day'"),
        "daily counts": db.execute(conn, "DELETE FROM daily WHERE day < CURRENT_DATE - %s", (pr_days,)),
    }
    removed = {k: v for k, v in removed.items() if v}
    if removed:
        log.info("Housekeeping removed %s", ", ".join(f"{n} {k}" for k, n in removed.items()))
    return removed


def snapshot(conn, values: dict, now: Optional[datetime] = None) -> None:
    """Today's headline counts (rewritten through the day, so the last one stands)."""
    now = now or datetime.now(timezone.utc)
    prs = data.open_prs(conn, now=now)
    branches = data.classified_branches(conn, values, now=now)
    day: date = now.astimezone(_tz(values)).date()
    db.execute(conn, """
        INSERT INTO daily (day, open_prs, needs_review, stale_branches, left_branches) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (day) DO UPDATE SET open_prs = EXCLUDED.open_prs, needs_review = EXCLUDED.needs_review,
            stale_branches = EXCLUDED.stale_branches, left_branches = EXCLUDED.left_branches""",
               (day, len(prs), sum(1 for p in prs if "review" in p["cats"] and not p["draft"]),
                sum(1 for b in branches if b["status"] == "stale"),
                sum(1 for b in branches if b["status"] in ("left_merged", "left_closed", "pushed_after_merge"))))


def _tz(values: dict):
    import rules
    return rules.work_hours(values).tz
