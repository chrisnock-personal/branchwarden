import os

from flask import render_template
from jinja2 import Environment, FileSystemLoader

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def test_every_template_parses():
    env = Environment(loader=FileSystemLoader(HERE))
    env.filters["ago"] = env.filters["when"] = str      # the dashboard's own filters
    names = env.list_templates()
    assert "pulls.html" in names
    for name in names:
        env.get_template(name)


def _tokens_page(connect):
    """Render the real page, with the dashboard's autoescaping and context."""
    import dashboard

    dashboard.app.secret_key = "t" * 32        # the CSRF field in base.html
    with dashboard.app.test_request_context("/me/tokens", base_url="https://localhost"):
        return render_template("tokens.html", tokens=[], new_token=None, connect=connect)


def test_connect_info_reads_the_published_mcp_address(monkeypatch):
    import dashboard

    monkeypatch.setattr(dashboard.tls, "enabled", lambda: False)
    with dashboard.app.test_request_context("/me/tokens", base_url="https://localhost"):
        monkeypatch.delenv("MCP_PUBLIC_URL", raising=False)
        assert dashboard.connect_info({})["mcp_url"] == ""
        monkeypatch.setenv("MCP_PUBLIC_URL", "  https://agents.example.com:8765/mcp/  ")
        assert dashboard.connect_info({})["mcp_url"] == "https://agents.example.com:8765/mcp"
        monkeypatch.setenv("MCP_PUBLIC_URL", "javascript:alert(1)")
        assert dashboard.connect_info({})["mcp_url"] == ""


def test_tokens_page_shows_http_option_only_with_an_address():
    connect = {"url": "https://branchwarden.example.com:8443", "fingerprint": None, "mcp_url": ""}
    page = _tokens_page(connect)
    assert "claude mcp add branchwarden -- branchwarden mcp" in page
    assert "--transport http" not in page and "conn-mcp-http" not in page

    connect["mcp_url"] = "https://agents.example.com:8765/mcp"
    page = _tokens_page(connect)
    assert ('id="conn-mcp-http">claude mcp add --transport http branchwarden '
            'https://agents.example.com:8765/mcp --header "Authorization: Bearer bwt_…"') in page
    assert "NODE_EXTRA_CA_CERTS" not in page          # a trusted certificate needs no extra trust

    connect["fingerprint"] = "AA:BB:CC"
    assert "NODE_EXTRA_CA_CERTS" in _tokens_page(connect)


def test_tokens_page_escapes_the_mcp_address():
    connect = {"url": "https://branchwarden.example.com:8443", "fingerprint": None,
               "mcp_url": 'https://agents.example.com:8765/mcp?x="><script>alert(1)</script>'}
    page = _tokens_page(connect)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
