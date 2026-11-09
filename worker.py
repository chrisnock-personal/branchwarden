#!/usr/bin/env python3
"""
worker.py
~~~~~~~~~
Branchwarden's background worker. Every few seconds it:

- rescans repos that webhooks or the dashboard asked about,
- runs a full scan of every account when the scan interval has passed,
- queues messages for what the scans saw happen (see rules.py),
- requests reviewers for PRs nobody was asked to review (when that rule is on),
- queues reminders during working hours and the weekly digest when it's due,
- once an hour, removes old records and writes the day's counts for the trend charts,
- sends what's queued to Teams and Slack (see notify.py).

Only one worker runs at a time: a second one waits on a Postgres advisory lock.

Usage:
    python worker.py              # run for ever
    python worker.py --once       # one full scan, then deliver, then exit
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

import psycopg2  # noqa: E402

import db  # noqa: E402
import housekeeping  # noqa: E402
import notify  # noqa: E402
import rules  # noqa: E402
import scanner  # noqa: E402
import settings  # noqa: E402
from github import Connections  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("branchwarden")

TICK = 5            # seconds between loops
REMINDER_EVERY = 60  # seconds between reminder and auto-assign checks
HOUSEKEEPING_EVERY = 3600
_stop: list[bool] = []


def _last_full_scan(conn):
    row = db.one(conn, "SELECT MAX(started_at) AS at FROM scan_log WHERE trigger IN ('schedule', 'manual')")
    return row["at"] if row else None


def run_scan(conn, values: dict, trigger: str, only: set | None = None) -> scanner.ScanResult:
    """Scan every enabled account (or just the repos in `only`), log it, and
    queue messages for what happened."""
    log_id = db.one(conn, "INSERT INTO scan_log (trigger) VALUES (%s) RETURNING id", (trigger,))["id"]
    conn.commit()
    result = scanner.ScanResult()
    conns = Connections(values)
    targets = db.rows(conn, "SELECT * FROM targets WHERE enabled ORDER BY id")
    if not conns.configured:
        result.errors.append("No GitHub App or token is set up yet (Settings › GitHub)")
    elif not targets:
        result.errors.append("No account or organisation to scan yet (Settings › GitHub)")
    else:
        for t in targets:
            if only is None:
                scanner.scan_target(conn, conns, t, values, result)
                continue
            mine = {f for f in only if f.split("/", 1)[0] == t["login"]}
            mine |= {r["full_name"] for r in db.rows(
                conn, "SELECT full_name FROM repos WHERE target_id = %s AND full_name = ANY(%s)",
                (t["id"], list(only)))}
            if mine:
                scanner.scan_target(conn, conns, t, values, result, only=mine)
    queued = rules.handle_events(conn, result.events, values) if result.events else 0
    rate = result.rate
    db.execute(conn, """UPDATE scan_log SET finished_at = NOW(), repos = %s, prs = %s, branches = %s,
                               events = %s, rate_remaining = %s, rate_limit = %s, error = %s WHERE id = %s""",
               (result.repos, result.prs, result.branches, len(result.events),
                rate.remaining if rate else None, rate.limit if rate else None,
                "; ".join(result.errors)[:2000] or None, log_id))
    conn.commit()
    level = logging.WARNING if result.errors else logging.INFO
    log.log(level, "Scan (%s): %d repos, %d open PRs, %d branches, %d events, %d messages queued%s",
            trigger, result.repos, result.prs, result.branches, len(result.events), queued,
            f"; problems: {'; '.join(result.errors)}" if result.errors else "")
    return result


def take_requests(conn) -> tuple[bool, set, int]:
    """(full scan asked for, repos asked for, highest request id)."""
    reqs = db.rows(conn, "SELECT id, full_name FROM scan_requests ORDER BY id")
    if not reqs:
        return False, set(), 0
    return (any(r["full_name"] == "*" for r in reqs), {r["full_name"] for r in reqs if r["full_name"] != "*"},
            max(r["id"] for r in reqs))


def tick(state: dict, *, force_full: bool = False) -> None:
    now = datetime.now(timezone.utc)
    with db.get_conn() as conn:
        values = settings.load(conn)
        full_wanted, repos_wanted, max_id = take_requests(conn)
        interval = timedelta(minutes=max(1, settings.number(values, "SCAN_INTERVAL_MINUTES", 10)))
        last = _last_full_scan(conn)
        due = force_full or full_wanted or last is None or now - last >= interval
        if due:
            run_scan(conn, values, "manual" if (force_full or full_wanted) else "schedule")
        elif repos_wanted:
            run_scan(conn, values, "webhook", only=repos_wanted)
        if max_id:
            db.execute(conn, "DELETE FROM scan_requests WHERE id <= %s", (max_id,))
            conn.commit()
        if force_full or time.monotonic() - state.get("reminders_at", 0) >= REMINDER_EVERY:
            state["reminders_at"] = time.monotonic()
            assigned = rules.run_auto_assign(conn, values, now)
            conn.commit()
            if assigned:
                log.info("Auto-assigned reviewers on %d pull request%s", assigned, "s" if assigned != 1 else "")
            n = rules.run_reminders(conn, values, now)
            n += rules.run_digest(conn, values, now)
            conn.commit()
            if n:
                log.info("Queued %d reminder and digest messages", n)
        if force_full or time.monotonic() - state.get("housekeeping_at", -HOUSEKEEPING_EVERY) >= HOUSEKEEPING_EVERY:
            state["housekeeping_at"] = time.monotonic()
            housekeeping.prune(conn, values)
            housekeeping.snapshot(conn, values, now)
            conn.commit()
        sent, failed = notify.deliver(conn)
        if sent or failed:
            log.info("Delivered %d messages, %d failed", sent, failed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Branchwarden worker")
    parser.add_argument("--once", action="store_true", help="one full scan and delivery, then exit")
    args = parser.parse_args()

    problem = settings.secret_key_problem()
    if problem:
        log.error(problem)
        sys.exit(1)
    for attempt in range(30):
        try:
            db.init_schema()
            break
        except psycopg2.OperationalError as exc:
            log.info("Waiting for the database (%s)", str(exc).strip().splitlines()[0])
            time.sleep(2)
    else:
        log.error("The database never answered")
        sys.exit(1)
    settings.bootstrap()
    with db.get_conn() as conn:
        rules.seed(conn)

    lock_conn = psycopg2.connect(db.DATABASE_URL)
    lock_conn.autocommit = True
    with lock_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(7347003)")
        if not cur.fetchone()[0]:
            log.info("Another worker is running; waiting for it to stop")
            cur.execute("SELECT pg_advisory_lock(7347003)")

    signal.signal(signal.SIGTERM, lambda *_: _stop.append(True))
    state: dict = {}
    if args.once:
        tick(state, force_full=True)
        return
    log.info("Branchwarden worker started")
    while not _stop:
        try:
            tick(state)
        except psycopg2.Error as exc:
            log.error("Database error: %s", str(exc).strip().splitlines()[0] if str(exc).strip() else exc)
        except Exception:
            log.exception("Unexpected error in the worker loop")
        for _ in range(TICK * 2):
            if _stop:
                break
            time.sleep(0.5)
    log.info("Branchwarden worker stopped")


if __name__ == "__main__":
    main()
