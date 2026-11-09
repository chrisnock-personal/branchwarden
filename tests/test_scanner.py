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
