#!/usr/bin/env python3
"""
demo.py
~~~~~~~
Made-up activity for trying Branchwarden without GitHub, and for screenshots:
a "demo-org" organisation with six repositories, eight people, open PRs in
every state, five weeks of merges, reviews, branch deletions, daily snapshots
and GitHub Actions runs.

    python demo.py            # load it (in compose: podman-compose exec worker python demo.py)
    python demo.py --remove   # take it out again

It refuses to load when a GitHub token or App is set up, or there is already
data from GitHub, so demo and real data never mix. While it's loaded, every
page says so, the worker doesn't scan, and nothing is sent: the demo channels
have no webhook URLs. Adding a token or an App is refused until it's removed.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from psycopg2.extras import Json

import db
import settings

ORG = "demo-org"
TICKETS = "https://tickets.example.com/browse/{key}"
PEOPLE = {"dana": "Dana Reyes", "sasha": "Sasha Okafor", "morgan": "Morgan Lindqvist", "lee": "Lee Tanaka",
          "priya": "Priya Nair", "tom": "Tom Hartley", "alex": "Alex Moreau", "sam": "Sam Kowalski"}
REPOS = ("auth-service", "billing-api", "data-pipeline", "infra", "mobile-app", "web-console")
HEALTH = {
    "auth-service": (True, {"protected": True, "reviews": 1, "rules": ["pull_request", "deletion"]},
                     "* @demo-org/identity\nsrc/token/ @sasha @demo-org/identity\ndocs/ @dana\n"),
    "billing-api": (False, {"protected": True, "reviews": 0, "rules": ["deletion", "non_fast_forward"]},
                    "* @demo-org/payments\n/migrations/ @morgan\n"),
    "data-pipeline": (False, {"protected": False, "reviews": None, "rules": []}, None),
    "infra": (True, {"protected": True, "reviews": 2, "rules": ["pull_request"]},
              "* @demo-org/platform\nterraform/dns/ @priya\n"),
    "mobile-app": (False, {"protected": True, "reviews": 1, "rules": ["pull_request"]}, "* @demo-org/mobile\n"),
    "web-console": (True, {"protected": True, "reviews": 1, "rules": ["pull_request"]}, None),
}
PR_COLS = ("repo_id", "number", "title", "url", "author", "state", "draft", "head_ref", "head_oid", "base_ref",
           "review_decision", "mergeable", "ci_state", "additions", "deletions", "changed_files", "files", "labels",
           "requested", "reviews", "created_at", "updated_at", "ready_at", "review_requested_at", "last_commit_at",
           "merged_at", "merged_by", "closed_at", "first_review_at", "body_tickets")
# Everything the demo writes, emptied before loading and on removal (targets cascade to repos and their rows).
TABLES = ("snoozes", "assignments", "reminders", "threads", "notifications", "branch_deletions", "daily",
          "channels", "people", "scan_requests", "targets")


class Refused(Exception):
    pass


def loaded(values: dict) -> bool:
    return settings.flag(values, "DEMO_MODE")


def check(conn, values: dict) -> None:
    """Refuse when real GitHub data or a connection is there."""
    if loaded(values):
        return                                    # reloading the demo is fine
    if values.get("GITHUB_TOKEN") or values.get("GITHUB_APP_ID"):
        raise Refused("A GitHub token or App is set up: the demo only loads into an empty Branchwarden.")
    if db.one(conn, "SELECT COUNT(*) AS n FROM targets")["n"] or db.one(conn, "SELECT COUNT(*) AS n FROM repos")["n"]:
        raise Refused("There are accounts or repositories from GitHub already: the demo only loads into an empty Branchwarden.")


class _Builder:
    def __init__(self, conn, now: datetime):
        self.conn, self.now = conn, now
        self.rand = random.Random(11)
        self.repos: dict[str, dict] = {}

    def at(self, days: float, hours: float = 0) -> datetime:
        return self.now - timedelta(days=days, hours=hours)

    def oid(self) -> str:
        return f"{self.rand.getrandbits(160):040x}"

    # ── rows ──
    def pr(self, repo: str, number: int, title: str, author: str, *, state="open", draft=False,
           decision="REVIEW_REQUIRED", mergeable="MERGEABLE", ci="SUCCESS", lines=120, files=4, requested=(),
           reviews=(), created=3.0, active: Optional[float] = None, merged: Optional[float] = None, merged_by=None,
           closed: Optional[float] = None, head=None, paths=None, body=(), asked: Optional[float] = None) -> None:
        r = self.repos[repo]
        created_at = self.at(created)
        active_at = self.at(active) if active is not None else self.at(min(created, 1), 2)
        revs = [{"login": who, "state": st, "at": self.at(d).isoformat()} for who, st, d in reviews]
        first = min((self.at(d) for who, _, d in reviews if who != author), default=None)
        head = head or "feat/" + "-".join(title.split(" ", 1)[-1].lower().split())[:30].rstrip("-")
        req = [{"team": x} if "/" in x else {"user": x} for x in requested]
        row = dict(repo_id=r["id"], number=number, title=title, url=f"{r['url']}/pull/{number}", author=author,
                   state=state, draft=draft, head_ref=head, head_oid=self.oid(), base_ref="main",
                   review_decision=decision, mergeable=mergeable, ci_state=ci, additions=int(lines * 0.8),
                   deletions=lines - int(lines * 0.8), changed_files=files,
                   files=Json(paths or [f"src/{head.split('/')[-1]}.py"]), labels=Json([]), requested=Json(req),
                   reviews=Json(revs), created_at=created_at, updated_at=active_at, ready_at=None,
                   review_requested_at=self.at(asked) if asked is not None else (created_at + timedelta(hours=1) if req else None),
                   last_commit_at=active_at, merged_at=self.at(merged) if merged is not None else None,
                   merged_by=merged_by,
                   closed_at=self.at(closed) if closed is not None else (self.at(merged) if merged is not None else None),
                   first_review_at=first, body_tickets=Json(list(body)))
        db.execute(self.conn, f"INSERT INTO pull_requests ({', '.join(PR_COLS)}) VALUES ({', '.join(['%s'] * len(PR_COLS))})",
                   [row[c] for c in PR_COLS])

    def branch(self, repo: str, name: str, *, days: float, who: str, pr=None, state=None, pushed_after=False,
               merged_by=None, protected=False) -> None:
        r = self.repos[repo]
        head = self.oid()
        db.execute(self.conn, """
            INSERT INTO branches (repo_id, name, head_oid, last_commit_at, last_committer, protected, ahead, behind,
                                  pr_number, pr_state, pr_title, pr_head_oid, pr_merged_at, pr_closed_at, pr_merged_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (r["id"], name, head, self.at(days), who, protected, self.rand.randint(1, 9), self.rand.randint(0, 40),
                    pr, state, f"PR for {name}" if pr else None, self.oid() if pushed_after else head,
                    self.at(days - 1) if state == "MERGED" else None,
                    self.at(days - 1) if state in ("MERGED", "CLOSED") else None, merged_by))


