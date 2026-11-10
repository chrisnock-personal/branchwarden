"""
rules.py
~~~~~~~~
What Branchwarden tells the channels, and when.

Event rules post as soon as a scan sees something happen. Reminder rules nudge
about PRs that have sat in one state too long, only during working hours, and
at most a set number of times. The digest goes out once a week with the
stale branches and those left behind by merged or closed PRs.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional

from psycopg2.extras import Json

import analysis
import data
import db
import notify
import settings

log = logging.getLogger("branchwarden.rules")


@dataclass(frozen=True)
class Rule:
    key: str
    kind: str            # event | assign | reminder | digest | post
    name: str
    defaults: dict
    enabled: bool = True


REMINDER_DEFAULTS = {"after": 24, "unit": "working_hours", "repeat": 24, "max": 3, "target": "routed",
                     "mention": True, "skip_label": "do-not-review", "group": False}

RULES = (
    Rule("opened", "event", "PR opened or ready for review", {"target": "routed", "mention": True}),
    Rule("review", "event", "Review submitted", {"target": "routed", "mention": True}),
    Rule("closed", "event", "Merged or closed", {"target": "routed", "mention": False}),
    Rule("assign", "assign", "Auto-assign reviewers",
         {"after": 1, "unit": "working_hours", "count": 1, "codeowners": True, "pool": "", "repos": "",
          "skip_label": "do-not-review", "target": "routed", "mention": True}, enabled=False),
    Rule("no_reviewer", "reminder", "No reviewer assigned",
         {**REMINDER_DEFAULTS, "after": 4, "max": 2, "mention": False}),
    Rule("waiting", "reminder", "Waiting for review", dict(REMINDER_DEFAULTS)),
    Rule("changes", "reminder", "Changes requested, no new commits",
         {**REMINDER_DEFAULTS, "after": 3, "unit": "days", "repeat": 2, "max": 2}),
    Rule("approved", "reminder", "Approved, not merged",
         {**REMINDER_DEFAULTS, "after": 2, "unit": "days", "repeat": 1, "max": 2}, enabled=False),
    Rule("digest", "digest", "Weekly stale-branch digest",
         {"day": "mon", "time": "09:00", "target": "all", "mention": True, "limit": 10}),
    Rule("mvp", "post", "Weekly MVP post",
         {"day": "fri", "time": "16:00", "target": "all", "mention": True}, enabled=False),
)
RULE_BY_KEY = {r.key: r for r in RULES}

UNITS = (("working_hours", "working hours"), ("hours", "hours"), ("days", "days"))


def seed(conn) -> None:
    for r in RULES:
        db.execute(conn, "INSERT INTO rules (key, enabled, params) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                   (r.key, r.enabled, Json(r.defaults)))


def load(conn) -> dict[str, dict]:
    stored = {r["key"]: r for r in db.rows(conn, "SELECT * FROM rules")}
    out = {}
    for r in RULES:
        row = stored.get(r.key) or {"enabled": r.enabled, "params": {}, "last_run_at": None}
        out[r.key] = {"key": r.key, "kind": r.kind, "name": r.name, "enabled": row["enabled"],
                      "params": {**r.defaults, **(row["params"] or {})}, "last_run_at": row.get("last_run_at")}
    return out


def describe(rule: dict, channel_names: dict[int, str]) -> str:
    p = rule["params"]
    if rule["kind"] == "event":
        return {"opened": "Posted straight away"
                          + (", with the requested reviewers @mentioned" if p.get("mention") else ""),
                "review": "Approved or changes requested: tells the author"
                          + (" with an @mention" if p.get("mention") else ""),
                "closed": "Merged, or closed without merging, noting a branch left behind"}[rule["key"]]
    if rule["kind"] == "assign":
        count = int(p.get("count", 1))
        source = (["code owners"] if p.get("codeowners") else []) + \
                 ([f"the least busy of {p['pool']}"] if p.get("pool") else ["the least busy recent reviewers"])
        where = f" in {p['repos']}" if p.get("repos") else ""
        return (f"After {_amount(p['after'], p['unit'])} with nobody asked{where}: requests {count} "
                f"reviewer{'s' if count != 1 else ''} on GitHub, from {' then '.join(source)}")
    if rule["kind"] == "post":
        return (f"{p['day'].capitalize()}days at {p['time']}: the last 7 days' MVPs for reviews, merges, "
                "branch prunes and PRs raised, with the top three of each")
    if rule["kind"] == "digest":
        return f"{p['day'].capitalize()}days at {p['time']}: stale branches and branches left after a PR, per repo"
    who = {"no_reviewer": "nudges the channel", "waiting": "reminds the requested reviewers",
           "changes": "reminds the author", "approved": "reminds the author to merge or close"}[rule["key"]]
    return (f"After {_amount(p['after'], p['unit'])}: {who}, then every {_amount(p['repeat'], p['unit'])}, "
            f"up to {p['max']} time{'s' if int(p['max']) != 1 else ''}")


def _amount(n, unit: str) -> str:
    n = int(n) if float(n).is_integer() else n
    word = {"working_hours": "working hour", "hours": "hour", "days": "day"}.get(unit, unit)
    return f"{n} {word}{'' if n == 1 else 's'}"


def target_label(target: str, channel_names: dict[int, str]) -> str:
    if target == "routed":
        return "Routed channels"
    if target == "all":
        return "All channels"
    return channel_names.get(int(target), "A removed channel") if str(target).isdigit() else target


def work_hours(values: dict) -> analysis.WorkHours:
    return analysis.WorkHours.from_settings(settings.get(values, "WORK_DAYS"), settings.get(values, "WORK_START"),
                                            settings.get(values, "WORK_END"), settings.get(values, "WORK_TZ"))


# ─── Messages ───────────────────────────────────────────────────────────────

def _link(values: dict, path: str) -> Optional[str]:
    base = settings.get(values, "PUBLIC_URL").rstrip("/")
    return f"{base}{path}" if base else None


def _facts(pr: dict) -> list:
    facts = [["Repository", pr["ref"]]]
    if pr.get("author"):
        facts.append(["Author", f"@{{{pr['author']}}}"])
    facts.append(["Branch", f"{pr['head_ref']} → {pr['base_ref']}"])
    ci = pr.get("ci_label")
    facts.append(["Checks", f"{ci} · +{pr['additions']} −{pr['deletions']}" if ci and ci != "none"
                  else f"+{pr['additions']} −{pr['deletions']}"])
    return facts


def _buttons(pr: dict, values: dict, snooze: bool = False) -> list:
    buttons = [["Open pull request", pr["url"]], ["Open in Branchwarden", _link(values, f"/pulls?assign={pr['id']}")]]
    if snooze:
        buttons.append(["Snooze", _link(values, f"/pulls?assign={pr['id']}#snooze")])
    return buttons


def _who(logins: list[str], mention: bool) -> str:
    return ", ".join(f"@{{{x}}}" if mention else x for x in logins)


def _pr_for_event(conn, pr_id: int, now: datetime) -> Optional[dict]:
    pr = db.one(conn, """
        SELECT p.*, r.full_name, r.name AS repo_name, r.owner AS repo_owner, r.url AS repo_url,
               r.default_branch, r.codeowners
          FROM pull_requests p JOIN repos r ON r.id = p.repo_id WHERE p.id = %s""", (pr_id,))
    return data.enrich_pr(pr, now) if pr else None


def event_message(conn, event: dict, pr: dict, params: dict, values: dict) -> Optional[dict]:
    mention = params.get("mention", True)
    kind = event["kind"]
    msg = {"title": pr["title"], "title_url": pr["url"], "facts": _facts(pr), "buttons": _buttons(pr, values),
           "pr": pr["ref"]}
    if kind in ("opened", "ready"):
        reviewers = pr["requested_users"]
        msg.update(heading="Ready for review" if kind == "ready" else "New pull request", tone="info")
        if reviewers or pr["requested_teams"]:
            msg["facts"].append(["Reviewers", ", ".join(filter(None, [_who(reviewers, mention),
                                                                       ", ".join(pr["requested_teams"])]))])
        else:
            msg["facts"].append(["Reviewers", "none yet"])
        return msg
    if kind == "review":
        r = event["review"]
        approved = r["state"] == "APPROVED"
        msg.update(heading=f"{'Approved' if approved else 'Changes requested'} by {r['login']}",
                   tone="good" if approved else "bad")
        author = f"@{{{pr['author']}}}" if mention and pr.get("author") else (pr.get("author") or "The author")
        msg["text"] = (f"{author}, {r['login']} approved this." if approved
                       else f"{author}, {r['login']} asked for changes.")
        return msg
    if kind in ("merged", "closed"):
        merged = kind == "merged"
        msg.update(heading="Merged" if merged else "Closed without merging", tone="good" if merged else "muted")
        msg["facts"] = [f for f in msg["facts"] if f[0] != "Checks"]
        if merged and pr.get("merged_by"):
            msg["facts"].append(["Merged by", pr["merged_by"]])
        left = db.one(conn, "SELECT 1 AS x FROM branches WHERE repo_id = %s AND name = %s",
                      (pr["repo_id"], pr["head_ref"]))
        if left and pr["head_ref"] != pr.get("default_branch"):
            msg["text"] = (f"The branch `{pr['head_ref']}` is still there: flagged as "
                           f"{'left after merge' if merged else 'closed, unmerged'}.")
        msg["buttons"] = [["Open pull request", pr["url"]]]
        return msg
    return None


def handle_events(conn, events: list[dict], values: dict, now: Optional[datetime] = None) -> int:
    """Queue messages for what the scan saw happen. Returns how many were queued."""
    now = now or datetime.now(timezone.utc)
    rules = load(conn)
    queued = 0
    for ev in events:
        key = {"opened": "opened", "ready": "opened", "review": "review",
               "merged": "closed", "closed": "closed"}.get(ev["kind"])
        rule = rules.get(key)
        if not rule or not rule["enabled"]:
            continue
        pr = _pr_for_event(conn, ev["pr_id"], now)
        if not pr:
            continue
        msg = event_message(conn, ev, pr, rule["params"], values)
        if not msg:
            continue
        chans = notify.channels_for(conn, str(rule["params"].get("target", "routed")), pr["full_name"],
                                    pr["requested_teams"])
        queued += notify.enqueue(conn, chans, ev["kind"], msg, pr["id"])
    return queued


# ─── Reminders ──────────────────────────────────────────────────────────────

def reminder_since(key: str, pr: dict) -> Optional[datetime]:
    """When the state this reminder is about began, or None if it doesn't apply."""
    state = pr["state_key"]
    since = pr["since"]
    if key == "waiting":
        pending = [x for x in pr["requested_users"]] + pr["requested_teams"]
        return since if state in ("awaiting", "in_review") and pending else None
    if key == "no_reviewer":
        return since if state == "no_reviewer" else None
    if key == "changes":
        if state != "changes":
            return None
        last = analysis.parse_time(pr.get("last_commit_at"))
        return since if last is None or last <= since else None
    if key == "approved":
        return since if state == "approved" else None
    return None


