import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest

import analysis
import ghlogin
import github
import metrics
import notify


def test_choose_reviewers_least_busy_codeowners_first():
    groups = [("owner", ["Sasha", "morgan"]), ("pool", ["alex", "jules", "morgan"])]
    picked = analysis.choose_reviewers(groups, count=2, exclude={"dana"},
                                       load={"sasha": 3, "morgan": 1}, recent={})
    assert picked == [("morgan", "owner"), ("sasha", "owner")]
    picked = analysis.choose_reviewers(groups, count=3, exclude={"sasha"},
                                       load={"morgan": 1, "alex": 0, "jules": 0}, recent={"alex": 2})
    assert picked == [("morgan", "owner"), ("jules", "pool"), ("alex", "pool")]


def test_choose_reviewers_never_the_author_and_may_find_nobody():
    assert analysis.choose_reviewers([("pool", ["dana"])], count=1, exclude={"dana"}, load={}, recent={}) == []


def test_role_for():
    who = {"login": "alice", "teams": {"acme/leads"}, "orgs": {"acme"}}
    mapping = {"admin": ["acme/leads"], "member": ["acme/engineering"], "viewer": ["acme"]}
    assert ghlogin.role_for(who, mapping) == "admin"
    assert ghlogin.role_for({**who, "teams": set()}, mapping) == "viewer"
    assert ghlogin.role_for({"login": "bob", "teams": set(), "orgs": {"other"}}, mapping) is None
    assert ghlogin.role_for({"login": "bob", "teams": set(), "orgs": set()}, {"member": ["@Bob"]}) == "member"
    assert ghlogin.role_for(who, {}) is None          # nothing mapped: nobody gets in


def test_pkce_and_authorize_url():
    verifier, challenge = ghlogin.pkce_pair()
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    url = ghlogin.authorize_url("https://api.github.com", "cid", "https://bw/login/github/callback", "st", challenge)
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    assert parts.netloc == "github.com" and q["state"] == ["st"] and q["code_challenge_method"] == ["S256"]
    assert ghlogin.noreply_email(42, "alice") == "42+alice@users.noreply.github.com"


def test_web_url():
    assert github.web_url("https://api.github.com") == "https://github.com"
    assert github.web_url("https://ghe.example.com/api/v3") == "https://ghe.example.com"


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


@pytest.mark.parametrize("status,body,expect", [
    (200, {"ok": True, "ts": "1.2"}, "1.2"),
    (200, {"ok": False, "error": "ratelimited"}, notify.RetryLater),
    (200, {"ok": False, "error": "not_in_channel"}, notify.PermanentFailure),
    (429, {}, notify.RetryLater),
])
def test_slack_api_answers(monkeypatch, status, body, expect):
    seen = {}

    def fake(url, json=None, timeout=None, headers=None):
        seen.update(url=url, headers=headers, json=json)
        return Resp(status, body)
    monkeypatch.setattr(notify.requests, "post", fake)
    if isinstance(expect, str):
        assert notify.slack_api_post("xoxb-1", {"channel": "C1"}) == expect
        assert seen["headers"]["Authorization"] == "Bearer xoxb-1"
    else:
        with pytest.raises(expect):
            notify.slack_api_post("xoxb-1", {"channel": "C1"})


def test_metrics_formatting():
    assert metrics.duration(None) == "—"
    assert metrics.duration(0.25) == "15 min"
    assert metrics.duration(5.5) == "5.5 h"
    assert metrics.duration(30) == "30 h"
    assert metrics.duration(72) == "3.0 days"
    b = metrics.bars([("a", 2, "a: 2"), ("b", None, "b: none"), ("c", 8, "c: 8")])
    assert [x["height"] for x in b] == [25, 0, 100]


def test_scanner_first_review_skips_the_author():
    import scanner
    node = {"author": {"login": "dana"}, "firstReviews": {"nodes": [
        {"submittedAt": "2026-11-01T09:00:00Z", "author": {"login": "dana"}},
        {"submittedAt": "2026-11-02T09:00:00Z", "author": {"login": "sasha"}}]}}
    assert scanner.first_review(node) == "2026-11-02T09:00:00Z"


def test_author_next_step_most_urgent_first():
    base = {"draft": False, "mergeable": "MERGEABLE", "ci_state": "SUCCESS", "review_decision": None,
            "requested": [{"user": "morgan"}], "reviews": []}
    assert analysis.author_next_step({**base, "draft": True, "mergeable": "CONFLICTING"}).startswith("Finish")
    assert analysis.author_next_step({**base, "mergeable": "CONFLICTING", "ci_state": "FAILURE"}).startswith("Resolve")
    assert analysis.author_next_step({**base, "ci_state": "FAILURE", "review_decision": "APPROVED"}).startswith("Fix")
    assert analysis.author_next_step({**base, "review_decision": "CHANGES_REQUESTED"}).startswith("Make")
    assert analysis.author_next_step({**base, "review_decision": "APPROVED"}).startswith("Merge")
    assert analysis.author_next_step({**base, "requested": []}).startswith("Ask")
    assert analysis.author_next_step(base) is None        # waiting on reviewers


def test_version_is_semver():
    import re
    from version import VERSION
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION)
