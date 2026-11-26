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
        for login in ("dana", "sam", "priya", "quiet"):                  # each opted in from their own account
            _account(conn, login)
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


def _account(conn, login, github=False):
    """An account with this GitHub login, owning the person's morning message (as opting in on My work does)."""
    import db
    import settings
    uid = settings.create_user(conn, f"{login}-{__import__('uuid').uuid4().hex[:8]}@example.com", login.title(), "member", "test",
                               password="a-long-test-password").user_id
    settings.set_my_login(conn, uid, login)
    if github:
        db.execute(conn, "UPDATE users SET github_id = %s WHERE id = %s", (900000 + uid, uid))
    db.execute(conn, "UPDATE people SET morning_user = COALESCE(morning_user, %s) WHERE login = %s", (uid, login))
    return uid


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
        settings = __import__("settings")
        r = actions.morning_now(conn, settings.load(conn), "dana")
        assert not r.ok and "Demo data" in r.message                          # demo data: nothing goes out
        settings.save(conn, {"DEMO_MODE": None}, "test")
        assert actions.morning_now(conn, settings.load(conn), "dana").ok


TEAMS_URL = "https://prod-12.westeurope.logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sig=x"


def test_teams_instead_of_slack(db_url, monkeypatch):
    import actions
    import db
    import demo
    import notify
    import settings
    with db.get_conn() as conn:
        demo.load(conn, settings.load(conn))
        db.execute(conn, "UPDATE rules SET enabled = TRUE WHERE key = 'morning'")
        settings.save(conn, {"WORK_DAYS": "mon,tue,wed,thu,fri,sat,sun"}, "test")      # no Slack bot token at all
        uid = _account(conn, "dana")
        r = actions.set_morning(conn, "dana", True, "Dana", to="teams", user_id=uid)
        assert not r.ok and "webhook" in r.message                                       # nothing to send it to yet
        assert not actions.set_morning(conn, "dana", True, "Dana", to="teams", teams_url="https://example.com/x",
                                       user_id=uid).ok
        assert actions.set_morning(conn, "dana", True, "Dana", to="teams", teams_url=TEAMS_URL, user_id=uid).ok
        stored = db.one(conn, "SELECT value, secret FROM settings WHERE key = %s", (settings.morning_teams_key("dana"),))
        assert stored["secret"] and TEAMS_URL not in stored["value"]                     # encrypted
        status = actions.morning_status(conn, "dana")
        assert status["enabled"] and status["to"] == "teams" and status["teams_set"]
        posted = []
        monkeypatch.setattr(notify, "post", lambda url, payload: posted.append((url, payload)))
        monkeypatch.setattr(notify, "slack_api_post", lambda *a: pytest.fail("Slack shouldn't be used"))
        assert rules.run_morning(conn, settings.load(conn), _at(9, 30)) == 1
        url, card = posted[0]
        content = card["attachments"][0]["content"]
        assert url == TEAMS_URL and content["type"] == "AdaptiveCard"
        assert "Good morning, Dana" in str(content["body"]) and "3 reviews waiting on you" in str(content["body"])
        # Switching back to Slack without a member ID is refused; turning it off keeps the URL for later.
        assert not actions.set_morning(conn, "dana", True, "Dana", to="slack").ok
        assert actions.set_morning(conn, "dana", False, "Dana").ok
        assert actions.morning_status(conn, "dana")["teams_set"]
        assert actions.set_morning(conn, "dana", None, "Dana", remove_teams=True).ok
        assert not actions.morning_status(conn, "dana")["teams_set"]


def test_only_the_account_that_set_it_up(db_url, monkeypatch):
    """A message stops when its account goes, and another account typing the same login can't redirect it."""
    import actions
    import db
    import demo
    import notify
    import settings
    with db.get_conn() as conn:
        demo.load(conn, settings.load(conn))
        db.execute(conn, "UPDATE rules SET enabled = TRUE WHERE key = 'morning'")
        settings.save(conn, {"WORK_DAYS": "mon,tue,wed,thu,fri,sat,sun"}, "test")
        dana = _account(conn, "dana")
        assert actions.set_morning(conn, "dana", True, "Dana", to="teams", teams_url=TEAMS_URL, user_id=dana).ok
        # Someone else types "dana" on My work and tries to send it to their own chat, or switch it off.
        other = _account(conn, "dana")
        r = actions.set_morning(conn, "dana", True, "Other", to="teams", teams_url=TEAMS_URL + "-theirs", user_id=other)
        assert not r.ok and r.status == 403
        assert not actions.set_morning(conn, "dana", False, "Other", user_id=other).ok
        assert settings.load(conn)[settings.morning_teams_key("dana")] == TEAMS_URL
        # Signing in with GitHub as dana proves it: that account may take it over.
        real = _account(conn, "dana", github=True)
        assert actions.set_morning(conn, "dana", True, "Dana", to="teams", teams_url=TEAMS_URL, user_id=real).ok
        posted = []
        monkeypatch.setattr(notify, "post", lambda url, payload: posted.append(url))
        settings.delete_user(conn, dana, "test")                    # the first account going changes nothing now
        assert rules.run_morning(conn, settings.load(conn), _at(9, 30)) == 1
        db.execute(conn, "UPDATE people SET morning_last = NULL")
        settings.delete_user(conn, real, "test")                    # the owner's account removed: nothing more is sent
        assert rules.run_morning(conn, settings.load(conn), _at(9, 30)) == 0
        assert len(posted) == 1


