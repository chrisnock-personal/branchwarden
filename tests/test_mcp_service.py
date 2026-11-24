"""The mcp service: it pins the dashboard's certificate by the fingerprint Settings › TLS shows."""

import tls


def test_pins_the_fingerprint_the_dashboard_shows(tmp_path):
    import mcp_service
    cert, _ = tls.make_self_signed()
    leaf = tmp_path / "cert.pem"
    leaf.write_bytes(cert)
    shown = tls.describe(cert)["fingerprint"]
    assert mcp_service.fingerprint(str(leaf)) == shown.replace(":", "")
    chain = tmp_path / "chain.pem"                     # an uploaded certificate with its chain: the first one
    other, _ = tls.make_self_signed()
    chain.write_bytes(cert + other)
    assert mcp_service.fingerprint(str(chain)) == shown.replace(":", "")


def test_finds_the_dashboard_with_or_without_its_tls(monkeypatch):
    import mcp_service
    for k in ("MCP_UPSTREAM", "MCP_DASHBOARD_HOST", "DASHBOARD_PORT", "BRANCHWARDEN_TLS"):
        monkeypatch.delenv(k, raising=False)
    assert mcp_service.upstream_url() == "https://dashboard:8443"
    monkeypatch.setenv("MCP_DASHBOARD_HOST", "branchwarden-dashboard")             # the quadlets' name for it
    assert mcp_service.upstream_url() == "https://branchwarden-dashboard:8443"
    monkeypatch.setenv("BRANCHWARDEN_TLS", "off")                                  # a proxy does HTTPS
    assert mcp_service.upstream_url() == "http://branchwarden-dashboard:8080"     # where gunicorn listens then
    monkeypatch.setenv("DASHBOARD_PORT", "8443")
    assert mcp_service.upstream_url() == "http://branchwarden-dashboard:8443"
    monkeypatch.setenv("MCP_UPSTREAM", "https://elsewhere:9443")
    assert mcp_service.upstream_url() == "https://elsewhere:9443"


def test_with_tls_off_it_starts_without_a_certificate(monkeypatch, tmp_path):
    """No certificate is ever made with TLS off: it mustn't wait for one (and restart in a loop)."""
    import os
    import sys

    import pytest
    pytest.importorskip("mcp")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli"))
    import mcp_service
    from branchwarden_cli import mcp_server
    for k in ("MCP_UPSTREAM", "MCP_DASHBOARD_HOST", "DASHBOARD_PORT"):
        monkeypatch.delenv(k, raising=False)
    for k in ("BRANCHWARDEN_URL", "BRANCHWARDEN_CLI_ALLOW_HTTP", "BRANCHWARDEN_CLI_FORWARDED_PROTO"):
        monkeypatch.setenv(k, "")                                      # main() sets these: put back afterwards
    monkeypatch.setenv("BRANCHWARDEN_TLS", "off")
    monkeypatch.setattr(tls, "CERT", str(tmp_path / "none.pem"))
    monkeypatch.setattr(mcp_service, "wait_for_certificate", lambda: (_ for _ in ()).throw(AssertionError("waited")))
    got = {}
    monkeypatch.setattr(mcp_server, "run_http", lambda **kw: got.update(kw, url=os.environ["BRANCHWARDEN_URL"]))
    mcp_service.main()
    assert got["allow_http"] is True and not got.get("certfile") and got["url"] == "http://dashboard:8080"
    assert os.environ["BRANCHWARDEN_CLI_FORWARDED_PROTO"] == "https"
