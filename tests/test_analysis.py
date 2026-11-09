from datetime import datetime, timedelta, timezone

import analysis

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def pr(**kw):
    base = {"draft": False, "mergeable": "MERGEABLE", "review_decision": None, "requested": [], "reviews": [],
            "ci_state": "SUCCESS", "created_at": "2026-11-01T10:00:00Z", "ready_at": None,
            "review_requested_at": None, "author": "dana"}
    base.update(kw)
    return base


def test_states_most_pressing_first():
    assert analysis.pr_state(pr(draft=True, mergeable="CONFLICTING")) == "draft"
    assert analysis.pr_state(pr(mergeable="CONFLICTING", review_decision="APPROVED")) == "conflicts"
    assert analysis.pr_state(pr(review_decision="CHANGES_REQUESTED")) == "changes"
    assert analysis.pr_state(pr(review_decision="APPROVED")) == "approved"
    assert analysis.pr_state(pr()) == "no_reviewer"
    assert analysis.pr_state(pr(requested=[{"user": "morgan"}])) == "awaiting"
    assert analysis.pr_state(pr(requested=[{"user": "morgan"}],
                                reviews=[{"login": "sasha", "state": "COMMENTED", "at": "2026-11-02T00:00:00Z"}])) == "in_review"


def test_categories_overlap():
    cats = analysis.pr_categories(pr(review_decision="APPROVED", ci_state="FAILURE"))
    assert {"approved", "ci"} <= cats
    assert "review" in analysis.pr_categories(pr(requested=[{"team": "org/platform"}]))


def test_state_since_uses_the_right_moment():
    p = pr(review_decision="APPROVED", reviews=[
        {"login": "a", "state": "APPROVED", "at": "2026-11-03T09:00:00Z"},
        {"login": "b", "state": "APPROVED", "at": "2026-11-04T09:00:00Z"}])
    assert analysis.state_since(p) == datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
    p = pr(requested=[{"user": "x"}], ready_at="2026-11-02T00:00:00Z", review_requested_at="2026-11-05T00:00:00Z")
    assert analysis.state_since(p) == datetime(2026, 11, 5, tzinfo=timezone.utc)


def test_waiting_on():
    assert analysis.waiting_on(pr(requested=[{"user": "morgan"}, {"team": "org/web"}])) == "morgan, org/web"
    assert analysis.waiting_on(pr(review_decision="APPROVED")) == "dana"


def test_repo_patterns():
    assert analysis.matches("my-org/api-gateway", ["api-*"])
    assert analysis.matches("my-org/api-gateway", ["my-org/*"])
    assert not analysis.matches("my-org/web", ["other/*"])
    assert not analysis.repo_included("o/r", archived=True, fork=False, include=["*"], exclude=[],
                                      with_archived=False, with_forks=False)
    assert not analysis.repo_included("o/sandbox-1", archived=False, fork=False, include=["*"],
                                      exclude=["sandbox-*"], with_archived=False, with_forks=False)


def branch(**kw):
    b = {"name": "feat/x", "protected": False, "pr_state": None, "head_oid": "a", "pr_head_oid": None,
         "last_commit_at": NOW - timedelta(days=40)}
    b.update(kw)
    return b


def test_branch_status():
    kw = dict(default_branch="main", never_flag=["release/*"], ignored=False, stale_days=30, now=NOW)
    assert analysis.branch_status(branch(), **kw) == "stale"
    assert analysis.branch_status(branch(last_commit_at=NOW - timedelta(days=2)), **kw) == "active"
    assert analysis.branch_status(branch(name="main"), **kw) is None
    assert analysis.branch_status(branch(name="release/1.2"), **kw) is None
    assert analysis.branch_status(branch(protected=True), **kw) is None
    assert analysis.branch_status(branch(pr_state="MERGED", pr_head_oid="a"), **kw) == "left_merged"
    assert analysis.branch_status(branch(pr_state="MERGED", pr_head_oid="b"), **kw) == "pushed_after_merge"
    assert analysis.branch_status(branch(pr_state="CLOSED"), **kw) == "left_closed"
    assert analysis.branch_status(branch(pr_state="OPEN"), **kw) == "open_pr"
    assert analysis.branch_status(branch(), **{**kw, "ignored": True}) is None


def test_codeowners_last_match_wins():
    text = """
# comment
*           @org/everyone
/src/token/ @org/identity @sasha
*.md        @docs-writer
docs/**     @org/docs
"""
    assert analysis.owners_for(["src/token/refresh.py"], text) == ["org/identity", "sasha"]
    assert analysis.owners_for(["README.md"], text) == ["docs-writer"]
    assert analysis.owners_for(["docs/a/b.txt"], text) == ["org/docs"]
    assert analysis.owners_for(["lib/x.py"], text) == ["org/everyone"]
    assert analysis.owners_for(["x"], "") == []


def test_working_hours():
    h = analysis.WorkHours.from_settings("mon,tue,wed,thu,fri", "09:00", "17:00", "Europe/London")
    fri_4pm = datetime(2026, 11, 6, 16, 0, tzinfo=timezone.utc)      # GMT in November
    mon_10am = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
    assert h.seconds_between(fri_4pm, mon_10am) == 2 * 3600
    assert h.is_open(mon_10am)
    assert not h.is_open(datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc))   # Sunday
    assert analysis.elapsed(fri_4pm, mon_10am, "hours", h) == 66
    assert analysis.elapsed(fri_4pm, mon_10am, "days", h) == 2.75


def test_working_hours_bad_settings_fall_back():
    h = analysis.WorkHours.from_settings("", "nonsense", "", "Not/AZone")
    assert h.days == frozenset(range(5))
    assert str(h.tz) == "UTC"


def test_ago():
    assert analysis.ago(NOW - timedelta(minutes=5), NOW) == "5 min"
    assert analysis.ago(NOW - timedelta(hours=26), NOW) == "26 h"
    assert analysis.ago(NOW - timedelta(days=3), NOW) == "3 days"
    assert analysis.ago(None, NOW) == "—"