def test_sending_now_checks_the_owner(slack):
    """“Send me one now” needs the same ownership as changing the message: the login alone isn't enough."""
    import actions
    import db
    import settings
    sent, answer = slack
    with db.get_conn() as conn:
        settings.save(conn, {"DEMO_MODE": None}, "test")
        values = settings.load(conn)
        owner = db.one(conn, "SELECT id FROM users WHERE github_login = 'dana'")["id"]
        impostor = _account(conn, "dana")                         # typed the login on My work
        r = actions.morning_now(conn, values, "dana", user_id=impostor)
        assert not r.ok and r.status == 403
        assert not sent
        assert actions.morning_now(conn, values, "dana", user_id=owner).ok
        assert len(sent) == 1 and sent[0][1]["channel"] == "U01DANA001"
        verified = _account(conn, "dana", github=True)            # signing in with GitHub as dana proves it
        assert actions.morning_now(conn, values, "dana", user_id=verified).ok
        assert len(sent) == 2


def test_sending_now_for_an_unclaimed_login_needs_github(slack):
    """Nobody owns it yet: a typed login can't send someone's message now either."""
    import actions
    import db
    import settings
    sent, answer = slack
    with db.get_conn() as conn:
        settings.save(conn, {"DEMO_MODE": None}, "test")
        db.execute(conn, "UPDATE people SET morning_user = NULL WHERE login = 'dana'")
        uid = settings.create_user(conn, "typed@example.com", "Typed", "member", "test",
                                   password="a-long-test-password").user_id
        settings.set_my_login(conn, uid, "dana")
        r = actions.morning_now(conn, settings.load(conn), "dana", user_id=uid)
        assert not r.ok and r.status == 409 and "Sign in with GitHub" in r.message
        assert not sent


def test_unclaimed_login_cannot_be_taken_by_a_typed_login(db_url):
    """Nobody owns it yet: a GitHub login typed on My work isn't enough to claim someone's morning message."""
    import actions
    import db
    import settings
    with db.get_conn() as conn:
        db.execute(conn, "INSERT INTO people (login, name) VALUES ('victim', 'Victim')")
        attacker = settings.create_user(conn, "attacker@example.com", "Attacker", "member", "test",
                                        password="a-long-test-password").user_id
        settings.set_my_login(conn, attacker, "victim")                  # typed, not verified
        r = actions.set_morning(conn, "victim", True, "Attacker", to="teams", teams_url=TEAMS_URL, user_id=attacker)
        assert not r.ok and r.status == 409 and "Sign in with GitHub" in r.message
        row = db.one(conn, "SELECT morning, morning_to, morning_user FROM people WHERE login = 'victim'")
        assert not row["morning"] and row["morning_user"] is None
        assert settings.morning_teams_key("victim") not in settings.load(conn)   # no attacker webhook stored
        # First-time setup works for the account that signs in with GitHub as that login.
        real = settings.create_user(conn, "victim@example.com", "Victim", "member", "test",
                                    password="a-long-test-password").user_id
        settings.set_my_login(conn, real, "victim")
        db.execute(conn, "UPDATE users SET github_id = %s WHERE id = %s", (900123, real))
        assert actions.set_morning(conn, "victim", True, "Victim", to="teams", teams_url=TEAMS_URL, user_id=real).ok
        assert db.one(conn, "SELECT morning_user FROM people WHERE login = 'victim'")["morning_user"] == real


def test_opt_ins_from_before_keep_going_after_upgrading(db_url):
    """Opted in before accounts owned the message: on upgrade, it's linked to the account with that login."""
    import db
    import settings
    with db.get_conn() as conn:
        uid = settings.create_user(conn, "old@example.com", "Old Timer", "member", "test",
                                   password="a-long-test-password").user_id
        settings.set_my_login(conn, uid, "oldtimer")
        db.execute(conn, "INSERT INTO people (login, slack_id, morning) VALUES ('oldtimer', 'U0OLD00001', TRUE)")
        db.execute(conn, "INSERT INTO people (login, slack_id, morning) VALUES ('nobody', 'U0NOBODY01', TRUE)")
    db.init_schema()                                                     # what starting the new version does
    with db.get_conn() as conn:
        assert db.one(conn, "SELECT morning_user FROM people WHERE login = 'oldtimer'")["morning_user"] == uid
        assert db.one(conn, "SELECT morning_user FROM people WHERE login = 'nobody'")["morning_user"] is None
