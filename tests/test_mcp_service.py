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
