"""
workflows.py
~~~~~~~~~~~~
GitHub Actions workflows: what each repo's workflows are, when the scheduled
ones were due, which runs happened and how they ended.

- Each scan lists a repo's workflows (REST, for their state: GitHub switches a
  scheduled workflow off after 60 days with no activity in the repo) and the
  runs since the last scan. The workflow files are read again (GraphQL) only
  when the default branch has moved, for their triggers and cron schedules.
- A cron schedule gives the times a workflow was due. GitHub runs schedules in
  UTC, from the default branch, anything from minutes to hours late, and
  drops some when it's busy. A due time with no scheduled run by the time
  this workflow's runs usually start (see grace_for) is missed.
- Comparing a scan with the last gives the events: a workflow started failing
  or was fixed, a scheduled run was missed, a workflow was switched off.

Needs the "Actions: read" permission. Without it the repo's PRs and branches
are scanned as before and the Workflows page says what's missing.
"""

from __future__ import annotations

import logging
import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional
from urllib.parse import quote

import yaml
from psycopg2.extras import Json

import db
from github import Client, GitHubError

log = logging.getLogger("branchwarden.workflows")

MIN_GRACE = timedelta(minutes=90)      # a scheduled run is missed this long after its time at the soonest…
MAX_DELAY = timedelta(hours=16)        # …and this long at the latest (see grace_for)
FIRST_LOOKBACK = timedelta(days=30)    # runs read on a repo's first workflow scan
RERUN_LOOKBACK = timedelta(hours=3)    # runs re-read each scan, to see re-runs end
MAX_RUNS = 1000                        # per repo per scan (GitHub's own limit for a filtered list)
MAX_FILE = 512 * 1024                  # workflow files bigger than this aren't parsed
PERIODS = ((7, "7 days"), (14, "14 days"), (30, "30 days"))

PASSED = {"success", "neutral"}
FAILED = {"failure", "timed_out", "startup_failure"}
CANCELLED = {"cancelled", "skipped", "stale"}

# Outcome → (label, tone), for the page and the messages.
OUTCOMES = {
    "passed": ("Passed", "good"), "failed": ("Failed", "bad"), "cancelled": ("Cancelled", "muted"),
    "running": ("Running", "info"), "waiting": ("Waiting", "info"), "missed": ("Missed", "warn"),
    "due": ("Due", "muted"), "off": ("Switched off", "muted"),
}
WORST = ("failed", "missed", "running", "waiting", "cancelled", "passed", "due", "off")


def outcome(run: dict) -> str:
    """passed | failed | cancelled | running | waiting, from a run's status and conclusion."""
    if run.get("status") != "completed":
        return "waiting" if run.get("status") in ("waiting", "requested", "pending") else "running"
    c = run.get("conclusion")
    if c in PASSED:
        return "passed"
    if c in FAILED:
        return "failed"
    if c == "action_required":
        return "waiting"
    return "cancelled"


# ─── Cron ───────────────────────────────────────────────────────────────────

MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                      "nov", "dec"), start=1)}
WEEKDAYS = {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}


class CronError(ValueError):
    pass


def _field(text: str, lo: int, hi: int, names: Optional[dict] = None) -> tuple[frozenset, bool]:
    """The values one cron field allows, and whether it was '*' (unrestricted)."""
    out: set[int] = set()
    star = False

    def value(s: str) -> int:
        s = s.strip().lower()
        if names and s in names:
            return names[s]
        if not s.isdigit():
            raise CronError(f"not a number: {s!r}")
        return int(s)

    for part in text.split(","):
        step = 1
        if "/" in part:
            part, _, st = part.partition("/")
            if not st.isdigit() or int(st) < 1:
                raise CronError(f"bad step: {st!r}")
            step = int(st)
        if part == "*":
            a, b = lo, hi
            star = star or step == 1
        elif "-" in part:
            x, _, y = part.partition("-")
            a, b = value(x), value(y)
        else:
            a = value(part)
            b = hi if step > 1 else a      # "5/15" means 5, 20, 35, 50
        if a < lo or b > hi or a > b:
            raise CronError(f"out of range: {text!r}")
        out.update(range(a, b + 1, step))
    return frozenset(out), star


