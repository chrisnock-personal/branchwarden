import scanner


def test_pr_from_node():
    node = {
        "number": 412, "title": "Rate-limit per key", "url": "https://github.com/o/r/pull/412", "isDraft": False,
        "state": "OPEN", "createdAt": "2026-11-01T10:00:00Z", "updatedAt": "2026-11-02T10:00:00Z",
        "author": {"login": "Dana"}, "mergedBy": None, "headRefName": "feat/x", "headRefOid": "abc",
        "baseRefName": "main", "reviewDecision": "REVIEW_REQUIRED", "mergeable": "MERGEABLE",
        "additions": 10, "deletions": 2, "changedFiles": 3,
        "labels": {"nodes": [{"name": "backend"}]}, "files": {"nodes": [{"path": "src/a.py"}]},
        "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": "User", "login": "Morgan"}},
                                     {"requestedReviewer": {"__typename": "Team", "combinedSlug": "Org/Platform"}},
                                     {"requestedReviewer": None}]},
        "latestReviews": {"nodes": [{"author": {"login": "sasha"}, "state": "COMMENTED", "submittedAt": "2026-11-02T00:00:00Z"}]},
        "commits": {"nodes": [{"commit": {"committedDate": "2026-11-01T09:00:00Z", "statusCheckRollup": {"state": "FAILURE"}}}]},
        "timelineItems": {"nodes": [{"__typename": "ReviewRequestedEvent", "createdAt": "2026-11-01T11:00:00Z"},
                                    {"__typename": "ReadyForReviewEvent", "createdAt": "2026-11-01T10:30:00Z"},
                                    {"__typename": "ReviewRequestedEvent", "createdAt": "2026-11-01T12:00:00Z"}]},
    }
    pr = scanner.pr_from_node(node)
    assert pr["author"] == "dana"
    assert pr["requested"] == [{"user": "morgan"}, {"team": "org/platform"}]
    assert pr["ci_state"] == "FAILURE"
    assert pr["review_requested_at"] == "2026-11-01T12:00:00Z"
    assert pr["ready_at"] == "2026-11-01T10:30:00Z"
    assert pr["files"] == ["src/a.py"] and pr["labels"] == ["backend"]


def test_branch_from_node_swaps_compare():
    node = {"name": "feat/x", "branchProtectionRule": None,
            "target": {"oid": "abc", "committedDate": "2026-10-01T00:00:00Z", "author": {"name": "Dana D", "user": {"login": "dana"}}},
            "compare": {"aheadBy": 7, "behindBy": 2},
            "associatedPullRequests": {"nodes": [{"number": 3, "state": "MERGED", "title": "t", "headRefOid": "abc",
                                                  "mergedAt": "2026-10-02T00:00:00Z", "closedAt": "2026-10-02T00:00:00Z",
                                                  "mergedBy": {"login": "Sasha"}}]}}
    b = scanner.branch_from_node(node)
    assert (b["ahead"], b["behind"]) == (2, 7)
    assert b["pr_state"] == "MERGED" and b["pr_merged_by"] == "sasha"
    assert b["last_committer"] == "dana"


def test_branch_without_github_user_keeps_git_name():
    b = scanner.branch_from_node({"name": "x", "target": {"author": {"name": "Some One", "user": None}},
                                  "associatedPullRequests": {"nodes": []}})
    assert b["last_committer"] == "Some One" and b["pr_state"] is None


def test_review_events_only_new_decisions():
    old = [{"login": "a", "state": "COMMENTED", "at": "1"}]
    new = [{"login": "a", "state": "APPROVED", "at": "2"}, {"login": "b", "state": "COMMENTED", "at": "3"}]
    assert scanner.review_events(old, new) == [{"login": "a", "state": "APPROVED", "at": "2"}]
    assert scanner.review_events(new, new) == []


def test_ended_query_is_well_formed():
    q = scanner._ended_query([1, 22])
    assert "p1: pullRequest(number: 1)" in q and "p22: pullRequest(number: 22)" in q


def _pr_node(n, updated, merged=None, closed=None, state="MERGED"):
    return {"number": n, "title": f"PR {n}", "url": f"https://github.com/o/r/pull/{n}", "state": state,
            "createdAt": "2026-01-01T00:00:00Z", "updatedAt": updated, "mergedAt": merged, "closedAt": closed or merged,
            "author": {"login": "dana"}, "mergedBy": {"login": "Sasha"} if merged else None,
            "latestReviews": {"nodes": [{"author": {"login": "lee"}, "state": "APPROVED", "submittedAt": updated}]}}


class _FakeClient:
    def __init__(self, pages):
        self.pages, self.calls = pages, 0

    def graphql(self, query, variables):
        page = self.pages[self.calls]
        self.calls += 1
        return {"repository": {"pullRequests": {"nodes": page, "pageInfo": {
            "hasNextPage": self.calls < len(self.pages), "endCursor": str(self.calls)}}}}


def test_fetch_history_stops_at_the_cutoff():
    from datetime import datetime, timezone
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    pages = [
        [_pr_node(9, "2026-11-10T00:00:00Z", merged="2026-11-09T00:00:00Z"),
         _pr_node(8, "2026-11-05T00:00:00Z", closed="2026-11-04T00:00:00Z", state="CLOSED"),
         # Updated recently (a comment) but merged before the cutoff: not imported.
         _pr_node(7, "2026-10-20T00:00:00Z", merged="2026-09-15T00:00:00Z")],
        [_pr_node(6, "2026-10-02T00:00:00Z", merged="2026-10-02T00:00:00Z"),
         _pr_node(5, "2026-09-20T00:00:00Z", merged="2026-09-20T00:00:00Z")],
        [_pr_node(4, "2026-09-01T00:00:00Z", merged="2026-09-01T00:00:00Z")],   # never read
    ]
    client = _FakeClient(pages)
    prs = scanner.fetch_history(client, "o", "r", since)
    assert [p["number"] for p in prs] == [9, 8, 6]
    assert client.calls == 2
    merged = prs[0]
    assert merged["state"] == "merged" and merged["merged_by"] == "sasha"
    assert merged["reviews"] == [{"login": "lee", "state": "APPROVED", "at": "2026-11-10T00:00:00Z"}]
    assert prs[1]["state"] == "closed" and prs[1]["merged_by"] is None


def test_history_query_is_well_formed():
    assert "states: [MERGED, CLOSED]" in scanner.ENDED_PRS and "UPDATED_AT" in scanner.ENDED_PRS
    assert scanner.ENDED_PRS.count("{") == scanner.ENDED_PRS.count("}")
