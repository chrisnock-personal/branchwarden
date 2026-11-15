from datetime import datetime, timedelta, timezone

import github
import scanner

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
NODE = {"pushedAt": "2026-11-15T10:00:00Z",
        "pullRequests": {"totalCount": 3, "nodes": [{"updatedAt": "2026-11-15T11:00:00Z"}]}}


def test_signature_changes_with_pushes_prs_and_pr_updates():
    sig = scanner.signature(NODE)
    assert sig == "2026-11-15T10:00:00Z|3|2026-11-15T11:00:00Z"
    assert scanner.signature({**NODE, "pushedAt": "2026-11-15T10:05:00Z"}) != sig
    assert scanner.signature({**NODE, "pullRequests": {"totalCount": 2, "nodes": NODE["pullRequests"]["nodes"]}}) != sig
    assert scanner.signature({**NODE, "pullRequests": {"totalCount": 3, "nodes": [{"updatedAt": "2026-11-15T11:30:00Z"}]}}) != sig
    assert scanner.signature({"pushedAt": None, "pullRequests": {"totalCount": 0, "nodes": []}}) == "None|0|None"
    assert scanner.signature({"name": "x"}) is None          # a query without the fields


def _repo(**kw):
    return {"scan_sig": scanner.signature(NODE), "scanned_once": True, "history_at": NOW, "last_error": None,
            "full_scan_at": NOW - timedelta(minutes=20), **kw}


def test_unchanged_only_when_everything_matches():
    sig = scanner.signature(NODE)
    assert scanner.unchanged(_repo(), sig, NOW)
    assert not scanner.unchanged(_repo(), "other", NOW)
    assert not scanner.unchanged(_repo(), None, NOW)
    assert not scanner.unchanged(_repo(scanned_once=False), sig, NOW)
    assert not scanner.unchanged(_repo(history_at=None), sig, NOW)        # history still to import
    assert not scanner.unchanged(_repo(last_error="boom"), sig, NOW)      # last read failed
    assert not scanner.unchanged(_repo(full_scan_at=None), sig, NOW)
    assert not scanner.unchanged(_repo(full_scan_at=NOW - timedelta(minutes=61)), sig, NOW)   # hourly refresh


class _Resp:
    def __init__(self, status, headers=None, text=""):
        self.status_code, self.headers, self.text = status, headers or {}, text


def test_retry_after():
    assert github.retry_after(_Resp(200)) is None
    assert github.retry_after(_Resp(429, {"retry-after": "7"})) == 7
    assert github.retry_after(_Resp(403, {"retry-after": "900"})) == github.MAX_WAIT
    assert github.retry_after(_Resp(403, text="You have exceeded a secondary rate limit")) == 60
    assert github.retry_after(_Resp(403, {"x-ratelimit-remaining": "0"}, "API rate limit exceeded")) is None
    assert github.retry_after(_Resp(403, text="Resource not accessible by integration")) is None


def test_client_waits_and_retries_then_counts(monkeypatch):
    answers = [_Resp(429, {"retry-after": "1"}), _Resp(200)]
    slept = []
    monkeypatch.setattr(github.time, "sleep", slept.append)
    c = github.Client("https://api.github.com", lambda: "t", "token")

    class S:
        def request(self, *a, **k):
            r = answers.pop(0)
            r.json = lambda: {}
            r.links = {}
            return r
    c._local.session = S()
    assert c.rest("GET", "/x").status_code == 200
    assert slept == [1] and c.requests == 2


def test_repo_queries_ask_for_the_signature():
    for q in (scanner.ORG_REPOS, scanner.USER_REPOS, scanner.ONE_REPO):
        assert "pushedAt" in q and "totalCount" in q
        assert q.count("{") == q.count("}")
