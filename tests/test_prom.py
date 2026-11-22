"""The Prometheus endpoint: its format (parsed with Prometheus's own client
library) and its numbers against the demo data (needs TEST_DATABASE_URL)."""

import pytest

import prom


def test_labels_are_escaped_and_quantiles_sensible():
    assert prom._label('a"b\\c\nd') == 'a\\"b\\\\c\\nd'
    assert prom._quantiles([]) == {}
    assert prom._quantiles([5.0]) == {"0.5": 5.0, "0.9": 5.0}
    q = prom._quantiles([float(x) for x in range(1, 11)])
    assert q["0.5"] == 5.5 and 9 <= q["0.9"] <= 10
    out = prom.Out()
    out.metric("x", "gauge", "X", [({"repo": 'o/"r'}, 2), ({}, None)])
    assert 'branchwarden_x{repo="o/\\"r"} 2' in out.text() and "branchwarden_x NaN" in out.text()


@pytest.fixture
def scrape(db_url):
    import apitokens
    import dashboard
    import db
    import demo
    import settings
    with db.get_conn() as conn:
        demo.load(conn, settings.load(conn))
        token = apitokens.create(conn, "prometheus", "read", "test", role="viewer")[1]
    prom._cache.update(at=0.0, text="")
    c = dashboard.app.test_client()
    return lambda t=token: c.get("/metrics/prometheus", base_url="https://localhost",
                                 headers={"Authorization": f"Bearer {t}"} if t else {})


def test_scrape(scrape):
    from prometheus_client.parser import text_string_to_metric_families
    resp = scrape()
    assert resp.status_code == 200 and resp.headers["Content-Type"].startswith("text/plain; version=0.0.4")
    fams = {f.name: f for f in text_string_to_metric_families(resp.get_data(as_text=True))}
    total = lambda name, **want: sum(s.value for s in fams[name].samples  # noqa: E731
                                     if all(s.labels.get(k) == v for k, v in want.items()))
    assert total("branchwarden_open_pull_requests") == 16
    assert total("branchwarden_branches", status="stale") == 7
    assert total("branchwarden_reviews_waiting", reviewer="dana") == 3
    assert total("branchwarden_pull_requests_flagged", flag="stalled") == 2
    assert total("branchwarden_workflows_failing", repo="demo-org/auth-service") == 1
    assert total("branchwarden_repo_health_flag", repo="demo-org/data-pipeline", flag="unprotected") == 1
    assert fams["branchwarden_time_to_merge_hours"].type == "summary"
    assert any(s.labels.get("quantile") == "0.5" for s in fams["branchwarden_time_to_merge_hours"].samples)
    assert total("branchwarden_info", demo="true") == 1
    assert "branchwarden_messages" in fams and "branchwarden_scans_stopped" in fams


def test_scrape_needs_a_token(scrape):
    assert scrape(None).status_code == 401
    assert scrape("bwt_wrong").status_code == 401