@dataclass(frozen=True)
class Cron:
    text: str
    minutes: frozenset
    hours: frozenset
    days: frozenset
    months: frozenset
    weekdays: frozenset       # 0 = Sunday
    any_day: bool
    any_weekday: bool

    def day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom = d.day in self.days
        dow = (d.isoweekday() % 7) in self.weekdays
        # Standard cron: with both day fields restricted, either one will do.
        if not self.any_day and not self.any_weekday:
            return dom or dow
        return dom and dow

    def per_day(self) -> int:
        return len(self.minutes) * len(self.hours)


def parse_cron(text: str) -> Cron:
    parts = str(text).split()
    if len(parts) != 5:
        raise CronError(f"a cron schedule has five fields: {text!r}")
    minutes, _ = _field(parts[0], 0, 59)
    hours, _ = _field(parts[1], 0, 23)
    days, any_day = _field(parts[2], 1, 31)
    months, _ = _field(parts[3], 1, 12, MONTHS)
    weekdays, any_weekday = _field(parts[4], 0, 7, WEEKDAYS)
    if 7 in weekdays:
        weekdays = (weekdays - {7}) | {0}
    return Cron(" ".join(parts), minutes, hours, days, months, frozenset(weekdays), any_day, any_weekday)


def cron_times(cron: Cron, start: datetime, end: datetime) -> list[datetime]:
    """Every time the cron was due in [start, end), in UTC."""
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    out: list[datetime] = []
    day = start.date()
    while day <= end.date():
        if cron.day_matches(day):
            for h in sorted(cron.hours):
                for m in sorted(cron.minutes):
                    t = datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)
                    if start <= t < end:
                        out.append(t)
        day += timedelta(days=1)
    return out


def next_due(crons: Iterable[Cron], after: datetime) -> Optional[datetime]:
    """The next time any of the crons is due (looking a year ahead)."""
    best = None
    for c in crons:
        for span in (2, 40, 400):
            times = cron_times(c, after + timedelta(seconds=1), after + timedelta(days=span))
            if times:
                best = min(best or times[0], times[0])
                break
    return best


def describe_cron(text: str) -> str:
    """A few common shapes in words; the cron itself otherwise."""
    parts = text.split()
    if len(parts) != 5:
        return text
    m, h, dom, mon, dow = parts
    if m.isdigit() and h.isdigit() and dom == "*" and mon == "*":
        at = f"{int(h):02d}:{int(m):02d} UTC"
        if dow == "*":
            return f"daily at {at}"
        if dow in ("1-5", "mon-fri", "MON-FRI"):
            return f"weekdays at {at}"
        names = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        if dow.isdigit() and int(dow) <= 7:
            return f"{names[int(dow)]}days at {at}"
    if m.isdigit() and h == "*" and dom == mon == dow == "*":
        return f"hourly at :{int(m):02d}"
    return text


# ─── Workflow files ─────────────────────────────────────────────────────────

def parse_workflow(text: str) -> dict:
    """{"triggers": [event, …], "crons": [cron, …], "error": str|None} from a workflow file."""
    try:
        doc = yaml.safe_load(text) if text and len(text) <= MAX_FILE else None
    except yaml.YAMLError as exc:
        return {"triggers": [], "crons": [], "error": f"not valid YAML ({type(exc).__name__})"}
    if not isinstance(doc, dict):
        return {"triggers": [], "crons": [], "error": None if text else "empty"}
    # YAML 1.1 reads a bare `on:` key as true.
    on = doc.get("on", doc.get(True))
    triggers: list[str] = []
    crons: list[str] = []
    if isinstance(on, str):
        triggers = [on]
    elif isinstance(on, list):
        triggers = [str(x) for x in on if isinstance(x, (str, int))]
    elif isinstance(on, dict):
        triggers = [str(k) for k in on]
        for entry in on.get("schedule") or []:
            if isinstance(entry, dict) and entry.get("cron"):
                crons.append(" ".join(str(entry["cron"]).split()))
    error = None
    for c in crons:
        try:
            parse_cron(c)
        except CronError as exc:
            error = f"schedule {c!r}: {exc}"
    return {"triggers": triggers, "crons": crons, "error": error}


