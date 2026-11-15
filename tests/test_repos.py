import repohealth
import repos

BASE = {"url": "https://github.com/o/api", "default_branch": "main", "scanned_once": True,
        "codeowners": "* @o/core", "delete_branch_on_merge": True, "classic_protection": None,
        "health": {"protected": True, "reviews": 1, "rules": ["pull_request"]}}


def keys(**kw):
    return [f["key"] for f in repohealth.flags({**BASE, **kw})]


def test_healthy_repo_has_no_flags():
    assert keys() == []


def test_each_flag():
    assert keys(delete_branch_on_merge=False) == ["auto_delete"]
    assert keys(health={"protected": False, "reviews": None, "rules": []}) == ["unprotected"]
    assert keys(health={"protected": True, "reviews": 0, "rules": ["deletion"]}) == ["no_reviews"]
    assert keys(codeowners=None) == ["no_codeowners"]


def test_unknowns_are_not_flagged():
    assert keys(delete_branch_on_merge=None) == []                       # not read yet
    assert keys(health=None) == []                                         # protection not read yet
    assert keys(codeowners=None, scanned_once=False) == []                 # never fully read
    # Protected, but its review rule can't be seen (classic protection without admin rights).
    assert keys(health={"protected": True, "reviews": None, "rules": []}) == []


def test_classic_protection_counts():
    classic = {"requiresApprovingReviews": True, "requiredApprovingReviewCount": 2}
    assert keys(health={"protected": False, "reviews": None, "rules": []}, classic_protection=classic) == []
    no_reviews = {"requiresApprovingReviews": False, "requiredApprovingReviewCount": 0}
    assert keys(health={"protected": True, "reviews": None, "rules": []}, classic_protection=no_reviews) == ["no_reviews"]


def test_flag_links_go_to_the_settings():
    flags = {f["key"]: f["link"] for f in repohealth.flags(
        {**BASE, "delete_branch_on_merge": False, "codeowners": None,
         "health": {"protected": False, "reviews": None, "rules": []}})}
    assert flags["auto_delete"] == "https://github.com/o/api/settings"
    assert flags["unprotected"] == "https://github.com/o/api/settings/branches"
    assert flags["no_codeowners"] == "https://github.com/o/api/new/main?filename=.github/CODEOWNERS"


class _Client:
    def __init__(self, branch, rules):
        self.answers = {"branches": branch, "rules": rules}

    def rest(self, method, path):
        key = "rules" if "/rules/" in path else "branches"
        answer = self.answers[key]
        if isinstance(answer, Exception):
            raise answer

        class R:
            def json(self_inner):
                return answer
        return R()


def test_read_rulesets_and_branch():
    from github import GitHubError
    r = repohealth.read(_Client({"protected": False}, [{"type": "pull_request", "parameters": {"required_approving_review_count": 2}},
                                                      {"type": "deletion"}]), "o/api", "main")
    assert r == {"protected": True, "reviews": 2, "rules": ["pull_request", "deletion"]}
    # Rulesets unavailable (a free private repo): fall back to the branch's own flag.
    r = repohealth.read(_Client({"protected": True}, GitHubError("Upgrade to GitHub Pro", 403)), "o/api", "main")
    assert r == {"protected": True, "reviews": None, "rules": []}


def test_codeowners_rules():
    text = "# owners\n*       @o/core\n/docs/ @dana @o/writers  # docs\n\nsrc/token/ @sasha\n"
    assert repos.codeowners_rules(text) == [
        {"pattern": "*", "owners": ["@o/core"]},
        {"pattern": "/docs/", "owners": ["@dana", "@o/writers"]},
        {"pattern": "src/token/", "owners": ["@sasha"]}]
    assert repos.codeowners_rules(None) == []
