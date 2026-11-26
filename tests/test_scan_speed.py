import threading
from datetime import datetime, timedelta, timezone

import github
import scanner

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
NODE = {"pushedAt": "2026-11-15T10:00:00Z",
        "pullRequests": {"totalCount": 3, "nodes": [{"updatedAt": "2026-11-15T11:00:00Z"}]}}
EMPTY = {"pushedAt": "2026-11-15T10:00:00Z", "pullRequests": {"totalCount": 0, "nodes": []}}


def test_signature_changes_with_pushes_prs_and_pr_updates():
    sig = scanner.signature(NODE)
    assert sig == "2026-11-15T10:00:00Z|3|2026-11-15T11:00:00Z"
    assert scanner.signature({**NODE, "pushedAt": "2026-11-15T10:05:00Z"}) != sig
    assert scanner.signature({**NODE, "pullRequests": {"totalCount": 2, "nodes": NODE["pullRequests"]["nodes"]}}) != sig
    assert scanner.signature({**NODE, "pullRequests": {"totalCount": 3, "nodes": [{"updatedAt": "2026-11-15T11:30:00Z"}]}}) != sig
    assert scanner.signature({"pushedAt": None, "pullRequests": {"totalCount": 0, "nodes": []}}) == "None|0|None"
    assert scanner.signature({"name": "x"}) is None          # a query without the fields


def _repo(**kw):
    return {"scan_sig": scanner.signature(EMPTY), "scanned_once": True, "history_at": NOW, "last_error": None,
            "full_scan_at": NOW - timedelta(minutes=20), **kw}


def test_unchanged_only_when_everything_matches():
    sig = scanner.signature(EMPTY)
    assert scanner.unchanged(_repo(), sig, NOW)
    assert not scanner.unchanged(_repo(), "other", NOW)
    assert not scanner.unchanged(_repo(), "a|b|c", NOW)      # a signature we didn't make
    assert not scanner.unchanged(_repo(), None, NOW)
    assert not scanner.unchanged(_repo(scanned_once=False), sig, NOW)
    assert not scanner.unchanged(_repo(history_at=None), sig, NOW)        # history still to import
    assert not scanner.unchanged(_repo(last_error="boom"), sig, NOW)      # last read failed
    assert not scanner.unchanged(_repo(full_scan_at=None), sig, NOW)
    assert not scanner.unchanged(_repo(full_scan_at=NOW - timedelta(minutes=61)), sig, NOW)   # hourly refresh


def test_open_prs_are_never_skipped():
    """A review, comment, check or label need not move pushedAt, and the PR
    whose updatedAt moved need not be the newest one in the repo list."""
    sig = scanner.signature(NODE)
    assert scanner.unchanged(_repo(scan_sig=sig), sig, NOW) is False
    assert scanner.unchanged(_repo(scan_sig=scanner.signature(EMPTY)), scanner.signature(EMPTY), NOW) is True
    assert scanner.unchanged(_repo(scan_sig=scanner.signature({**EMPTY, "pushedAt": "2026-11-15T10:05:00Z"})),
                             scanner.signature(EMPTY), NOW) is False


def test_scan_workers_is_bounded():
    assert scanner.scan_workers({}) == 4
    assert scanner.scan_workers({"SCAN_THREADS": "6"}) == 6
    assert scanner.scan_workers({"SCAN_THREADS": "0"}) == 1
    assert scanner.scan_workers({"SCAN_THREADS": "99"}) == scanner.MAX_SCAN_THREADS
    assert scanner.scan_workers({"SCAN_THREADS": "junk"}) == 4


def test_run_repos_bounds_concurrency_and_keeps_order():
    # Three repos must really be in flight together; the pool must never have
    # more than three, and the results come back in repo order, not completion order.
    barrier = threading.Barrier(3, timeout=5)
    lock = threading.Lock()
    active = peak = 0
    todos = [{"id": i} for i in range(12)]

    def worker(repo):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait()
        with lock:
            active -= 1
        return {"id": repo["id"]}

    progress = []
    out = scanner.run_repos(todos, worker, 3, progress=lambda: progress.append(1))
    assert [o["id"] for o in out] == [r["id"] for r in todos]
    assert peak == 3 and len(progress) == len(todos)


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
    assert c.points == 2                      # one point per call when no GraphQL cost is given


def test_client_counts_graphql_cost_for_the_scan_log():
    c = github.Client("https://api.github.com", lambda: "t", "token")

    class S:
        def request(self, *a, **k):
            r = _Resp(200, {"x-ratelimit-remaining": "4993"})
            r.json = lambda: {"data": {"rateLimit": {"cost": 7, "remaining": 4993, "limit": 5000}}}
            r.links = {}
            return r
    c._local.session = S()
    c.graphql("query { rateLimit { cost } }")
    assert (c.requests, c.points) == (1, 7)
    assert c.rate.cost == 7 and c.rate.remaining == 4993


def test_repo_queries_ask_for_the_signature():
    for q in (scanner.ORG_REPOS, scanner.USER_REPOS, scanner.ONE_REPO):
        assert "pushedAt" in q and "totalCount" in q
        assert "rateLimit { cost" in q
        assert q.count("{") == q.count("}")


class _Client:
    label = "app"
    token_expires = None

    def __init__(self):
        self.requests = self.points = 0
        self.rate = github.RateLimit()


class _Conns:
    def __init__(self, client):
        self.client = client

    def client_for(self, login, via="auto"):
        return self.client


def test_scan_target_uses_setting_and_logs_points(monkeypatch):
    """With fakes (no GitHub or Postgres): a zero-PR repo is skipped, the
    configured pool size is used, and points spent on repos and the repo list
    are added to the scan result."""
    client = _Client()
    repo = {**_repo(), "id": 1, "full_name": "acme/api", "new_sig": scanner.signature(EMPTY),
            "included": True, "full_scan_at": datetime.now(timezone.utc)}
    seen = []

    def fake_scan_one(client, repo, values, skip, workflows_too):
        seen.append((repo["id"], skip, workflows_too))
        client.requests += 1
        client.points += 3
        return {"events": [], "prs": 0, "branches": 0, "skipped": int(skip), "error": None}

    real_run_repos = scanner.run_repos

    def spy(todo, worker, workers, progress=None):
        seen.append(("workers", workers))
        return real_run_repos(todo, worker, workers, progress)

    class Conn:
        def commit(self):
            pass

        def rollback(self):
            pass

    monkeypatch.setattr(scanner, "list_repos", lambda c, t: [EMPTY])
    monkeypatch.setattr(scanner, "_store_repo", lambda c, t, n, v: repo)
    monkeypatch.setattr(scanner, "_scan_one", fake_scan_one)
    monkeypatch.setattr(scanner, "run_repos", spy)
    monkeypatch.setattr(scanner.db, "execute", lambda *a, **k: 0)
    monkeypatch.setattr(scanner.db, "rows", lambda *a, **k: [])
    monkeypatch.setattr(scanner.db, "one", lambda *a, **k: {"inc": 1, "skip": 0})
    client.requests = client.points = 2            # the repo list cost two points
    result = scanner.ScanResult()
    scanner.scan_target(Conn(), _Conns(client), {"id": 1, "login": "acme", "via": "auto"},
                        {"SCAN_THREADS": "2"}, result)
    assert seen == [("workers", 2), (1, True, True)]
    assert (result.repos, result.skipped) == (1, 1)
    assert (result.requests, result.api_points) == (1, 3)
