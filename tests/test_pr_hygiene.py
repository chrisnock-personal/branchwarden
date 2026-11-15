from datetime import datetime, timedelta, timezone

import analysis
import data
import rules

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
P = analysis.PrPrefs(big_lines=500, big_files=30, stalled_days=7,
                     ticket_url="https://example.atlassian.net/browse/{key}")


def pr(**kw):
    base = {"title": "Add invoice PDFs", "head_ref": "feat/x", "draft": False, "additions": 40, "deletions": 10,
            "changed_files": 3, "created_at": NOW - timedelta(days=20), "updated_at": NOW - timedelta(days=1),
            "last_commit_at": NOW - timedelta(days=2), "reviews": [], "requested": [{"user": "lee"}],
            "mergeable": "MERGEABLE", "review_decision": "REVIEW_REQUIRED", "body_tickets": []}
    return {**base, **kw}


def test_big_by_lines_or_files():
    assert not analysis.is_big(pr(), P)
    assert analysis.is_big(pr(additions=450, deletions=50), P)
    assert analysis.is_big(pr(changed_files=30), P)


def test_size_band():
    assert [analysis.size_band(n, P) for n in (0, 99, 100, 499, 500)] == ["small", "small", "medium", "medium", "large"]
    assert analysis.size_band(150, analysis.PrPrefs(big_lines=80)) == "large"


def test_last_activity_and_stalled():
    quiet = pr(updated_at=NOW - timedelta(days=9), last_commit_at=NOW - timedelta(days=12))
    assert analysis.last_activity(quiet) == NOW - timedelta(days=9)
    assert analysis.is_stalled(quiet, P, NOW)
    reviewed = {**quiet, "reviews": [{"login": "lee", "state": "COMMENTED", "at": (NOW - timedelta(days=1)).isoformat()}]}
    assert not analysis.is_stalled(reviewed, P, NOW)
    assert not analysis.is_stalled({**quiet, "draft": True}, P, NOW)       # drafts never stall


def test_ticket_keys():
    texts = ["ABC-123: fix login (see abc-9, XABC-1)", "feat/ABC-123-and-DEF-7", "tracked in OPS-42"]
    assert analysis.ticket_keys(texts, P.ticket_pattern) == ["ABC-123", "XABC-1", "DEF-7", "OPS-42"]
    assert analysis.ticket_keys(["no keys here", None], P.ticket_pattern) == []
    assert analysis.ticket_keys(["ABC-1"], "([unclosed") == []          # a bad pattern finds nothing
    assert analysis.ticket_keys(["#123 and #45"], r"#[0-9]+") == ["#123", "#45"]
    assert analysis.ticket_links(["ABC-1"], P.ticket_url) == [
        {"key": "ABC-1", "url": "https://example.atlassian.net/browse/ABC-1"}]
    assert analysis.ticket_links(["ABC-1"], "") == []


def test_enrich_pr_categories():
    p = data.enrich_pr({**pr(title="WEB-12 Add invoice PDFs", additions=900, updated_at=NOW - timedelta(days=8),
                             last_commit_at=NOW - timedelta(days=8)),
                        "repo_name": "api", "number": 4}, NOW, P)
    assert {"big", "stalled"} <= p["cats"] and "noticket" not in p["cats"]
    assert p["tickets"][0]["key"] == "WEB-12" and p["quiet"] == "8 days"
    p = data.enrich_pr({**pr(), "repo_name": "api", "number": 5}, NOW, P)
    assert "noticket" in p["cats"] and not p["big"] and not p["stalled"]
    # Without a ticket link set up, tickets aren't looked for and "No ticket" never applies.
    p = data.enrich_pr({**pr(), "repo_name": "api", "number": 6}, NOW, analysis.PrPrefs())
    assert p["tickets"] == [] and "noticket" not in p["cats"]
    # Keys from the description (stored at scan time) count too.
    p = data.enrich_pr({**pr(body_tickets=["OPS-3"]), "repo_name": "api", "number": 7}, NOW, P)
    assert [t["key"] for t in p["tickets"]] == ["OPS-3"]


def test_pr_prefs_from_values():
    p = data.pr_prefs(values={"BIG_PR_LINES": "800", "STALLED_DAYS": "3", "TICKET_URL": "https://x/{key}"})
    assert (p.big_lines, p.big_files, p.stalled_days, p.ticket_url) == (800, 30, 3, "https://x/{key}")


def test_stalled_reminder():
    quiet = data.enrich_pr({**pr(updated_at=NOW - timedelta(days=9), last_commit_at=NOW - timedelta(days=9)),
                            "repo_name": "api", "number": 8, "author": "dana"}, NOW, P)
    assert rules.reminder_since("stalled", quiet) == NOW - timedelta(days=9)
    heading, text = rules.reminder_line("stalled", quiet, True)
    assert heading == "Stalled · 9 days" and text.startswith("@{dana}, nothing has happened here for 9 days")
    assert rules.RULE_BY_KEY["stalled"].enabled is False


def test_ticket_buttons_on_messages():
    p = {"url": "https://github.com/o/api/pull/8", "id": 1,
         "tickets": [{"key": "WEB-12", "url": "https://x/WEB-12"}, {"key": "A-1", "url": "u1"}, {"key": "B-2", "url": "u2"}]}
    buttons = rules._buttons(p, {"PUBLIC_URL": "https://bw"})
    assert buttons[2:] == [["WEB-12", "https://x/WEB-12"], ["A-1", "u1"]]      # at most two