def good_crons(texts: Iterable[str]) -> list[Cron]:
    out = []
    for t in texts or []:
        try:
            out.append(parse_cron(t))
        except CronError:
            pass
    return out


# ─── Matching scheduled runs to due times ───────────────────────────────────

def grace_for(delays: list[timedelta]) -> timedelta:
    """How long after its time a scheduled run may still start before it counts
    as missed: 1.5 times this workflow's usual delay plus an hour, between
    MIN_GRACE and MAX_DELAY. GitHub is prompt for some repos and hours late for
    others (quiet repos on the free plan often start 6-8 hours late)."""
    if not delays:
        return MIN_GRACE
    usual = sorted(delays)[len(delays) // 2]
    return min(MAX_DELAY, max(MIN_GRACE, usual * 1.5 + timedelta(hours=1)))


def match_slots(slots: list[datetime], runs: list[dict], now: datetime) -> list[dict]:
    """Pair each due time with the scheduled run that answered it.

    GitHub starts a scheduled run at or after its time, sometimes hours later,
    and skips some. Each run (oldest first) takes the latest due time before it,
    or, if another run already took that, the earliest one still open from the
    MAX_DELAY before it: a late run counts for its own due time, a skipped one
    shows as missed, and the counts stay right when runs come later than the
    schedule's interval. Returns, per slot, {"at", "run" (or None), "state",
    "grace"}: a run's outcome, "due" while it may still start, or "missed"."""
    slots = sorted(slots)
    taken: dict[int, dict] = {}
    for run in sorted(runs, key=lambda r: r["created_at"]):
        at = run["created_at"]
        own = bisect_right(slots, at + timedelta(minutes=1)) - 1
        if own < 0 or at - slots[own] > MAX_DELAY:
            continue                        # no due time it can be for
        if own not in taken:
            taken[own] = run
            continue
        for i in range(own):
            if i not in taken and slots[i] >= at - MAX_DELAY:
                taken[i] = run
                break
    grace = grace_for([taken[i]["created_at"] - slots[i] for i in taken])
    out = []
    for i, s in enumerate(slots):
        run = taken.get(i)
        if run:
            state = outcome(run)
        else:
            state = "due" if now - s < grace else "missed"
        out.append({"at": s, "run": run, "state": state, "grace": grace})
    return out


def usual_delay(matched: list[dict]) -> Optional[timedelta]:
    delays = sorted(m["run"]["created_at"] - m["at"] for m in matched if m["run"])
    return delays[len(delays) // 2] if delays else None


# ─── Reading from GitHub ────────────────────────────────────────────────────

FILES_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    object(expression: "HEAD:.github/workflows") {
      ... on Tree { entries { name type object { ... on Blob { text byteSize } } } }
    }
  }
  rateLimit { remaining limit resetAt }
}"""


def fetch_files(client: Client, owner: str, name: str) -> dict[str, dict]:
    """Path → parsed workflow file, from the default branch."""
    data = client.graphql(FILES_QUERY, {"owner": owner, "name": name})
    tree = (data.get("repository") or {}).get("object") or {}
    out = {}
    for e in tree.get("entries") or []:
        if e.get("type") != "blob" or not re.search(r"\.ya?ml$", e.get("name", ""), re.I):
            continue
        blob = e.get("object") or {}
        if (blob.get("byteSize") or 0) > MAX_FILE:
            out[f".github/workflows/{e['name']}"] = {"triggers": [], "crons": [], "error": "too big to read"}
            continue
        out[f".github/workflows/{e['name']}"] = parse_workflow(blob.get("text") or "")
    return out


def fetch_workflows(client: Client, full_name: str) -> list[dict]:
    return client.paged(f"/repos/{full_name}/actions/workflows", limit=500, key="workflows")


def fetch_runs(client: Client, full_name: str, since: datetime) -> list[dict]:
    created = quote(">=" + since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return client.paged(f"/repos/{full_name}/actions/runs?exclude_pull_requests=true&created={created}",
                        limit=MAX_RUNS, key="workflow_runs")


def _ts(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def run_from_api(r: dict) -> dict:
    return {
        "id": r["id"], "workflow_id": r.get("workflow_id"), "name": r.get("name") or "",
        "event": r.get("event") or "", "status": r.get("status") or "", "conclusion": r.get("conclusion"),
        "head_branch": r.get("head_branch"), "head_sha": r.get("head_sha"),
        "run_number": r.get("run_number"), "run_attempt": r.get("run_attempt") or 1,
        "actor": ((r.get("triggering_actor") or r.get("actor") or {}).get("login") or "").lower() or None,
        "url": r.get("html_url"), "created_at": _ts(r.get("created_at")),
        "started_at": _ts(r.get("run_started_at")), "updated_at": _ts(r.get("updated_at")),
    }


# ─── Scanning one repo ──────────────────────────────────────────────────────

RUN_COLUMNS = ("workflow_id", "name", "event", "status", "conclusion", "head_branch", "head_sha", "run_number",
               "run_attempt", "actor", "url", "created_at", "started_at", "updated_at")


def _runs_since(conn, repo_id: int, now: datetime) -> datetime:
    row = db.one(conn, """SELECT MAX(created_at) AS latest,
                                 MIN(created_at) FILTER (WHERE status <> 'completed'
                                                         AND created_at > %s - INTERVAL '2 days') AS open
                            FROM workflow_runs WHERE repo_id = %s""", (now, repo_id))
    if not row or not row["latest"]:
        return now - FIRST_LOOKBACK
    # Re-runs keep their run's creation time: look back far enough to catch the usual ones.
    since = row["latest"] - RERUN_LOOKBACK
    return min(since, row["open"]) if row["open"] else since


def _store_workflows(conn, repo: dict, listed: list[dict], files: Optional[dict], now: datetime,
                     first: bool) -> list[dict]:
    """Upsert the listed workflows; returns events for ones switched off."""
    events = []
    before = {w["id"]: w for w in db.rows(conn, "SELECT * FROM workflows WHERE repo_id = %s", (repo["id"],))}
    for w in listed:
        old = before.get(w["id"])
        path = w.get("path") or ""
        parsed = (files or {}).get(path)
        if parsed is None:
            parsed = ({"triggers": old["triggers"], "crons": old["crons"], "error": old["file_error"]} if old
                      else {"triggers": [], "crons": [], "error": None})
        if files is not None and path.startswith(".github/") and path not in files:
            parsed = {"triggers": [], "crons": [], "error": "not on the default branch"}
        created = _ts(w.get("created_at")) or now
        state = w.get("state") or "active"
        crons_since = old["crons_since"] if old and old["crons"] == parsed["crons"] else (created if not old else now)
        state_since = old["state_since"] if old and old["state"] == state else (created if not old else now)
        base = path.rsplit("/", 1)[-1]
        url = (f"{repo['url']}/actions/workflows/{base}" if path.startswith(".github/") and repo.get("url")
               else (w.get("html_url") or (f"{repo['url']}/actions" if repo.get("url") else None)))
        db.execute(conn, """
            INSERT INTO workflows (id, repo_id, name, path, state, url, triggers, crons, file_error,
                                   created_at, crons_since, state_since, missed_seen_at, seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (id) DO UPDATE SET repo_id = EXCLUDED.repo_id, name = EXCLUDED.name, path = EXCLUDED.path,
                state = EXCLUDED.state, url = EXCLUDED.url, triggers = EXCLUDED.triggers, crons = EXCLUDED.crons,
                file_error = EXCLUDED.file_error, crons_since = EXCLUDED.crons_since,
                state_since = EXCLUDED.state_since, seen_at = NOW()""",
                   (w["id"], repo["id"], w.get("name") or base, path, state, url, Json(parsed["triggers"]),
                    Json(parsed["crons"]), parsed["error"], created, crons_since, state_since, now))
        if old and not first and old["state"] == "active" and state.startswith("disabled") and old["crons"]:
            events.append({"kind": "wf_disabled", "workflow_id": w["id"], "state": state})
    gone = [i for i in before if i not in {w["id"] for w in listed}]
    if gone:
        db.execute(conn, "DELETE FROM workflow_runs WHERE workflow_id = ANY(%s)", (gone,))
        db.execute(conn, "DELETE FROM workflows WHERE id = ANY(%s)", (gone,))
    return events


def _store_runs(conn, repo_id: int, runs: list[dict]) -> list[tuple[dict, Optional[dict]]]:
    """Upsert runs; returns (run, its row before) for each run that has just ended
    or ended differently (a re-run)."""
    if not runs:
        return []
    before = {r["id"]: r for r in db.rows(
        conn, "SELECT id, status, conclusion, prior_conclusion FROM workflow_runs WHERE id = ANY(%s)",
        ([r["id"] for r in runs],))}
    ended = []
    cols = ", ".join(RUN_COLUMNS)
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in RUN_COLUMNS)
    for r in runs:
        old = before.get(r["id"])
        # A re-run clears the conclusion until it ends: keep the last one for comparing.
        prior = (old["conclusion"] or old["prior_conclusion"]) if old else None
        db.execute(conn, f"""
            INSERT INTO workflow_runs (id, repo_id, {cols}, prior_conclusion)
            VALUES (%s, %s, {', '.join(['%s'] * len(RUN_COLUMNS))}, %s)
            ON CONFLICT (id) DO UPDATE SET {sets}, prior_conclusion = EXCLUDED.prior_conclusion""",
                   [r["id"], repo_id, *[r[c] for c in RUN_COLUMNS], prior])
        if r["status"] == "completed" and (not old or old["status"] != "completed"
                                           or old["conclusion"] != r["conclusion"]):
            ended.append((r, old))
    return ended


def failing_transitions(ended: list[tuple[dict, Optional[dict]]], previous) -> list[dict]:
    """Events for runs that started a failure or ended one.

    `previous(run)` gives the outcome of the run before it (same workflow,
    same branch), or None. A re-run is compared with its own last result."""
    events = []
    for run, old in sorted(ended, key=lambda x: x[0]["created_at"]):
        now = outcome(run)
        if now not in ("passed", "failed"):
            continue
        last_conclusion = (old or {}).get("conclusion") or (old or {}).get("prior_conclusion")
        before = outcome({"status": "completed", "conclusion": last_conclusion}) if last_conclusion \
            else previous(run)
        if now == "failed" and before != "failed":
            events.append({"kind": "wf_failed", "workflow_id": run["workflow_id"], "run_id": run["id"]})
        elif now == "passed" and before == "failed":
            events.append({"kind": "wf_fixed", "workflow_id": run["workflow_id"], "run_id": run["id"]})
    return events


def _previous_outcome(conn):
    def previous(run: dict) -> Optional[str]:
        row = db.one(conn, """SELECT status, conclusion FROM workflow_runs
                               WHERE workflow_id = %s AND head_branch IS NOT DISTINCT FROM %s AND id <> %s
                                 AND created_at < %s AND status = 'completed'
                                 AND conclusion = ANY(%s)
                               ORDER BY created_at DESC LIMIT 1""",
                     (run["workflow_id"], run["head_branch"], run["id"], run["created_at"],
                      sorted(PASSED | FAILED)))
        return outcome(row) if row else None
    return previous


LEARN_DELAY = timedelta(days=14)       # how far back missed_events looks, to learn a workflow's usual delay


def missed_events(conn, repo_id: int, now: datetime) -> list[dict]:
    """Scheduled runs that are now missed, each reported once."""
    events = []
    for w in db.rows(conn, """SELECT * FROM workflows WHERE repo_id = %s AND state = 'active'
                               AND jsonb_array_length(crons) > 0""", (repo_id,)):
        seen = w["missed_seen_at"] or now
        start = max(now - LEARN_DELAY, w["crons_since"] or now, w["state_since"] or now)
        if start >= now:
            continue
        slots = sorted({t for c in good_crons(w["crons"]) for t in cron_times(c, start, now)})
        runs = db.rows(conn, """SELECT id, status, conclusion, created_at FROM workflow_runs
                                 WHERE workflow_id = %s AND event = 'schedule' AND created_at >= %s""",
                       (w["id"], start))
        matched = match_slots(slots, runs, now)
        missed = [m for m in matched if m["state"] == "missed" and m["at"] > seen]
        if missed:
            events.append({"kind": "wf_missed", "workflow_id": w["id"], "times": [m["at"].isoformat() for m in missed],
                           "grace_minutes": int(missed[0]["grace"].total_seconds() // 60)})
        decided = [m["at"] for m in matched if m["state"] != "due"]
        if decided and max(decided) > seen:
            db.execute(conn, "UPDATE workflows SET missed_seen_at = %s WHERE id = %s", (max(decided), w["id"]))
    return events


def scan_repo(conn, client: Client, repo: dict, default_head: Optional[str],
              now: Optional[datetime] = None) -> list[dict]:
    """Read one repo's workflows and new runs; returns events. A GitHub error
    (no Actions access, say) is noted on the repo, not raised: the rest of the
    repo's scan stands."""
    now = now or datetime.now(timezone.utc)
    if repo.get("archived"):
        return []
    first = not repo.get("workflows_scanned")
    full = repo["full_name"]
    try:
        listed = fetch_workflows(client, full)
        files = None
        if listed and (default_head is None or default_head != repo.get("workflows_oid")):
            files = fetch_files(client, repo["owner"], repo["name"])
        runs = [run_from_api(r) for r in fetch_runs(client, full, _runs_since(conn, repo["id"], now))] if listed else []
    except GitHubError as exc:
        text = str(exc)
        if exc.status in (403, 404):
            text = "no access to Actions (the token or App needs Actions: read)" if exc.status == 403 \
                else "Actions isn't available for this repo"
        db.execute(conn, "UPDATE repos SET workflows_error = %s, workflows_at = NOW() WHERE id = %s",
                   (text, repo["id"]))
        log.info("%s: workflows not read: %s", full, exc)
        return []
    events = _store_workflows(conn, repo, listed, files, now, first)
    known = {w["id"] for w in listed}
    runs = [r for r in runs if r["workflow_id"] in known and r["created_at"]]
    ended = _store_runs(conn, repo["id"], runs)
    if not first:
        events += failing_transitions(ended, _previous_outcome(conn))
        events += missed_events(conn, repo["id"], now)
    db.execute(conn, """UPDATE repos SET workflows_scanned = TRUE, workflows_error = NULL, workflows_at = NOW(),
                               workflows_oid = COALESCE(%s, workflows_oid) WHERE id = %s""",
               (default_head if files is not None or not listed else None, repo["id"]))
    for ev in events:
        ev["repo"] = full
    return events


# ─── The Workflows page ─────────────────────────────────────────────────────

def _segments(states: list[str], small: bool) -> list[dict]:
    """A day's cell: one segment per due time when there are few, otherwise one
    per outcome, as wide as its count."""
    if not states:
        return []
    if small:
        return [{"state": s, "grow": 1} for s in states]
    counts: dict[str, int] = {}
    for s in states:
        counts[s] = counts.get(s, 0) + 1
    order = ("passed", "failed", "missed", "cancelled", "running", "waiting", "due", "off")
    return [{"state": s, "grow": counts[s]} for s in order if s in counts]


def worst(states: Iterable[str]) -> Optional[str]:
    states = set(states)
    for s in WORST:
        if s in states:
            return s
    return None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def took(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _cell_label(day: date, counts: dict[str, int]) -> str:
    parts = []
    for s in ("passed", "failed", "missed", "cancelled", "running", "waiting", "due", "off"):
        if counts.get(s):
            parts.append(f"{counts[s]} {OUTCOMES[s][0].lower()}")
    return f"{day.strftime('%a %d %b')}: " + (", ".join(parts) if parts else "no runs")


def build(workflows: list[dict], runs: list[dict], repos: dict[int, dict], now: datetime, days: int,
          tz) -> dict:
    """Everything the Workflows page shows, from stored rows (no database here)."""
    today = now.astimezone(tz).date()
    day_list = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    start = datetime.combine(day_list[0], datetime.min.time(), tz).astimezone(timezone.utc)
    by_wf: dict[int, list[dict]] = {}
    for r in runs:
        by_wf.setdefault(r["workflow_id"], []).append(r)

    rows = []
    for w in workflows:
        repo = repos.get(w["repo_id"]) or {}
        mine = sorted(by_wf.get(w["id"], []), key=lambda r: r["created_at"])
        crons = good_crons(w["crons"])
        active = w["state"] == "active"
        # Due times: from when this schedule was in place, while the workflow was on.
        slot_from = max(start, w["crons_since"] or start)
        if not active and w["state_since"]:
            slot_to = min(now, max(w["state_since"], slot_from))
        else:
            slot_to = now
        slots = sorted({t for c in crons for t in cron_times(c, slot_from, slot_to)}) if crons else []
        scheduled = [r for r in mine if r["event"] == "schedule"]
        matched = match_slots(slots, [r for r in scheduled], now)
        used = {id(s["run"]) for s in matched if s["run"]}
        # (time, state) for everything that happened or should have, in the window.
        marks = [(s["at"], s["state"]) for s in matched]
        marks += [(r["created_at"], outcome(r)) for r in mine if id(r) not in used and r["created_at"] >= start]
        per_day = max((c.per_day() for c in crons), default=0) * max(len(crons), 1)
        small = bool(crons) and per_day <= 4 and len(marks) == len(matched)
        cells = []
        for d in day_list:
            states = [st for at, st in sorted(marks) if at.astimezone(tz).date() == d]
            counts: dict[str, int] = {}
            for s in states:
                counts[s] = counts.get(s, 0) + 1
            cells.append({"day": d, "segments": _segments(states, small), "label": _cell_label(d, counts),
                          "worst": worst(states)})
        in_window = [r for r in mine if r["created_at"] >= start]
        done = [r for r in in_window if outcome(r) in ("passed", "failed")]
        passed = sum(1 for r in done if outcome(r) == "passed")
        default_branch = repo.get("default_branch")
        on_default = [r for r in mine if r["status"] == "completed" and outcome(r) in ("passed", "failed")
                      and (r["event"] == "schedule" or r["head_branch"] == default_branch)]
        latest_default = on_default[-1] if on_default else None
        failing_since, failing_before = None, False
        if latest_default and outcome(latest_default) == "failed":
            failing_before = True          # until a run that passed turns up
            for r in reversed(on_default):
                if outcome(r) != "failed":
                    failing_before = False
                    break
                failing_since = r["created_at"]
        missed = [s["at"] for s in matched if s["state"] == "missed"]
        delay = usual_delay(matched)
        triggers = list(dict.fromkeys(list(w["triggers"] or []) + sorted({r["event"] for r in mine})))
        last = mine[-1] if mine else None
        problems = []
        if not active:
            problems.append("off")
        if failing_since:
            problems.append("failing")
        if missed:
            problems.append("missed")
        rows.append({
            "id": w["id"], "name": w["name"], "path": w["path"], "url": w["url"], "state": w["state"],
            "repo_id": w["repo_id"], "repo": repo.get("name"), "full_name": repo.get("full_name"),
            "repo_url": repo.get("url"), "triggers": triggers,
            "crons": [{"text": c.text, "words": describe_cron(c.text)} for c in crons],
            "file_error": w.get("file_error"), "cells": cells, "small": small,
            "runs": len(in_window), "passed": passed, "finished": len(done),
            "pass_rate": round(100 * passed / len(done)) if done else None,
            "last": last, "last_outcome": outcome(last) if last else None,
            "failing_since": failing_since, "failing_before": failing_before, "missed": missed, "slots": len(slots),
            "usual_delay": took(delay.total_seconds()) if delay and delay >= timedelta(minutes=5) else None,
            "next_due": next_due(crons, now) if crons and active else None,
            "recent": [{**r, "outcome": outcome(r),
                        "took": took((r["updated_at"] - r["started_at"]).total_seconds())
                        if r["status"] == "completed" and r.get("started_at") and r.get("updated_at") else ""}
                       for r in reversed(in_window[-15:])],
            "problems": problems,
        })
    rows.sort(key=lambda r: ((r["repo"] or "").lower(), r["name"].lower()))
    groups: list[dict] = []
    for r in rows:
        if not groups or groups[-1]["repo_id"] != r["repo_id"]:
            groups.append({"repo_id": r["repo_id"], "repo": r["repo"], "full_name": r["full_name"],
                           "repo_url": r["repo_url"], "rows": []})
        groups[-1]["rows"].append(r)
    for g in groups:
        g["problems"] = sum(1 for r in g["rows"] if r["problems"])

    finished = sum(r["finished"] for r in rows)
    passed = sum(r["passed"] for r in rows)
    failing = [r for r in rows if r["failing_since"]]
    off = [r for r in rows if r["state"] != "active"]
    missed_n = sum(len(r["missed"]) for r in rows)
    scheduled = [r for r in rows if r["crons"]]
    tiles = [
        {"label": "Workflows", "value": len(rows), "tone": "",
         "note": f"in {_plural(len(groups), 'repo')} · {len(scheduled)} on a schedule"},
        {"label": "Runs", "value": sum(r["runs"] for r in rows), "tone": "",
         "note": f"{round(100 * passed / finished)}% of finished runs passed" if finished else "none finished"},
        {"label": "Failing now", "value": len(failing), "tone": "tone-bad" if failing else "",
         "note": "last run on the default branch failed"},
        {"label": "Missed schedules", "value": missed_n, "tone": "tone-warn" if missed_n else "",
         "note": "didn't start in time, allowing for GitHub's usual delay"},
        {"label": "Switched off", "value": len(off), "tone": "tone-warn" if off else "",
         "note": "by GitHub or by hand"},
    ]
    attention = sorted([r for r in rows if r["problems"]],
                       key=lambda r: (0 if "failing" in r["problems"] else 1 if "missed" in r["problems"] else 2,
                                      r["repo"] or "", r["name"]))
    return {"days": day_list, "groups": groups, "rows": rows, "tiles": tiles, "attention": attention,
            "start": start}


def load(conn, values: dict, days: int, repo: str = "", now: Optional[datetime] = None) -> dict:
    import rules
    now = now or datetime.now(timezone.utc)
    tz = rules.work_hours(values).tz
    where, params = ["r.included"], []
    if repo:
        where.append("r.full_name = %s")
        params.append(repo)
    repos = {r["id"]: r for r in db.rows(conn, f"""SELECT r.id, r.name, r.full_name, r.url, r.default_branch
                                                    FROM repos r WHERE {' AND '.join(where)}""", params)}
    ids = list(repos)
    wfs = db.rows(conn, "SELECT * FROM workflows WHERE repo_id = ANY(%s)", (ids,))
    since = now - timedelta(days=days + 1) - MAX_DELAY
    # Runs in the window, plus each workflow's last few before it (to know whether it's failing now).
    runs = db.rows(conn, """
        SELECT * FROM workflow_runs WHERE repo_id = ANY(%s) AND created_at >= %s
        UNION
        SELECT * FROM (SELECT DISTINCT ON (workflow_id) * FROM workflow_runs
                        WHERE repo_id = ANY(%s) AND created_at < %s AND status = 'completed'
                        ORDER BY workflow_id, created_at DESC) x""", (ids, since, ids, since))
    out = build(wfs, runs, repos, now, days, tz)
    out["tz"] = str(tz)
    out["no_access"] = db.rows(conn, f"""SELECT name, full_name, workflows_error FROM repos r
                                          WHERE {' AND '.join(where)} AND workflows_error IS NOT NULL
                                          ORDER BY name""", params)
    out["unscanned"] = db.one(conn, f"""SELECT COUNT(*) AS n FROM repos r WHERE {' AND '.join(where)}
                                        AND NOT workflows_scanned AND workflows_error IS NULL""", params)["n"]
    return out
