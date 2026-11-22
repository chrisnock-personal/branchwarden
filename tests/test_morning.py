"""The Slack morning message: its wording, and when it goes (Postgres tests need TEST_DATABASE_URL)."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import rules

LONDON = ZoneInfo("Europe/London")


def _pr(ref, title, **kw):
    return {"ref": ref, "url": f"https://github.com/o/{ref}", "title": title, **kw}


def test_morning_message_wording():
    assert rules.morning_message({"to_review": [], "mine": [{"next_step": None}]}, "Dana Reyes", None) is None
    work = {"to_review": [_pr("api #1", "Fix login", asked_ago="2 days"), _pr("web #2", "Dark mode", asked_ago="5 h")],
            "mine": [_pr("api #3", "Retries", next_step="Fix the failing checks"), _pr("api #4", "Docs", next_step=None)]}
    m = rules.morning_message(work, "Dana Reyes", "https://bw/me")
    assert m["heading"] == "Good morning, Dana"
    assert m["text"] == "2 reviews waiting on you, oldest 2 days; 1 of your PRs needs something."
    assert [s["title"] for s in m["sections"]] == ["Waiting on your review", "Your pull requests"]
    assert m["sections"][1]["lines"] == [{"link": "https://github.com/o/api #3", "label": "api #3",
                                          "text": "Retries · Fix the failing checks"}]
    assert m["buttons"] == [["Open My work", "https://bw/me"]]
    many = {"to_review": [_pr(f"r #{i}", "t", asked_ago="1 h") for i in range(11)], "mine": []}
    assert rules.morning_message(many, "x", None)["sections"][0]["more"] == "+ 3 more"


@pytest.fixture
def slack(db_url, monkeypatch):
    import db
    import demo
    import notify
    import settings
    with db.get_conn() as conn:
        demo.load(conn, settings.load(conn))
        db.execute(conn, "UPDATE people SET slack_id = 'U01DANA001', morning = TRUE WHERE login = 'dana'")
        db.execute(conn, "UPDATE people SET slack_id = 'U01SAM0001', morning = FALSE WHERE login = 'sam'")
        db.execute(conn, "UPDATE people SET slack_id = 'U01PRIYA01', morning = TRUE WHERE login = 'priya'")
        db.execute(conn, "INSERT INTO people (login, name, slack_id, morning) VALUES ('quiet', 'Quinn', 'U01QUIET01', TRUE)")
        db.execute(conn, "UPDATE rules SET enabled = TRUE WHERE key = 'morning'")
        settings.save(conn, {"SLACK_DM_TOKEN": "xoxb-test", "WORK_DAYS": "mon,tue,wed,thu,fri,sat,sun"}, "test")
    sent = []
    answer = {"raise": None}

    def post(token, payload):
        if answer["raise"]:
            raise answer["raise"]
        sent.append((token, payload))
        return "1.0"
    monkeypatch.setattr(notify, "slack_api_post", post)
    return sent, answer


def _at(hour, minute=0, days=0):
    today = datetime.now(LONDON).date() + timedelta(days=days)
    return datetime(today.year, today.month, today.day, hour, minute, tzinfo=LONDON).astimezone(timezone.utc)


def test_sent_once_a_working_day_after_its_time(slack):
    import db
    import notify
    import settings
    sent, answer = slack
    with db.get_conn() as conn:
        values = settings.load(conn)
        assert rules.run_morning(conn, values, _at(8, 0)) == 0            # before 09:15
        assert rules.run_morning(conn, values, _at(9, 30)) == 2           # dana (three reviews) and priya (two to merge)
        to = {p["channel"]: p for _, p in sent}
        assert set(to) == {"U01DANA001", "U01PRIYA01"} and sent[0][0] == "xoxb-test"
        assert "3 reviews waiting on you" in str(to["U01DANA001"]["blocks"])
        assert "Merge it, or close it" in str(to["U01PRIYA01"]["blocks"])
        assert rules.run_morning(conn, values, _at(11, 0)) == 0           # once a day
        # quinn has nothing waiting: nothing sent, but the day is done for them.
        assert "U01QUIET01" not in to
        assert db.one(conn, "SELECT morning_last FROM people WHERE login = 'quiet'")["morning_last"] is not None
        assert all(p["channel"] != "U01SAM0001" for _, p in sent)         # sam didn't opt in
        # Tomorrow, Slack is down: retried, not recorded; then a setup problem: recorded.
        answer["raise"] = notify.RetryLater("HTTP 503")
        assert rules.run_morning(conn, values, _at(9, 30, days=1)) == 0
        assert db.one(conn, "SELECT morning_error FROM people WHERE login = 'dana'")["morning_error"] is None
        answer["raise"] = notify.PermanentFailure("Slack: channel_not_found")
        rules.run_morning(conn, values, _at(9, 31, days=1))
        assert db.one(conn, "SELECT morning_error FROM people WHERE login = 'dana'")["morning_error"] == "Slack: channel_not_found"
        # Not a working day: nothing.
        settings.save(conn, {"WORK_DAYS": "mon"}, "test")
        values = settings.load(conn)
        when = _at(9, 30, days=2)
        if when.astimezone(LONDON).weekday() != 0:
            answer["raise"] = None
            assert rules.run_morning(conn, values, when) == 0


def test_opting_in(slack):
    import actions
    import db
    with db.get_conn() as conn:
        r = actions.set_morning(conn, "alex", True, "Alex")                # no Slack member ID
        assert not r.ok and "Settings › People" in r.message
        assert actions.set_morning(conn, "sam", True, "Sam").ok
        assert actions.morning_status(conn, "sam")["enabled"] is True
        assert actions.morning_now(conn, __import__("settings").load(conn), "dana").ok
