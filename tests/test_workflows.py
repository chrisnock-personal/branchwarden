import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from jinja2 import Environment, FileSystemLoader

import rules
import workflows as wf

UTC = timezone.utc


def t(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def run(rid, created, conclusion="success", event="schedule", status="completed", branch="main", wid=1, **kw):
    created = t(created) if isinstance(created, str) else created
    return {"id": rid, "workflow_id": wid, "event": event, "status": status, "conclusion": conclusion,
            "head_branch": branch, "created_at": created, "started_at": created,
            "updated_at": created + timedelta(minutes=3), "run_number": rid, "run_attempt": 1,
            "actor": "dana", "url": f"https://github.com/o/r/actions/runs/{rid}", "head_sha": "abcdef1234", **kw}


# ─── Cron ───────────────────────────────────────────────────────────────────

def test_cron_fields():
    c = wf.parse_cron("*/15 3,4 1-10 JAN-MAR mon-fri")
    assert c.minutes == {0, 15, 30, 45} and c.hours == {3, 4}
    assert c.days == set(range(1, 11)) and c.months == {1, 2, 3} and c.weekdays == {1, 2, 3, 4, 5}
    assert wf.parse_cron("0 0 * * 7").weekdays == {0}
    assert wf.parse_cron("5/20 * * * *").minutes == {5, 25, 45}


@pytest.mark.parametrize("bad", ["", "* * * *", "61 * * * *", "* * * * mon-xyz", "*/0 * * * *", "5-1 * * * *"])
def test_cron_rejects(bad):
    with pytest.raises(wf.CronError):
        wf.parse_cron(bad)


def test_cron_times_daily_and_weekdays():
    c = wf.parse_cron("30 2 * * *")
    times = wf.cron_times(c, t("2026-11-10T00:00:00"), t("2026-11-13T00:00:00"))
    assert times == [t("2026-11-10T02:30:00"), t("2026-11-11T02:30:00"), t("2026-11-12T02:30:00")]
    # 2026-11-14 is a Saturday, 15 a Sunday.
    weekdays = wf.cron_times(wf.parse_cron("0 9 * * 1-5"), t("2026-11-13T00:00:00"), t("2026-11-17T00:00:00"))
    assert [x.day for x in weekdays] == [13, 16]


def test_cron_day_of_month_or_weekday():
    # Both restricted: either matches (standard cron).
    c = wf.parse_cron("0 0 1 * 1")
    days = [x.day for x in wf.cron_times(c, t("2026-11-01T00:00:00"), t("2026-11-10T00:00:00"))]
    assert days == [1, 2, 9]          # the 1st, and Mondays 2 and 9


def test_next_due_and_describe():
    c = wf.parse_cron("0 3 * * *")
    assert wf.next_due([c], t("2026-11-15T03:00:00")) == t("2026-11-16T03:00:00")
    # 29 February is more than the year ahead it looks.
    assert wf.next_due([wf.parse_cron("0 0 29 2 *")], t("2026-11-15T00:00:00")) is None
    assert wf.describe_cron("0 3 * * *") == "daily at 03:00 UTC"
    assert wf.describe_cron("15 6 * * 1-5") == "weekdays at 06:15 UTC"
    assert wf.describe_cron("0 4 * * 0") == "Sundays at 04:00 UTC"
    assert wf.describe_cron("7 * * * *") == "hourly at :07"
    assert wf.describe_cron("*/5 * * * *") == "*/5 * * * *"


# ─── Workflow files ─────────────────────────────────────────────────────────

def test_parse_workflow_on_key_read_as_true():
    text = """
name: Nightly
on:
  schedule:
    - cron: '0  3 * * *'
    - cron: "30 12 * * 1-5"
  workflow_dispatch:
jobs: {}
"""
    p = wf.parse_workflow(text)
    assert p["triggers"] == ["schedule", "workflow_dispatch"]
    assert p["crons"] == ["0 3 * * *", "30 12 * * 1-5"] and p["error"] is None


@pytest.mark.parametrize("text,triggers", [("on: push", ["push"]), ("on: [push, pull_request]", ["push", "pull_request"]),
                                           ("name: x", [])])
def test_parse_workflow_shapes(text, triggers):
    assert wf.parse_workflow(text)["triggers"] == triggers


def test_parse_workflow_bad_yaml_and_bad_cron():
    assert "YAML" in wf.parse_workflow("on: [push\n")["error"]
    p = wf.parse_workflow("on:\n  schedule:\n    - cron: '99 * * * *'\n")
    assert p["crons"] == ["99 * * * *"] and "out of range" in p["error"]
    assert wf.good_crons(p["crons"]) == []


# ─── Matching runs to due times ─────────────────────────────────────────────

def test_match_slots_late_dropped_and_due():
    slots = [t("2026-11-10T03:00:00"), t("2026-11-11T03:00:00"), t("2026-11-12T03:00:00"), t("2026-11-13T03:00:00")]
    runs = [run(1, "2026-11-10T03:41:00"),                         # late but answered
            run(2, "2026-11-12T03:05:00", conclusion="failure")]   # the 11th never ran
    now = t("2026-11-13T03:30:00")                                 # the 13th may still start
    states = [s["state"] for s in wf.match_slots(slots, runs, now)]
    assert states == ["passed", "missed", "failed", "due"]


def test_match_slots_frequent_and_delayed_keeps_counts():
    slots = [t("2026-11-10T00:00:00") + timedelta(minutes=15 * i) for i in range(8)]
    # Every run 20 minutes late, and one dropped.
    runs = [run(i, slots[i] + timedelta(minutes=20)) for i in range(8) if i != 3]
    out = wf.match_slots(slots, runs, t("2026-11-10T06:00:00"))
    assert sum(s["state"] == "missed" for s in out) == 1
    assert sum(s["state"] == "passed" for s in out) == 7


def test_match_slots_hours_late_like_a_quiet_repo():
    # Seen on a real repo: a 03:17 UTC nightly that GitHub starts 6-8 hours late, every day.
    slots = [t("2026-11-01T03:17:00") + timedelta(days=i) for i in range(14)]
    late = [7.1, 6.9, 7.7, 8.1, 7.5, 7.6, 7.6, 7.8, 7.4, 7.6, 7.0, 6.9, 7.3]
    runs = [run(i, slots[i] + timedelta(hours=h), conclusion="failure") for i, h in enumerate(late)]
    now = slots[13] + timedelta(hours=9)        # today's run hasn't come yet, 9 hours on
    out = wf.match_slots(slots, runs, now)
    assert [s["state"] for s in out] == ["failed"] * 13 + ["due"]
    assert timedelta(hours=11) < out[0]["grace"] < timedelta(hours=13)
    assert wf.usual_delay(out) > timedelta(hours=7)
    # Much later still, it's missed.
    assert wf.match_slots(slots, runs, slots[13] + timedelta(hours=14))[-1]["state"] == "missed"


def test_grace_for():
    assert wf.grace_for([]) == wf.MIN_GRACE
    assert wf.grace_for([timedelta(minutes=3)] * 5) == wf.MIN_GRACE
    assert wf.grace_for([timedelta(hours=2)] * 5) == timedelta(hours=4)
    assert wf.grace_for([timedelta(hours=20)]) == wf.MAX_DELAY


def test_match_slots_ignores_runs_before_their_time():
    slots = [t("2026-11-10T03:00:00")]
    out = wf.match_slots(slots, [run(1, "2026-11-10T02:00:00")], t("2026-11-10T09:00:00"))
    assert out[0]["state"] == "missed"


def test_outcome():
    assert wf.outcome({"status": "in_progress"}) == "running"
    assert wf.outcome({"status": "queued"}) == "running"
    assert wf.outcome({"status": "waiting"}) == "waiting"
    assert wf.outcome({"status": "completed", "conclusion": "timed_out"}) == "failed"
    assert wf.outcome({"status": "completed", "conclusion": "skipped"}) == "cancelled"
    assert wf.outcome({"status": "completed", "conclusion": "neutral"}) == "passed"


# ─── Failing and fixed ──────────────────────────────────────────────────────

def test_failing_transitions():
    a = run(10, "2026-11-10T10:00:00", conclusion="failure", event="push")
    b = run(11, "2026-11-10T11:00:00", conclusion="failure", event="push")
    c = run(12, "2026-11-10T12:00:00", conclusion="success", event="push")
    prev = {10: "passed", 11: "failed", 12: "failed"}
    events = wf.failing_transitions([(c, None), (a, None), (b, None)], lambda r: prev[r["id"]])
    assert [(e["kind"], e["run_id"]) for e in events] == [("wf_failed", 10), ("wf_fixed", 12)]


def test_failing_transitions_rerun_compares_with_itself():
    rerun = run(20, "2026-11-10T10:00:00", conclusion="success", run_attempt=2)
    old = {"status": "completed", "conclusion": "failure", "prior_conclusion": None}
    events = wf.failing_transitions([(rerun, old)], lambda r: "passed")
    assert [e["kind"] for e in events] == ["wf_fixed"]
    # A re-run in progress cleared the conclusion; its prior one still counts.
    old = {"status": "in_progress", "conclusion": None, "prior_conclusion": "failure"}
    again = run(21, "2026-11-10T10:00:00", conclusion="failure", run_attempt=2)
    assert wf.failing_transitions([(again, old)], lambda r: "passed") == []


# ─── Page data ──────────────────────────────────────────────────────────────

def _page(now, days=7, state="active", state_since=None):
    repos = {1: {"id": 1, "name": "api", "full_name": "o/api", "url": "https://github.com/o/api",
                 "default_branch": "main"}}
    flows = [
        {"id": 1, "repo_id": 1, "name": "Nightly", "path": ".github/workflows/nightly.yml", "state": state,
         "url": "u", "triggers": ["schedule"], "crons": ["0 3 * * *"], "file_error": None,
         "crons_since": t("2026-10-01T00:00:00"), "state_since": state_since or t("2026-10-01T00:00:00")},
        {"id": 2, "repo_id": 1, "name": "CI", "path": ".github/workflows/ci.yml", "state": "active", "url": "u",
         "triggers": ["push", "pull_request"], "crons": [], "file_error": None,
         "crons_since": t("2026-10-01T00:00:00"), "state_since": t("2026-10-01T00:00:00")},
    ]
    runs = [run(1, "2026-11-10T03:10:00"), run(2, "2026-11-12T03:04:00", conclusion="failure"),
            run(3, "2026-11-13T03:02:00", conclusion="failure"), run(4, "2026-11-14T03:01:00", conclusion="failure"),
            run(5, "2026-11-14T09:00:00", event="push", wid=2), run(6, "2026-11-14T10:00:00", event="push", wid=2,
                                                                     conclusion="failure", branch="feat/x"),
            run(7, "2026-11-01T00:00:00", event="push", wid=2)]   # before the window: no cell, still counted as last
    return wf.build(flows, runs, repos, now, days, ZoneInfo("Europe/London"))


def test_build_page():
    now = t("2026-11-15T12:00:00")
    p = _page(now)
    nightly, ci = p["groups"][0]["rows"][1], p["groups"][0]["rows"][0]
    assert (ci["name"], nightly["name"]) == ("CI", "Nightly")
    assert [d.day for d in p["days"]] == [9, 10, 11, 12, 13, 14, 15]
    assert nightly["small"]
    states = [[s["state"] for s in c["segments"]] for c in nightly["cells"]]
    # 9th missed, 10th passed, 11th missed, 12-14 failed, 15th's 03:00 missed (over 90 min ago).
    assert states == [["missed"], ["passed"], ["missed"], ["failed"], ["failed"], ["failed"], ["missed"]]
    assert nightly["failing_since"] == t("2026-11-12T03:04:00") and not nightly["failing_before"]
    assert len(nightly["missed"]) == 3 and nightly["next_due"] == t("2026-11-16T03:00:00")
    assert nightly["pass_rate"] == 25
    # The CI failure on a feature branch doesn't make it "failing now".
    assert ci["failing_since"] is None and ci["runs"] == 2
    assert "push" in ci["triggers"]
    tiles = {x["label"]: x["value"] for x in p["tiles"]}
    assert tiles["Failing now"] == 1 and tiles["Missed schedules"] == 3 and tiles["Switched off"] == 0
    assert [r["name"] for r in p["attention"]] == ["Nightly"]


def test_build_switched_off_stops_expecting_runs():
    p = _page(t("2026-11-15T12:00:00"), state="disabled_inactivity", state_since=t("2026-11-12T00:00:00"))
    nightly = [r for r in p["rows"] if r["name"] == "Nightly"][0]
    assert nightly["slots"] == 3 and nightly["next_due"] is None      # 9th, 10th, 11th only
    assert "off" in nightly["problems"]


def test_took():
    assert (wf.took(5), wf.took(125), wf.took(3725), wf.took(None)) == ("5s", "2m 05s", "1h 02m", "")


# ─── Messages ───────────────────────────────────────────────────────────────

WF = {"id": 1, "full_name": "o/api", "repo_name": "api", "name": "Nightly", "path": ".github/workflows/n.yml",
      "url": "https://github.com/o/api/actions/workflows/n.yml", "crons": ["0 3 * * *"], "default_branch": "main"}
VALUES = {"PUBLIC_URL": "https://bw.example", "WORK_TZ": "UTC"}


def test_wf_in_scope():
    push_main = {"event": "push", "head_branch": "main"}
    push_feat = {"event": "push", "head_branch": "feat"}
    sched = {"event": "schedule", "head_branch": "main"}
    assert wf_scope("scheduled", sched) and not wf_scope("scheduled", push_main)
    assert wf_scope("default", push_main) and not wf_scope("default", push_feat)
    assert wf_scope("all", push_feat)


def wf_scope(scope, r):
    return rules.wf_in_scope(scope, r, "main")


def test_workflow_messages():
    r = run(42, "2026-11-14T03:01:00", conclusion="failure", event="push")
    msg = rules.workflow_message({"kind": "wf_failed"}, WF, r, {"mention": True}, VALUES)
    assert msg["heading"] == "Workflow failing: Nightly" and msg["tone"] == "bad"
    assert ["Started by", "@{dana}"] in msg["facts"]
    assert msg["buttons"][0] == ["Open the run", r["url"]]
    assert msg["buttons"][1][1] == "https://bw.example/workflows?repo=o/api"
    fixed = rules.workflow_message({"kind": "wf_fixed"}, WF, {**r, "conclusion": "success"}, {}, VALUES)
    assert fixed["tone"] == "good" and "after failing" in fixed["text"]
    missed = rules.workflow_message({"kind": "wf_missed", "times": ["2026-11-14T03:00:00+00:00"]}, WF, None, {}, VALUES)
    assert "Sat 14 Nov 03:00" in missed["text"]
    off = rules.workflow_message({"kind": "wf_disabled", "state": "disabled_inactivity"}, WF, None, {}, VALUES)
    assert "60 days" in off["text"]
    assert rules.workflow_message({"kind": "wf_failed"}, WF, None, {}, VALUES) is None


def test_rules_include_workflow_rules():
    assert {"wf_failed", "wf_schedule"} <= set(rules.RULE_BY_KEY)
    for key in ("wf_failed", "wf_schedule"):
        rule = rules.RULE_BY_KEY[key]
        assert rules.describe({"key": key, "kind": rule.kind, "params": rule.defaults}, {})


# ─── The page renders ───────────────────────────────────────────────────────

def test_workflows_template_renders():
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
    env = Environment(loader=FileSystemLoader(here))
    env.filters["ago"] = lambda v: "2 days"
    env.filters["when"] = str
    now = t("2026-11-15T12:00:00")
    page = {**_page(now), "tz": "Europe/London", "no_access": [{"name": "web", "workflows_error": "no access"}],
            "unscanned": 0}

    class Req:
        args = {}
        endpoint = "workflows_page"
        blueprint = None
        path = "/workflows"

    html = env.get_template("workflows.html").render(
        w=page, periods=wf.PERIODS, days=7, repo="", repos=[], show="all",
        shows=(("all", "All"),), outcomes=wf.OUTCOMES, rule_on={"wf_failed": True},
        local=lambda x: x, request=Req, link=lambda **k: "?", repo_link=lambda f: "/repos/" + f, url_for=lambda *a, **k: "/x",
        current_user={"role": "admin", "name": "A"}, csrf_token=lambda: "", get_flashed_messages=lambda **k: [],
        version="0", reviews_waiting=None, q="")
    assert "Nightly" in html and "st-missed" in html and "Failing since 2 days ago" in html
    assert "Actions: read" in html
