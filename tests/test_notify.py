import json

import pytest

import notify

PEOPLE = {"morgan": {"login": "morgan", "name": "Morgan O", "teams_id": "morgan@example.com", "slack_id": "U01MORGAN"},
          "dana": {"login": "dana", "name": "", "teams_id": "", "slack_id": ""}}

MSG = {"heading": "Waiting for review · 3 days", "tone": "warn", "title": "Fix <script> & stuff",
       "title_url": "https://github.com/o/r/pull/1", "facts": [["Author", "@{dana}"]],
       "text": "@{morgan}: please review.", "buttons": [["Open", "https://x"], ["Skip", None]],
       "sections": [{"title": "Stale", "lines": [{"code": "feat/x", "text": "12 days · @{morgan}"},
                                                 {"link": "https://y", "label": "r #2", "text": "a < b"}], "more": "+ 3 more"}]}


def test_teams_card_mentions_and_entities():
    card = notify.teams_card(MSG, PEOPLE)["attachments"][0]["content"]
    text = json.dumps(card)
    assert "<at>Morgan O</at>" in text
    assert card["msteams"]["entities"][0]["mentioned"]["id"] == "morgan@example.com"
    assert "**@dana**" in text                      # unmatched people are bold names
    assert len(card["actions"]) == 1                # the button without a URL is dropped
    assert card["body"][1]["text"].startswith("[Fix <script> & stuff](")


def test_slack_escapes_and_mentions():
    out = notify.slack_message(MSG, PEOPLE)
    text = json.dumps(out)
    assert "<@U01MORGAN>" in text
    assert "Fix &lt;script&gt; &amp; stuff" in text
    assert "a &lt; b" in text and "<https://y|r #2>" in text
    assert out["text"].startswith("Waiting for review")


def test_mentioned_logins():
    assert notify.mentioned(MSG) == ["morgan", "dana"]


def test_channel_matching():
    ch = {"repos": "api-*", "teams": "org/web"}
    assert notify.channel_matches(ch, "org/api-gateway", [])
    assert notify.channel_matches(ch, "org/other", ["org/web"])
    assert not notify.channel_matches(ch, "org/other", ["org/infra"])
    assert notify.channel_matches({"repos": "", "teams": ""}, "anything/x", [])


class Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


def test_post_classifies_failures(monkeypatch):
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: Resp(404, "no_service"))
    with pytest.raises(notify.PermanentFailure, match="404: no_service"):
        notify.post("https://hooks.slack.com/x", {})
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: Resp(503))
    with pytest.raises(notify.RetryLater):
        notify.post("https://hooks.slack.com/x", {})

    def boom(*a, **k):
        raise notify.requests.ConnectionError("https://secret-url")
    monkeypatch.setattr(notify.requests, "post", boom)
    with pytest.raises(notify.RetryLater) as exc:
        notify.post("https://hooks.slack.com/x", {})
    assert "secret" not in str(exc.value)
