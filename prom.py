"""
prom.py
~~~~~~~
The headline numbers in Prometheus's text format, at /metrics/prometheus, for
Grafana and alerting. Scraped with an API token (a read-only service token is
the usual choice): Authorization: Bearer bwt_…, so there's no separate secret.

Everything is labelled by repo where it has one, for per-repo panels. Worked
out from the database at most every CACHE_SECONDS, however often it's scraped.
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import data
import db
import repohealth
import repos as repos_mod
import settings
from version import VERSION

CACHE_SECONDS = 15
WINDOW_DAYS = 30            # for the review and merge time summaries
_cache: dict = {"at": 0.0, "text": ""}
_lock = threading.Lock()


def _label(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Out:
    def __init__(self):
        self.lines: list[str] = []

    def metric(self, name: str, kind: str, help_: str, samples: Iterable[tuple[dict, float]], suffix: str = ""):
        self.lines.append(f"# HELP branchwarden_{name} {help_}")
        self.lines.append(f"# TYPE branchwarden_{name} {kind}")
        for labels, value in samples:
            lbl = ",".join(f'{k}="{_label(v)}"' for k, v in labels.items())
            n = f"branchwarden_{name}{suffix}"
            self.lines.append(f"{n}{{{lbl}}} {_num(value)}" if lbl else f"{n} {_num(value)}")

    def summary(self, name: str, help_: str, by_repo: dict[str, list[float]]):
        self.lines.append(f"# HELP branchwarden_{name} {help_}")
        self.lines.append(f"# TYPE branchwarden_{name} summary")
        for repo, values in sorted(by_repo.items()):
            q = _quantiles(values)
            for quantile, v in q.items():
                self.lines.append(f'branchwarden_{name}{{repo="{_label(repo)}",quantile="{quantile}"}} {_num(v)}')
            self.lines.append(f'branchwarden_{name}_sum{{repo="{_label(repo)}"}} {_num(sum(values))}')
            self.lines.append(f'branchwarden_{name}_count{{repo="{_label(repo)}"}} {len(values)}')

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _num(v) -> str:
    if v is None:
        return "NaN"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return repr(round(v, 3))
    return str(v)


def _quantiles(values: list[float]) -> dict[str, float]:
    s = sorted(values)
    if not s:
        return {}
    if len(s) == 1:
        return {"0.5": s[0], "0.9": s[0]}
    return {"0.5": statistics.median(s), "0.9": statistics.quantiles(s, n=10, method="inclusive")[8]}


def render(conn, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    values = settings.load(conn)
    prs = data.open_prs(conn, now=now)
    branches = data.classified_branches(conn, values, now=now)
    out = Out()

    out.metric("info", "gauge", "Branchwarden's version, and whether demo data is loaded",
               [({"version": VERSION, "demo": str(settings.flag(values, "DEMO_MODE")).lower()}, 1)])

    by_state = Counter((p["full_name"], p["state_key"]) for p in prs)
    out.metric("open_pull_requests", "gauge", "Open pull requests, by repo and headline state",
               [({"repo": r, "state": s}, n) for (r, s), n in sorted(by_state.items())])
    flags = Counter()
    for p in prs:
        for f, key in (("big", "big"), ("stalled", "stalled"), ("ci_failing", "ci"), ("conflicts", "conflicts"),
                       ("no_ticket", "noticket"), ("needs_review", "review")):
            if key in p["cats"] and not p["draft"]:
                flags[(p["full_name"], f)] += 1
    out.metric("pull_requests_flagged", "gauge", "Open pull requests (not drafts) that are big, stalled, failing CI, "
                                                 "conflicting, without a ticket, or needing review",
               [({"repo": r, "flag": f}, n) for (r, f), n in sorted(flags.items())])
    oldest = defaultdict(float)
    for p in prs:
        if not p["draft"]:
            oldest[p["full_name"]] = max(oldest[p["full_name"]], p["waiting_seconds"])
    out.metric("longest_wait_seconds", "gauge", "How long the longest-waiting open PR has been in its state, by repo",
               [({"repo": r}, round(s)) for r, s in sorted(oldest.items())])

    by_branch = Counter((b["full_name"], b["status"]) for b in branches if b["status"] != "active" and b["status"] != "open_pr")
    out.metric("branches", "gauge", "Flagged branches: stale (no PR), left after a merge or close, or pushed after merge",
               [({"repo": r, "status": s}, n) for (r, s), n in sorted(by_branch.items())])

    waiting, waited = Counter(), {}
    for p in prs:
        if p["draft"]:
            continue
        asked = p.get("review_requested_at") or p["since"]
        for who in p["requested_users"] + p["requested_teams"]:
            waiting[who] += 1
            waited[who] = max(waited.get(who, 0), (now - asked).total_seconds())
    out.metric("reviews_waiting", "gauge", "Open PRs asking each reviewer (person or team) to review",
               [({"reviewer": w}, n) for w, n in sorted(waiting.items())])
    out.metric("oldest_review_request_seconds", "gauge", "How long each reviewer's oldest open request has waited",
               [({"reviewer": w}, round(s)) for w, s in sorted(waited.items())])

    since = now - timedelta(days=WINDOW_DAYS)
    first, merge = defaultdict(list), defaultdict(list)
    for r in db.rows(conn, """SELECT x.full_name, p.created_at, p.ready_at, p.first_review_at, p.merged_at
                                FROM pull_requests p JOIN repos x ON x.id = p.repo_id
                               WHERE x.included AND (p.first_review_at >= %s OR p.merged_at >= %s)""", (since, since)):
        start = max(r["created_at"], r["ready_at"]) if r["ready_at"] else r["created_at"]
        if r["first_review_at"] and r["first_review_at"] >= since:
            first[r["full_name"]].append(max(0.0, (r["first_review_at"] - start).total_seconds() / 3600))
        if r["merged_at"] and r["merged_at"] >= since:
            merge[r["full_name"]].append(max(0.0, (r["merged_at"] - start).total_seconds() / 3600))
    out.summary("time_to_first_review_hours", f"Hours from ready for review to the first review, last {WINDOW_DAYS} days",
                first)
    out.summary("time_to_merge_hours", f"Hours from ready for review to merge, last {WINDOW_DAYS} days", merge)

    rows = repos_mod.overview(conn, values, now)
    out.metric("workflows_failing", "gauge", "Workflows whose last run on the default branch (or schedule) failed",
               [({"repo": r["full_name"]}, r["failing"]) for r in rows])
    out.metric("repo_health_flag", "gauge", "1 where a repository setting is flagged (see the Repositories page)",
               [({"repo": r["full_name"], "flag": f}, int(any(x["key"] == f for x in r["flags"])))
                for r in rows for f in repohealth.FLAGS])

    last = db.one(conn, """SELECT * FROM scan_log WHERE trigger IN ('schedule', 'manual') AND finished_at IS NOT NULL
                            ORDER BY id DESC LIMIT 1""")
    out.metric("last_full_scan_timestamp_seconds", "gauge", "When the last full scan finished (Unix time)",
               [({}, int(last["finished_at"].timestamp()) if last else None)])
    out.metric("last_full_scan_duration_seconds", "gauge", "How long the last full scan took",
               [({}, round((last["finished_at"] - last["started_at"]).total_seconds(), 1) if last else None)])
    out.metric("last_full_scan_error", "gauge", "1 if the last full scan reported a problem",
               [({}, int(bool(last and last["error"])))])
    out.metric("last_full_scan_repos", "gauge", "Repos the last full scan read in full, and skipped as unchanged",
               [({"read": "full"}, last["repos"] - (last.get("skipped") or 0) if last else None),
                ({"read": "skipped"}, last.get("skipped") if last else None)])
    out.metric("github_rate_remaining", "gauge", "GitHub API budget left after the last full scan",
               [({}, last["rate_remaining"] if last else None)])
    out.metric("scans_stopped", "gauge", "1 when no full scan has finished for twice the scan interval",
               [({}, int(data.worker_overdue(conn, now) is not None))])
    msgs = {r["status"]: r["n"] for r in db.rows(conn, "SELECT status, COUNT(*) AS n FROM notifications GROUP BY 1")}
    sent_day = db.one(conn, "SELECT COUNT(*) AS n FROM notifications WHERE status = 'sent' AND sent_at > %s",
                      (now - timedelta(days=1),))["n"]
    out.metric("messages", "gauge", "Teams and Slack messages: waiting to go, failed (kept for the log period), "
                                    "and sent in the last 24 hours",
               [({"status": "pending"}, msgs.get("pending", 0)), ({"status": "failed"}, msgs.get("failed", 0)),
                ({"status": "sent_24h"}, sent_day)])
    return out.text()


def cached(get_conn=db.get_conn) -> str:
    with _lock:
        if time.monotonic() - _cache["at"] < CACHE_SECONDS and _cache["text"]:
            return _cache["text"]
        with get_conn() as conn:
            _cache["text"] = render(conn)
        _cache["at"] = time.monotonic()
        return _cache["text"]
