"""
analysis.py
~~~~~~~~~~~
The pure logic: what state a pull request is in, whether a branch is stale or
was left behind by a merged PR, who owns which files (CODEOWNERS), and how
much working time has passed. No database or network here, so it's easy to test.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ─── Name patterns ──────────────────────────────────────────────────────────


def matches(name: str, patterns: Iterable[str]) -> bool:
    """Shell-style match, case-insensitive. A pattern with a slash is matched
    against the whole name; one without also against the part after the last
    slash (so "api-*" matches "my-org/api-gateway")."""
    low = name.lower()
    for p in patterns:
        p = p.strip().lower()
        if not p:
            continue
        if fnmatch.fnmatchcase(low, p):
            return True
        if "/" not in p and "/" in low and fnmatch.fnmatchcase(low.rsplit("/", 1)[1], p):
            return True
    return False


def repo_included(full_name: str, *, archived: bool, fork: bool, include: list[str],
                  exclude: list[str], with_archived: bool, with_forks: bool) -> bool:
    if archived and not with_archived:
        return False
    if fork and not with_forks:
        return False
    if include and not matches(full_name, include):
        return False
    return not matches(full_name, exclude)


# ─── Pull requests ──────────────────────────────────────────────────────────

STATES = {
    # key: (label, tone)
    "draft": ("Draft", "muted"),
    "conflicts": ("Conflicts", "bad"),
    "changes": ("Changes requested", "bad"),
    "approved": ("Approved", "good"),
    "no_reviewer": ("No reviewer", "muted"),
    "in_review": ("In review", "info"),
    "awaiting": ("Awaiting review", "warn"),
}

FILTERS = (("all", "All"), ("review", "Needs review"), ("changes", "Changes requested"),
           ("approved", "Approved"), ("ci", "CI failing"), ("conflicts", "Conflicts"),
           ("draft", "Drafts"))

CI_LABELS = {"SUCCESS": ("passing", "good"), "FAILURE": ("failing", "bad"), "ERROR": ("failing", "bad"),
             "PENDING": ("pending", "warn"), "EXPECTED": ("pending", "warn")}


def parse_time(value) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def requested_users(pr: dict) -> list[str]:
    return [r["user"] for r in pr.get("requested") or [] if r.get("user")]


def requested_teams(pr: dict) -> list[str]:
    return [r["team"] for r in pr.get("requested") or [] if r.get("team")]


def latest_review(pr: dict, state: str) -> Optional[dict]:
    found = [r for r in pr.get("reviews") or [] if r.get("state") == state]
    return max(found, key=lambda r: r["at"]) if found else None


def pr_state(pr: dict) -> str:
    """One headline state for a PR, the most pressing first."""
    if pr.get("draft"):
        return "draft"
    if pr.get("mergeable") == "CONFLICTING":
        return "conflicts"
    decision = pr.get("review_decision")
    if decision == "CHANGES_REQUESTED":
        return "changes"
    if decision == "APPROVED":
        return "approved"
    reviews = [r for r in pr.get("reviews") or [] if r.get("state") in ("APPROVED", "CHANGES_REQUESTED",
                                                                         "COMMENTED")]
    if not pr.get("requested") and not reviews:
        return "no_reviewer"
    if reviews:
        return "in_review"
    return "awaiting"


def pr_categories(pr: dict) -> set[str]:
    """Every filter a PR belongs to (a PR can be approved and failing CI)."""
    state = pr_state(pr)
    cats = set()
    if state in ("awaiting", "no_reviewer", "in_review"):
        cats.add("review")
    if state in ("draft", "conflicts", "changes", "approved"):
        cats.add(state)
    if pr.get("mergeable") == "CONFLICTING":
        cats.add("conflicts")
    if pr.get("review_decision") == "APPROVED":
        cats.add("approved")
    if pr.get("ci_state") in ("FAILURE", "ERROR"):
        cats.add("ci")
    return cats


def state_since(pr: dict) -> datetime:
    """When the PR entered its current state, as far as we can tell."""
    created = parse_time(pr["created_at"])
    ready = parse_time(pr.get("ready_at")) or created
    state = pr_state(pr)
    if state == "changes":
        r = latest_review(pr, "CHANGES_REQUESTED")
        if r:
            return parse_time(r["at"])
    if state == "approved":
        r = latest_review(pr, "APPROVED")
        if r:
            return parse_time(r["at"])
    if state in ("awaiting", "in_review"):
        requested = parse_time(pr.get("review_requested_at"))
        return max(ready, requested) if requested else ready
    return ready


def waiting_on(pr: dict) -> str:
    """Who has to act next, in a few words."""
    state = pr_state(pr)
    if state in ("draft", "conflicts", "changes", "approved"):
        return pr.get("author") or "the author"
    if state == "no_reviewer":
        return "a reviewer"
    names = requested_users(pr) + requested_teams(pr)
    return ", ".join(names) if names else "reviewers"


# ─── Branches ───────────────────────────────────────────────────────────────

BRANCH_STATUS = {
    "stale": ("Stale", "warn"),
    "active": ("Active", "good"),
    "left_merged": ("Left after merge", "warn"),
    "pushed_after_merge": ("Pushed after merge", "info"),
    "left_closed": ("Closed, unmerged", "bad"),
    "open_pr": ("Open PR", "info"),
}


def branch_status(branch: dict, *, default_branch: Optional[str], never_flag: list[str],
                  ignored: bool, stale_days: int, now: datetime) -> Optional[str]:
    """None when the branch isn't flagged at all (default, protected, never-flag
    pattern, ignored); otherwise a BRANCH_STATUS key."""
    name = branch["name"]
    if name == default_branch or branch.get("protected") or ignored or matches(name, never_flag):
        return None
    pr = branch.get("pr_state")
    if pr == "OPEN":
        return "open_pr"
    if pr == "MERGED":
        if branch.get("pr_head_oid") and branch.get("head_oid") and branch["pr_head_oid"] != branch["head_oid"]:
            return "pushed_after_merge"
        return "left_merged"
    if pr == "CLOSED":
        return "left_closed"
    last = parse_time(branch.get("last_commit_at"))
    if last and now - last >= timedelta(days=stale_days):
        return "stale"
    return "active"


# ─── CODEOWNERS ─────────────────────────────────────────────────────────────

def _owner_regex(pattern: str) -> re.Pattern:
    """A CODEOWNERS pattern (gitignore rules, mostly) as a regex on a path."""
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    pat = pattern.lstrip("/")
    directory = pat.endswith("/")
    pat = pat.rstrip("/")
    out = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pat.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    body = "".join(out)
    prefix = "^" if anchored else "^(?:.*/)?"
    # A name also matches everything under it, as a directory.
    suffix = "/.*$" if directory else "(?:/.*)?$"
    return re.compile(prefix + body + suffix)


def parse_codeowners(text: str) -> list[tuple[re.Pattern, list[str]]]:
    rules = []
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        owners = [o.lstrip("@").lower() for o in parts[1:] if o.startswith("@")]
        try:
            rules.append((_owner_regex(parts[0]), owners))
        except re.error:
            continue
    return rules


def owners_for(paths: Iterable[str], codeowners: str) -> list[str]:
    """Owners (logins and org/team slugs) of the given paths: for each path
    the last matching rule wins, as on GitHub."""
    rules = parse_codeowners(codeowners)
    found: list[str] = []
    for path in paths:
        hit: list[str] = []
        for regex, owners in rules:
            if regex.match(path):
                hit = owners
        for o in hit:
            if o not in found:
                found.append(o)
    return found


# ─── Working hours ──────────────────────────────────────────────────────────

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class WorkHours:
    days: frozenset          # weekday numbers, Monday = 0
    start: time
    end: time
    tz: ZoneInfo

    @classmethod
    def from_settings(cls, days: str, start: str, end: str, tz: str) -> "WorkHours":
        nums = frozenset(DAYS.index(d.strip().lower()[:3]) for d in days.split(",")
                         if d.strip().lower()[:3] in DAYS)
        try:
            zone = ZoneInfo(tz or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo("UTC")
        return cls(nums or frozenset(range(5)), _hhmm(start, time(9)), _hhmm(end, time(17, 30)), zone)

    def _window(self, day: date) -> Optional[tuple[datetime, datetime]]:
        if day.weekday() not in self.days:
            return None
        a = datetime.combine(day, self.start, self.tz)
        b = datetime.combine(day, self.end, self.tz)
        return (a, b) if b > a else None

    def is_open(self, when: datetime) -> bool:
        w = self._window(when.astimezone(self.tz).date())
        return bool(w and w[0] <= when < w[1])

    def seconds_between(self, start: datetime, end: datetime) -> float:
        """Working seconds from start to end (0 if end is before start)."""
        if end <= start:
            return 0.0
        total = 0.0
        day = start.astimezone(self.tz).date()
        last = end.astimezone(self.tz).date()
        if (last - day).days > 400:      # don't loop for ever on silly input
            day = last - timedelta(days=400)
        while day <= last:
            w = self._window(day)
            if w:
                a, b = max(w[0], start), min(w[1], end)
                if b > a:
                    total += (b - a).total_seconds()
            day += timedelta(days=1)
        return total


def _hhmm(value: str, fallback: time) -> time:
    try:
        h, m = value.strip().split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return fallback


def elapsed(start: datetime, end: datetime, unit: str, hours: WorkHours) -> float:
    """Time from start to end in `unit`: working_hours, hours or days."""
    if unit == "working_hours":
        return hours.seconds_between(start, end) / 3600
    seconds = max(0.0, (end - start).total_seconds())
    return seconds / 86400 if unit == "days" else seconds / 3600


# ─── Display ────────────────────────────────────────────────────────────────

def ago(when: Optional[datetime], now: Optional[datetime] = None) -> str:
    """'3 days', '26 h', '12 min'."""
    if not when:
        return "—"
    now = now or datetime.now(timezone.utc)
    seconds = max(0, (now - when).total_seconds())
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} min"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)} h"
    return f"{int(seconds // 86400)} days"


# ─── Picking reviewers ──────────────────────────────────────────────────────

def choose_reviewers(groups: list[tuple[str, list[str]]], *, count: int, exclude: set,
                     load: dict, recent: dict) -> list[tuple[str, str]]:
    """Pick `count` people, taking each group in turn (code owners first, then
    the pool). Within a group, the least busy wins: fewest open review requests,
    then fewest recent assignments, then name order so the choice is stable.
    Returns [(login, why)]."""
    picked: list[tuple[str, str]] = []
    taken = set(exclude)
    for why, people in groups:
        ranked = sorted({p.lower() for p in people if p and p.lower() not in taken},
                        key=lambda p: (load.get(p, 0), recent.get(p, 0), p))
        for p in ranked:
            if len(picked) >= count:
                return picked
            picked.append((p, why))
            taken.add(p)
    return picked
