from datetime import datetime, timezone

import analysis
import data
import rules

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def enriched(**kw):
    base = {"id": 1, "number": 7, "title": "T", "url": "u", "repo_name": "r", "full_name": "o/r", "draft": False,
            "mergeable": "MERGEABLE", "review_decision": None, "requested": [], "reviews": [], "ci_state": None,
            "created_at": datetime(2026, 11, 2, tzinfo=timezone.utc), "ready_at": None, "review_requested_at": None,
            "author": "dana", "last_commit_at": None, "head_ref": "h", "base_ref": "main", "additions": 1,
            "deletions": 1, "labels": []}
    base.update(kw)
    return data.enrich_pr(base, NOW)


def test_reminder_since():
    p = enriched(requested=[{"user": "morgan"}])
    assert rules.reminder_since("waiting", p) == p["since"]
    assert rules.reminder_since("no_reviewer", p) is None
    assert rules.reminder_since("no_reviewer", enriched()) is not None
    ch = enriched(review_decision="CHANGES_REQUESTED",
                  reviews=[{"login": "r", "state": "CHANGES_REQUESTED", "at": "2026-11-05T00:00:00Z"}],
                  last_commit_at="2026-11-06T00:00:00Z")
    assert rules.reminder_since("changes", ch) is None          # the author has pushed since
    ch2 = enriched(review_decision="CHANGES_REQUESTED",
                   reviews=[{"login": "r", "state": "CHANGES_REQUESTED", "at": "2026-11-05T00:00:00Z"}],
                   last_commit_at="2026-11-04T00:00:00Z")
    assert rules.reminder_since("changes", ch2) == datetime(2026, 11, 5, tzinfo=timezone.utc)


def test_reminder_line_mentions():
    heading, text = rules.reminder_line("waiting", enriched(requested=[{"user": "morgan"}, {"team": "o/web"}]), True)
    assert heading.startswith("Waiting for review") and "@{morgan}" in text and "o/web" in text
    _, text = rules.reminder_line("approved", enriched(review_decision="APPROVED"), False)
    assert "@{" not in text


def test_digest_due_once_a_day():
    hours = analysis.WorkHours.from_settings("mon", "09:00", "17:00", "UTC")
    rule = {"params": {"day": "mon", "time": "09:00"}, "last_run_at": None}
    assert rules.digest_due(rule, NOW, hours)                    # Monday 12:00
    assert not rules.digest_due({**rule, "last_run_at": NOW}, NOW, hours)
    assert not rules.digest_due(rule, datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc), hours)
    assert not rules.digest_due(rule, datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc), hours)


def test_digest_message():
    branches = [{"status": "stale", "name": f"b{i}", "repo_name": "r", "age_days": 40 + i, "last_committer": "dana"}
                for i in range(12)]
    branches.append({"status": "left_merged", "name": "m", "repo_name": "r2", "pr_state": "MERGED", "pr_number": 4,
                     "pr_merged_at": NOW, "pr_closed_at": NOW})
    msg = rules.digest_message(branches, {}, True, 10)
    assert msg["sections"][0]["more"] == "+ 2 more"
    assert msg["sections"][0]["lines"][0]["code"] == "b11"
    assert "@{dana}" in msg["sections"][0]["lines"][0]["text"]
    assert rules.digest_message([], {}, True, 10) is None


def test_describe_rules():
    for r in rules.RULES:
        rule = {"key": r.key, "kind": r.kind, "params": r.defaults}
        assert rules.describe(rule, {})
