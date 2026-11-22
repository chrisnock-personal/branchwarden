"""The API's shape, without a database: access rules, the OpenAPI document, and that
every route is described (and every description has a route)."""

import re

import pytest

import access
import api
import dashboard  # noqa: F401  (registers the API blueprint on the app)


@pytest.mark.parametrize("role,scope,need,expected", [
    ("viewer", "write", "read", True), ("viewer", "write", "act", False), ("viewer", "write", "admin", False),
    ("member", "write", "act", True), ("member", "write", "admin", False),
    ("admin", "write", "admin", True), ("admin", "read", "read", True),
    ("admin", "read", "act", False), ("admin", "read", "admin", False),       # a read-only token only reads
    ("nobody", "write", "read", False),
    ("viewer", "write", "self", True), ("viewer", "read", "self", False),    # your own tokens: any role, not read-only
])
def test_access(role, scope, need, expected):
    assert access.allowed(role, need, scope) is expected


def test_openapi_is_valid():
    from openapi_spec_validator import validate
    validate(api.openapi())


def test_every_route_is_described_and_every_description_is_a_route():
    app = dashboard.app
    routes = set()
    for rule in app.url_map.iter_rules():
        if rule.endpoint.startswith("api."):
            path = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", rule.rule)[len("/api/v1"):]
            for m in rule.methods - {"HEAD", "OPTIONS"}:
                routes.add((m, path))
    described = {(e.method, e.path) for e in api.ENDPOINTS}
    assert routes == described
    assert len(described) > 40


def test_every_change_needs_more_than_read():
    for e in api.ENDPOINTS:
        if e.method != "GET":
            assert e.need in ("act", "admin", "self"), (e.method, e.path)
        if e.path.startswith("/admin/"):
            assert e.need == "admin", e.path


def test_jsonable_and_flat():
    from datetime import date, datetime, timezone
    when = datetime(2026, 11, 22, 9, 30, tzinfo=timezone.utc)
    assert api.jsonable({"a": when, "b": {2, 1}, "c": [date(2026, 11, 22)]}) == \
        {"a": "2026-11-22T09:30:00Z", "b": [1, 2], "c": ["2026-11-22"]}
    assert api.flat({"repos": ["api-*", "web"], "days": ["mon"], "x": None, "n": 3}) == \
        {"repos": "api-*,web", "days": ["mon"], "n": 3}


def test_the_reference_page_is_current():
    import os
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "api.md")
    with open(here) as f:
        assert f.read() == api.markdown(), "docs/api.md is out of date: python api.py --markdown > docs/api.md"