def reminder_line(key: str, pr: dict, mention: bool) -> tuple[str, str]:
    """(heading, text) for one PR."""
    waited = pr["waiting"]
    author = (f"@{{{pr['author']}}}" if mention else pr["author"]) if pr.get("author") else "The author"
    if key == "waiting":
        who = _who(pr["requested_users"], mention)
        if pr["requested_teams"]:
            who = ", ".join(filter(None, [who, ", ".join(pr["requested_teams"])]))
        return f"Waiting for review · {waited}", f"{who}: this is waiting for your review."
    if key == "no_reviewer":
        return f"No reviewer · {waited}", "Nobody has been asked to review this yet."
    if key == "changes":
        reviewer = (analysis.latest_review(pr, "CHANGES_REQUESTED") or {}).get("login", "a reviewer")
        return (f"Changes requested · {waited}",
                f"{author}, {reviewer} asked for changes {waited} ago and there are no new commits yet.")
    return f"Approved, not merged · {waited}", f"{author}, this was approved {waited} ago: merge or close it?"


def run_reminders(conn, values: dict, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    hours = work_hours(values)
    if not hours.is_open(now):
        return 0
    rules = [r for r in load(conn).values() if r["kind"] == "reminder" and r["enabled"]]
    if not rules:
        return 0
    snoozed = {r["pr_id"] for r in db.rows(conn, "SELECT pr_id FROM snoozes WHERE until > %s", (now,))}
    prs = [p for p in data.open_prs(conn, now=now) if p["id"] not in snoozed]
    sent_before = {(r["rule"], r["pr_id"]): r for r in db.rows(conn, "SELECT * FROM reminders")}
    queued = 0
    grouped: dict = defaultdict(list)     # (rule key, channel id) -> [(pr, text)]
    channel_by_id: dict = {}
    for rule in rules:
        p = rule["params"]
        live = set()
        for pr in prs:
            if pr["draft"] or (p.get("skip_label") and p["skip_label"] in (pr.get("labels") or [])):
                continue
            since = reminder_since(rule["key"], pr)
            if since is None:
                continue
            live.add(pr["id"])
            rec = sent_before.get((rule["key"], pr["id"]))
            count, last_at = (rec["count"], rec["last_at"]) if rec and rec["since"] == since else (0, None)
            unit = p.get("unit", "working_hours")
            if analysis.elapsed(since, now, unit, hours) < float(p.get("after", 24)):
                continue
            if count >= int(p.get("max", 3)):
                continue
            if last_at and analysis.elapsed(last_at, now, unit, hours) < float(p.get("repeat", 24)):
                continue
            chans = notify.channels_for(conn, str(p.get("target", "routed")), pr["full_name"], pr["requested_teams"])
            if not chans:
                continue
            heading, text = reminder_line(rule["key"], pr, p.get("mention", True))
            if p.get("group"):
                for c in chans:
                    grouped[(rule["key"], c["id"])].append((pr, heading, text))
                    channel_by_id[c["id"]] = c
            else:
                msg = {"heading": heading, "tone": "bad" if rule["key"] == "changes" else "warn",
                       "title": pr["title"], "title_url": pr["url"], "facts": _facts(pr), "text": text,
                       "buttons": _buttons(pr, values, snooze=True), "pr": pr["ref"]}
                queued += notify.enqueue(conn, chans, f"reminder:{rule['key']}", msg, pr["id"])
            db.execute(conn, """
                INSERT INTO reminders (rule, pr_id, since, count, last_at) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (rule, pr_id) DO UPDATE SET since = EXCLUDED.since, count = EXCLUDED.count,
                    last_at = EXCLUDED.last_at""", (rule["key"], pr["id"], since, count + 1, now))
        # Forget reminders for PRs that have left this state, so a return to it starts afresh.
        db.execute(conn, "DELETE FROM reminders WHERE rule = %s AND NOT (pr_id = ANY(%s))",
                   (rule["key"], list(live | snoozed)))
    for (key, cid), items in grouped.items():
        rule = RULE_BY_KEY[key]
        lines = [{"link": pr["url"], "label": pr["ref"], "text": f"{pr['title']}: {text}"} for pr, _, text in items]
        msg = {"heading": f"{rule.name}: {len(items)} pull request{'s' if len(items) != 1 else ''}",
               "tone": "warn", "sections": [{"title": rule.name, "lines": lines}],
               "buttons": [["Open in Branchwarden", _link(values, "/pulls")]]}
        queued += notify.enqueue(conn, [channel_by_id[cid]], f"reminder:{key}", msg)
    return queued


# ─── Digest ─────────────────────────────────────────────────────────────────

def digest_due(rule: dict, now: datetime, hours: analysis.WorkHours) -> bool:
    p = rule["params"]
    local = now.astimezone(hours.tz)
    day = analysis.DAYS.index(p.get("day", "mon")[:3].lower()) if p.get("day", "mon")[:3].lower() in analysis.DAYS else 0
    try:
        hh, mm = (int(x) for x in str(p.get("time", "09:00")).split(":"))
        at = time(hh, mm)
    except ValueError:
        at = time(9)
    if local.weekday() != day or local.time() < at:
        return False
    last = rule.get("last_run_at")
    return not (last and last.astimezone(hours.tz).date() == local.date())


def digest_message(branches: list[dict], values: dict, mention: bool, limit: int) -> Optional[dict]:
    stale = [b for b in branches if b["status"] == "stale"]
    left = [b for b in branches if b["status"] in ("left_merged", "left_closed", "pushed_after_merge")]
    if not stale and not left:
        return None
    stale_days = settings.number(values, "STALE_DAYS", 30)
    repos = sorted({b["repo_name"] for b in stale + left})
    sections = []
    if stale:
        lines = []
        for b in sorted(stale, key=lambda b: b["age_days"] or 0, reverse=True)[:limit]:
            who = b.get("last_committer") or ""
            who = (f"@{{{who}}}" if mention and who and " " not in who else who)
            lines.append({"code": b["name"], "text": f"{b['repo_name']} · {b['age_days']} days" + (f" · {who}" if who else "")})
        sections.append({"title": f"Stale, no pull request (idle over {stale_days} days)", "lines": lines,
                         "more": f"+ {len(stale) - limit} more" if len(stale) > limit else ""})
    if left:
        lines = []
        for b in left[:limit]:
            what = "merged" if b["pr_state"] == "MERGED" else "closed"
            when = b["pr_merged_at"] or b["pr_closed_at"]
            lines.append({"code": b["name"], "text": f"{b['repo_name']} · #{b['pr_number']} {what} "
                                                     f"{analysis.ago(when)} ago"
                                                     + (" · pushed to since" if b["status"] == "pushed_after_merge" else "")})
        sections.append({"title": "Left after a pull request (delete them on GitHub)", "lines": lines,
                         "more": f"+ {len(left) - limit} more" if len(left) > limit else ""})
    shown = ", ".join(repos[:4]) + (f" and {len(repos) - 4} more" if len(repos) > 4 else "")
    return {"heading": f"Weekly branch tidy-up: {shown}", "tone": "muted", "sections": sections,
            "buttons": [["See all in Branchwarden", _link(values, "/branches")]]}


def run_digest(conn, values: dict, now: Optional[datetime] = None, force: bool = False) -> int:
    now = now or datetime.now(timezone.utc)
    rule = load(conn)["digest"]
    if not rule["enabled"] and not force:
        return 0
    if not force and not digest_due(rule, now, work_hours(values)):
        return 0
    p = rule["params"]
    queued = 0
    repos = db.rows(conn, "SELECT id, full_name FROM repos WHERE included")
    for c in notify.channels_for(conn, str(p.get("target", "all"))):
        patterns = settings.split_list(c["repos"])
        ids = [r["id"] for r in repos if not patterns or analysis.matches(r["full_name"], patterns)]
        if not ids:
            continue
        msg = digest_message(data.classified_branches(conn, values, now=now, repo_ids=ids), values,
                             p.get("mention", True), int(p.get("limit", 10)))
        if msg:
            queued += notify.enqueue(conn, [c], "digest", msg)
    db.execute(conn, "UPDATE rules SET last_run_at = %s WHERE key = 'digest'", (now,))
    return queued


# ─── Auto-assigning reviewers ───────────────────────────────────────────────

def _expand(client, entries: list[str], cache: dict) -> list[str]:
    """Logins, with org/team slugs replaced by the team's members."""
    out: list[str] = []
    for e in entries:
        e = e.strip().lstrip("@").lower()
        if not e:
            continue
        if "/" in e:
            if e not in cache:
                try:
                    cache[e] = client.team_members(e)
                except Exception as exc:  # GitHubError, or no team access
                    log.info("Can't list members of %s: %s", e, exc)
                    cache[e] = []
            out.extend(cache[e])
        else:
            out.append(e)
    return out


def assign_candidates(conn, pr: dict, p: dict, client, cache: dict) -> list[tuple[str, list[str]]]:
    groups: list[tuple[str, list[str]]] = []
    if p.get("codeowners"):
        owners = analysis.owners_for(pr.get("files") or [], pr.get("codeowners") or "")
        if owners:
            groups.append(("a code owner of the files changed", _expand(client, owners, cache)))
    pool = settings.split_list(p.get("pool", ""))
    if pool:
        groups.append(("the least busy in the reviewer pool", _expand(client, pool, cache)))
    else:
        recent = db.rows(conn, """SELECT DISTINCT r->>'login' AS login FROM pull_requests x, jsonb_array_elements(x.reviews) r
                                   WHERE x.repo_id = %s AND x.created_at > NOW() - INTERVAL '120 days'""", (pr["repo_id"],))
        groups.append(("the least busy recent reviewer here", [r["login"] for r in recent]))
    return groups


def run_auto_assign(conn, values: dict, now: Optional[datetime] = None, connections=None) -> int:
    """Request reviewers for PRs nobody has been asked to review. Returns how many PRs it assigned."""
    from github import Connections, GitHubError
    now = now or datetime.now(timezone.utc)
    rule = load(conn)["assign"]
    if not rule["enabled"]:
        return 0
    hours = work_hours(values)
    if not hours.is_open(now):
        return 0
    p = rule["params"]
    repos = settings.split_list(p.get("repos", ""))
    tried = {r["pr_id"]: r for r in db.rows(conn, "SELECT * FROM reminders WHERE rule = 'assign'")}
    load_by = {r["login"]: r["n"] for r in db.rows(conn, """
        SELECT x->>'user' AS login, COUNT(*) AS n FROM pull_requests pr, jsonb_array_elements(pr.requested) x
         WHERE pr.state = 'open' AND x ? 'user' GROUP BY 1""")}
    recent = {r["reviewer"]: r["n"] for r in db.rows(conn, """
        SELECT reviewer, COUNT(*) AS n FROM assignments WHERE assigned_at > NOW() - INTERVAL '14 days' GROUP BY 1""")}
    conns = connections or Connections(values)
    cache: dict = {}
    done = 0
    for pr in data.open_prs(conn, now=now):
        if pr["draft"] or pr["state_key"] != "no_reviewer":
            continue
        if p.get("skip_label") and p["skip_label"] in (pr.get("labels") or []):
            continue
        if repos and not analysis.matches(pr["full_name"], repos):
            continue
        prev = tried.get(pr["id"])
        if prev and prev["since"] == pr["since"]:
            continue                 # one go per time the PR is in this state
        if analysis.elapsed(pr["since"], now, p.get("unit", "working_hours"), hours) < float(p.get("after", 1)):
            continue
        db.execute(conn, """INSERT INTO reminders (rule, pr_id, since, count, last_at) VALUES ('assign', %s, %s, 1, %s)
                            ON CONFLICT (rule, pr_id) DO UPDATE SET since = EXCLUDED.since, count = 1,
                                last_at = EXCLUDED.last_at""", (pr["id"], pr["since"], now))
        try:
            client = conns.client_for(pr["target_login"], pr["target_via"])
            groups = assign_candidates(conn, pr, p, client, cache)
            chosen = analysis.choose_reviewers(groups, count=int(p.get("count", 1)),
                                               exclude={(pr.get("author") or "").lower()}, load=load_by, recent=recent)
            if not chosen:
                log.info("Auto-assign: nobody to ask for %s", pr["ref"])
                continue
            client.request_reviewers(pr["full_name"], pr["number"], [c for c, _ in chosen], [])
        except GitHubError as exc:
            log.warning("Auto-assign for %s failed: %s", pr["ref"], exc)
            continue
        requested = list(pr.get("requested") or []) + [{"user": c} for c, _ in chosen]
        db.execute(conn, "UPDATE pull_requests SET requested = %s, review_requested_at = %s WHERE id = %s",
                   (Json(requested), now, pr["id"]))
        for c, _ in chosen:
            db.execute(conn, "INSERT INTO assignments (pr_id, reviewer, by_rule, by_name) VALUES (%s, %s, TRUE, %s)",
                       (pr["id"], c, "Branchwarden"))
            load_by[c] = load_by.get(c, 0) + 1
            recent[c] = recent.get(c, 0) + 1
        db.request_scan(conn, pr["full_name"], "reviewers auto-assigned")
        mention = p.get("mention", True)
        who = "; ".join(f"{'@{' + c + '}' if mention else c} ({why})" for c, why in chosen)
        pr = data.enrich_pr({**pr, "requested": requested}, now)
        msg = {"heading": "Reviewer assigned", "tone": "info", "title": pr["title"], "title_url": pr["url"],
               "facts": _facts(pr), "text": f"Branchwarden asked {who} to review this.",
               "buttons": _buttons(pr, values), "pr": pr["ref"]}
        chans = notify.channels_for(conn, str(p.get("target", "routed")), pr["full_name"], pr["requested_teams"])
        notify.enqueue(conn, chans, "assigned", msg, pr["id"])
        done += 1
    return done


# ─── Weekly MVP post ────────────────────────────────────────────────────────

def run_mvp_post(conn, values: dict, now: Optional[datetime] = None, force: bool = False) -> int:
    import mvp
    now = now or datetime.now(timezone.utc)
    rule = load(conn)["mvp"]
    if not force and (not rule["enabled"] or not digest_due(rule, now, work_hours(values))):
        return 0
    p = rule["params"]
    msg = mvp.post_message(mvp.compute(conn, "this", now), _link(values, "/mvp"), p.get("mention", True))
    queued = notify.enqueue(conn, notify.channels_for(conn, str(p.get("target", "all"))), "mvp", msg) if msg else 0
    db.execute(conn, "UPDATE rules SET last_run_at = %s WHERE key = 'mvp'", (now,))
    return queued