def _workflows(b: _Builder) -> int:
    """CI everywhere; a nightly that started failing and skipped a night; a
    weekly audit; a manual release; a docs backup GitHub switched off."""
    run_id = [900_000_000]
    wf_id = [800_000_000]
    rand = b.rand

    def workflow(repo, name, file, triggers, crons=(), state="active", state_since=None):
        wf_id[0] += 1
        r = b.repos[repo]
        db.execute(b.conn, """INSERT INTO workflows (id, repo_id, name, path, state, url, triggers, crons, created_at,
                                  crons_since, state_since, missed_seen_at)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (wf_id[0], r["id"], name, f".github/workflows/{file}", state, f"{r['url']}/actions/workflows/{file}",
                    Json(list(triggers)), Json(list(crons)), b.at(200), b.at(200), state_since or b.at(200), b.now))
        return wf_id[0]

    def run(repo, wid, event, at, conclusion="success", branch="main", minutes=None):
        if at > b.now:
            return
        run_id[0] += 1
        r = b.repos[repo]
        took = timedelta(minutes=minutes) if minutes else timedelta(seconds=rand.randint(40, 420))
        db.execute(b.conn, """INSERT INTO workflow_runs (id, repo_id, workflow_id, name, event, status, conclusion,
                                  head_branch, head_sha, run_number, run_attempt, actor, url, created_at, started_at, updated_at)
                              VALUES (%s,%s,%s,'',%s,'completed',%s,%s,%s,%s,1,%s,%s,%s,%s,%s)""",
                   (run_id[0], r["id"], wid, event, conclusion, branch, b.oid(), run_id[0] % 1000,
                    None if event == "schedule" else rand.choice(list(PEOPLE)), f"{r['url']}/actions/runs/{run_id[0]}",
                    at, at + timedelta(seconds=5), at + took))

    for n, repo in enumerate(REPOS):
        wid = workflow(repo, "CI", "ci.yml", ["push", "pull_request"])
        for d in range(32, -1, -1):
            day = b.at(d)
            if day.weekday() >= 5:
                continue
            for _ in range(rand.randint(1, 5 if n < 3 else 2)):
                pr = rand.random() < 0.6
                bad = rand.random() < (0.25 if pr else 0.06)
                run(repo, wid, "pull_request" if pr else "push",
                    day.replace(hour=rand.randint(8, 17), minute=rand.randint(0, 59)), "failure" if bad else "success",
                    rand.choice(["feat/export", "fix/retry", "feat/sso", "chore/deps"]) if pr else "main")
    wid = workflow("auth-service", "Nightly build", "nightly.yml", ["schedule", "workflow_dispatch"], ["0 2 * * *"])
    for d in range(32, -1, -1):
        if d == 6:
            continue                                       # GitHub skipped this night
        run("auth-service", wid, "schedule", b.at(d).replace(hour=2, minute=0) + timedelta(minutes=rand.randint(12, 40)),
            "failure" if d <= 2 else "success", minutes=rand.randint(9, 14))
    wid = workflow("billing-api", "Dependency audit", "audit.yml", ["schedule", "workflow_dispatch"], ["0 6 * * 1"])
    for d in range(32, -1, -1):
        due = b.at(d).replace(hour=6, minute=0)
        if due.weekday() == 0:
            run("billing-api", wid, "schedule", due + timedelta(minutes=rand.randint(5, 30)))
    wid = workflow("billing-api", "Release", "release.yml", ["workflow_dispatch"])
    for d in (25, 11, 3):
        run("billing-api", wid, "workflow_dispatch", b.at(d).replace(hour=15, minute=10), minutes=6)
    wid = workflow("data-pipeline", "Docs backup", "backup-docs.yml", ["schedule"], ["30 4 * * *"],
                   state="disabled_inactivity", state_since=b.at(9))
    for d in range(32, 8, -1):
        run("data-pipeline", wid, "schedule", b.at(d).replace(hour=4, minute=30) + timedelta(minutes=rand.randint(5, 20)),
            minutes=2)
    return run_id[0] - 900_000_000


def load(conn, values: dict, now: Optional[datetime] = None) -> dict:
    """Load the demo (replacing an earlier one). Returns what was loaded."""
    check(conn, values)
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    clear(conn)
    b = _Builder(conn, now)
    target = db.one(conn, "INSERT INTO targets (login, kind, via, repo_count, last_scan_at) VALUES (%s, 'org', 'auto', %s, NOW()) "
                          "RETURNING id", (ORG, len(REPOS)))["id"]
    for name in REPOS:
        delete_on, health, owners = HEALTH[name]
        b.repos[name] = db.one(conn, """
            INSERT INTO repos (target_id, owner, name, full_name, url, default_branch, private, included, codeowners,
                               scanned_once, last_scan_at, history_at, full_scan_at, delete_branch_on_merge, health,
                               health_at, workflows_scanned)
            VALUES (%s, %s, %s, %s, %s, 'main', TRUE, TRUE, %s, TRUE, NOW(), NOW(), NOW(), %s, %s, NOW(), TRUE)
            RETURNING *""", (target, ORG, name, f"{ORG}/{name}", f"https://github.com/{ORG}/{name}", owners,
                             delete_on, Json(health)))
    for login, name in PEOPLE.items():
        db.execute(conn, "INSERT INTO people (login, name) VALUES (%s, %s)", (login, name))

    # Open pull requests: Dana has three reviews waiting and four PRs of her own.
    b.pr("auth-service", 77, "AUTH-214 Rotate signing keys without downtime", "tom", requested=["dana", "demo-org/identity"],
         created=2.2, asked=2.0, lines=180, files=6, paths=["src/token/keys.py", "src/token/rotate.py"])
    b.pr("auth-service", 81, "AUTH-220 Rate-limit password resets", "priya", decision="APPROVED",
         reviews=[("dana", "APPROVED", 3)], created=5, active=3, lines=95)
    b.pr("auth-service", 74, "Refactor the session store", "tom", lines=1240, files=42,
         reviews=[("lee", "COMMENTED", 11)], created=16, active=11, head="refactor/session-store")
    b.pr("billing-api", 142, "BILL-88 Invoice PDFs", "dana", decision="CHANGES_REQUESTED",
         reviews=[("morgan", "CHANGES_REQUESTED", 4)], created=6, active=4, lines=340, files=9)
    b.pr("billing-api", 145, "BILL-91 Retry webhooks with back-off", "lee", ci="FAILURE", requested=["sasha"],
         created=1, lines=210)
    b.pr("billing-api", 139, "Upgrade the Postgres driver", "alex", draft=True, created=9, active=6, lines=60)
    b.pr("data-pipeline", 58, "Nightly export to S3", "morgan", created=2, lines=150, head="feat/DATA-17-s3-export")
    b.pr("data-pipeline", 55, "Backfill the events table", "sam", mergeable="CONFLICTING", lines=2300, files=18,
         requested=["morgan"], created=21, active=15, body=["DATA-9"])
    b.pr("infra", 33, "OPS-301 Move DNS to Terraform", "priya", decision="APPROVED",
         reviews=[("alex", "APPROVED", 1)], created=3, active=1, lines=420, files=12)
    b.pr("infra", 36, "OPS-305 Alert on certificate expiry", "dana", decision="APPROVED",
         reviews=[("priya", "APPROVED", 0.5)], created=2, active=0.5, lines=70)
    b.pr("mobile-app", 210, "MOB-512 Offline mode for the inbox", "alex", requested=["dana"], lines=860, files=27,
         created=1.6, asked=1.5)
    b.pr("mobile-app", 214, "Fix a crash on rotate", "tom", ci="PENDING", reviews=[("lee", "COMMENTED", 0.3)],
         created=1, lines=24, files=2)
    b.pr("web-console", 96, "WEB-12 Dark mode for the settings pages", "lee", requested=["dana"], created=1.2, asked=1.0,
         lines=260)
    b.pr("web-console", 99, "WEB-19 Audit log page", "sasha", created=1, lines=380, files=11)
    b.pr("web-console", 101, "WEB-23 Keyboard shortcuts", "dana", requested=["lee"], reviews=[("lee", "COMMENTED", 0.2)],
         created=0.8, lines=150)
    b.pr("web-console", 102, "Spike: charts with no library", "dana", draft=True, created=3, active=2, lines=310)
    # Five weeks of merged (and a few closed) PRs: first reviews get quicker over the month, big PRs wait longer.
    number = 300
    for _ in range(85):
        repo = b.rand.choice(REPOS)
        author = b.rand.choice(list(PEOPLE))
        reviewer = b.rand.choice([p for p in PEOPLE if p != author])
        lines = b.rand.choice([20, 45, 80, 130, 220, 380, 650, 1100])
        created = b.rand.uniform(1.5, 88)
        speed = 0.3 + min(created, 35) / 35                  # older PRs waited longer
        wait_review = (0.1 + lines / 500) * speed * b.rand.uniform(0.4, 1.4)
        wait_merge = wait_review + b.rand.uniform(0.1, 1.2) * (1 + lines / 700)
        number += 1
        if created - wait_merge < 0.1:
            continue
        closed = b.rand.random() < 0.08
        b.pr(repo, number, f"{b.rand.choice(['Fix', 'Add', 'Tidy', 'Speed up'])} "
                           f"{b.rand.choice(['retries', 'exports', 'login', 'caching', 'search', 'alerts'])}",
             author, state="closed" if closed else "merged", decision="APPROVED", lines=lines, files=max(1, lines // 40),
             reviews=[(reviewer, "APPROVED", created - wait_review)], created=created, active=created - wait_merge,
             merged=None if closed else created - wait_merge,
             merged_by=None if closed else b.rand.choice([author, reviewer]),
             closed=created - wait_merge if closed else None)

    # Branches: seven stale (one more ignored), five left after a PR, and the open PRs' own.
    for repo in REPOS:
        b.branch(repo, "main", days=0.5, who="dana", protected=True)
    for repo, name, days, who in (("auth-service", "old/ldap-sync", 212, "tom"),
                                  ("web-console", "wip/onboarding-tour", 140, "lee"),
                                  ("auth-service", "spike/oauth-device-flow", 95, "sasha"),
                                  ("infra", "try/redis-cluster", 70, "priya"),
                                  ("billing-api", "experiment/stripe-v2", 48, "morgan"),
                                  ("mobile-app", "poc/push-notifications", 36, "alex"),
                                  ("data-pipeline", "tmp/backfill-2026", 33, "sam"),
                                  ("web-console", "release-candidate", 61, "lee")):
        b.branch(repo, name, days=days, who=who)
    b.branch("billing-api", "feat/invoice-csv", days=6, who="dana", pr=131, state="MERGED", merged_by="morgan")
    b.branch("billing-api", "fix/retry-storm", days=4, who="lee", pr=136, state="MERGED", pushed_after=True, merged_by="sasha")
    b.branch("data-pipeline", "feat/DATA-4-graphql-gateway", days=12, who="morgan", pr=51, state="CLOSED")
    b.branch("mobile-app", "feat/MOB-498-share-sheet", days=9, who="alex", pr=205, state="MERGED", merged_by="tom")
    b.branch("web-console", "feat/export-filters", days=3, who="sasha", pr=94, state="MERGED", merged_by="lee")
    db.execute(conn, "INSERT INTO ignored_branches (repo_id, name, ignored_by, ignored_at) VALUES (%s, %s, %s, %s)",
               (b.repos["web-console"]["id"], "release-candidate", PEOPLE["dana"], b.at(20)))

    # Branch deletions for the MVP prunes board: most by name (the App's webhook), three seen only by a scan.
    for days, who, repo, name in ((0.5, "dana", "billing-api", "feat/refunds"), (1.2, "sasha", "auth-service", "fix/jwt-skew"),
                                  (2, "dana", "web-console", "feat/csv-export"), (3, "lee", "mobile-app", "fix/badge-count"),
                                  (4, "tom", "auth-service", "chore/deps"), (5, None, "infra", "try/k3s"),
                                  (5.5, None, "data-pipeline", "tmp/reindex"), (6, None, "billing-api", "spike/tax")):
        db.execute(conn, "INSERT INTO branch_deletions (full_name, branch, deleted_by, deleted_at) VALUES (%s, %s, %s, %s)",
                   (f"{ORG}/{repo}", name, who, b.at(days)))

    # Five weeks of daily snapshots: reviews waiting ease off, stale branches get tidied.
    for d in range(35, 0, -1):
        db.execute(conn, "INSERT INTO daily (day, open_prs, needs_review, stale_branches, left_branches) VALUES (%s,%s,%s,%s,%s)",
                   (b.at(d).date(), 14 + b.rand.randint(-2, 3), round(4 + d / 5 + b.rand.randint(-1, 1)),
                    round(7 + d / 4), 5 + b.rand.randint(0, 3)))

    # A snoozed reminder, a few reviewers Branchwarden picked, and channels to show routing (no URLs: nothing is sent).
    pr77 = db.one(conn, "SELECT id FROM pull_requests WHERE repo_id = %s AND number = 77", (b.repos["auth-service"]["id"],))["id"]
    pr145 = db.one(conn, "SELECT id FROM pull_requests WHERE repo_id = %s AND number = 145", (b.repos["billing-api"]["id"],))["id"]
    db.execute(conn, "INSERT INTO snoozes (pr_id, until, by_name, at) VALUES (%s, %s, %s, %s)",
               (pr145, now + timedelta(days=3), PEOPLE["dana"], b.at(0.2)))
    db.execute(conn, "INSERT INTO assignments (pr_id, reviewer, by_rule, by_name, assigned_at) VALUES "
                     "(%s, 'dana', TRUE, 'Branchwarden', %s), (%s, 'demo-org/identity', FALSE, %s, %s)",
               (pr77, b.at(2.0), pr77, PEOPLE["tom"], b.at(2.1)))
    db.execute(conn, "INSERT INTO channels (kind, name, repos, teams, enabled) VALUES "
                     "('teams', 'Platform reviews', 'auth-*,infra', '', FALSE), ('slack', '#web-team', 'web-*,mobile-*', '', FALSE), "
                     "('slack', '#identity', '', 'demo-org/identity', FALSE), ('slack', '#eng-all', '', '', FALSE)")
    runs = _workflows(b)
    changes = {"DEMO_MODE": "true"}
    if not settings.get(values, "TICKET_URL"):
        changes["TICKET_URL"] = TICKETS
    settings.save(conn, changes, "demo")
    db.execute(conn, """INSERT INTO scan_log (started_at, finished_at, trigger, repos, prs, branches, events, error)
                        VALUES (NOW(), NOW(), 'manual', %s, %s, %s, 0, NULL)""",
               (len(REPOS), db.one(conn, "SELECT COUNT(*) AS n FROM pull_requests WHERE state = 'open'")["n"],
                db.one(conn, "SELECT COUNT(*) AS n FROM branches")["n"]))
    return {"repos": len(REPOS), "open": db.one(conn, "SELECT COUNT(*) AS n FROM pull_requests WHERE state = 'open'")["n"],
            "prs": db.one(conn, "SELECT COUNT(*) AS n FROM pull_requests")["n"], "runs": runs}


def clear(conn) -> None:
    for table in TABLES:
        db.execute(conn, f"DELETE FROM {table}")      # noqa: S608 (fixed names)


def remove(conn, values: dict) -> bool:
    """Take the demo out. Returns False when none is loaded."""
    if not loaded(values):
        return False
    clear(conn)
    db.execute(conn, "DELETE FROM scan_log")
    changes: dict = {"DEMO_MODE": None}
    if settings.get(values, "TICKET_URL") == TICKETS:
        changes["TICKET_URL"] = None
    settings.save(conn, changes, "demo")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Load or remove Branchwarden's demo data")
    parser.add_argument("--remove", action="store_true", help="take the demo data out again")
    args = parser.parse_args()
    db.init_schema()
    with db.get_conn() as conn:
        values = settings.load(conn)
        if args.remove:
            print("Demo data removed." if remove(conn, values) else "No demo data is loaded.")
            return 0
        try:
            found = load(conn, values)
        except Refused as exc:
            print(f"Not loaded: {exc}", file=sys.stderr)
            return 1
    print(f"Demo loaded: {found['repos']} repositories, {found['open']} open pull requests "
          f"({found['prs']} in all), {found['runs']} workflow runs. Remove it with: python demo.py --remove")
    return 0


if __name__ == "__main__":
    sys.exit(main())
