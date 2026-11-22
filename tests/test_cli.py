"""
The branchwarden CLI (cli/): helpers and parsing, certificate pinning against a
real local HTTPS server, and every command against the real app with the demo
data (needs TEST_DATABASE_URL, like the other integration tests).
"""

import hashlib
import http.server
import json
import os
import ssl
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli"))

from branchwarden_cli import __version__, main as cli  # noqa: E402


def test_version_matches_the_server():
    import version
    assert __version__ == version.VERSION


def test_helpers():
    assert cli.clean_fingerprint("ab:cd:ef") == "ABCDEF" and cli.nice_fingerprint("abcdef") == "AB:CD:EF"
    assert cli.parse_pairs(["STALE_DAYS=45", "INCLUDE_FORKS=true", "REPO_EXCLUDE=a-*,b"]) == \
        {"STALE_DAYS": 45, "INCLUDE_FORKS": True, "REPO_EXCLUDE": "a-*,b"}
    with pytest.raises(cli.Fail):
        cli.parse_pairs(["nope"])
    assert cli.normalise_url("bw.example.com/") == "https://bw.example.com"
    with pytest.raises(cli.Fail) as e:
        cli.normalise_url("http://bw.example.com")
    assert e.value.code == 2
    table = cli.render_table(["ID", "TITLE"], [[1, "x" * 400]])
    assert "…" in table and len(table.splitlines()) == 2
    assert cli.ago(90) == "1 min" and cli.ago(3 * 3600) == "3 h" and cli.ago(3 * 86400) == "3 days"


def test_flags_before_or_after_the_command():
    p = cli.parser()
    for argv in (["--json", "prs", "list"], ["prs", "list", "--json"], ["prs", "--json", "list"]):
        args = p.parse_args(argv)
        assert getattr(args, "json", False) is True, argv
    with pytest.raises(SystemExit) as e:
        p.parse_args(["prs", "snooze", "1", "--days", "2"])
    assert e.value.code == 2


@pytest.fixture
def https_server():
    """A local HTTPS server with a self-signed certificate that answers like the API."""
    import tls
    cert, key = tls.make_self_signed()
    d = tempfile.mkdtemp()
    with open(f"{d}/c.pem", "wb") as f:
        f.write(cert)
    with open(f"{d}/k.pem", "wb") as f:
        f.write(key)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"name": "T", "role": "viewer", "scope": "read", "token": {"prefix": "bwt_x"},
                               "version": __version__}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Branchwarden-Version", __version__)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(f"{d}/c.pem", f"{d}/k.pem")
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from cryptography import x509
    der = x509.load_pem_x509_certificate(cert).public_bytes(__import__("cryptography.hazmat.primitives.serialization",
                                                                       fromlist=["Encoding"]).Encoding.DER)
    yield f"https://127.0.0.1:{srv.server_port}", hashlib.sha256(der).hexdigest().upper()
    srv.shutdown()


def test_certificate_pinning(https_server):
    url, fp = https_server
    assert cli.server_fingerprint(url) == fp
    assert cli.system_trusts(url) is False                         # self-signed: not trusted by the system
    assert cli.Client(url, "bwt_x", fp).request("GET", "/me")["name"] == "T"
    with pytest.raises(cli.Fail) as e:
        cli.Client(url, "bwt_x", "00" * 32).request("GET", "/me")
    assert e.value.code == 3 and "certificate has changed" in str(e.value)
    with pytest.raises(cli.Fail) as e:
        cli.Client(url, "bwt_x").request("GET", "/me")               # no pin: refused, with advice
    assert e.value.code == 3 and "fingerprint" in str(e.value)


# ─── Every command against the real app ─────────────────────────────────────

@pytest.fixture
def live(db_url, monkeypatch, tmp_path):
    from werkzeug.serving import make_server

    import apitokens
    import dashboard
    import db
    import demo
    import settings
    with db.get_conn() as conn:
        demo.load(conn, settings.load(conn))
        tokens = {}
        for role in ("admin", "member", "viewer"):
            uid = settings.create_user(conn, f"{role}@example.com", role.capitalize(), role, "test",
                                       password="a-long-test-password").user_id
            if role == "member":
                settings.set_my_login(conn, uid, "dana")
            tokens[role] = apitokens.create(conn, role, "write", "test", user_id=uid)[1]
    dashboard.app.secret_key = "s" * 32
    monkeypatch.setenv("BRANCHWARDEN_INSECURE_HTTP", "1")
    monkeypatch.setenv("BRANCHWARDEN_CLI_ALLOW_HTTP", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "CONFIG", str(tmp_path / "branchwarden" / "cli.json"))
    monkeypatch.setattr(cli, "keyring_get", lambda url: None)
    monkeypatch.setattr(cli, "keyring_set", lambda url, token: False)
    srv = make_server("127.0.0.1", 0, dashboard.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("BRANCHWARDEN_URL", url)
    yield {"url": url, "tokens": tokens}
    srv.shutdown()


def run(live, monkeypatch, capsys, who, *argv):
    monkeypatch.setenv("BRANCHWARDEN_TOKEN", live["tokens"][who])
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_commands(live, monkeypatch, capsys):
    r = lambda who, *a: run(live, monkeypatch, capsys, who, *a)  # noqa: E731
    code, out, _ = r("member", "whoami")
    assert code == 0 and out.startswith("Member, member")
    code, out, _ = r("member", "me")
    assert code == 0 and "Reviews waiting on dana: 3" in out
    code, out, _ = r("viewer", "prs", "list", "--filter", "stalled", "--json")
    stalled = json.loads(out)
    assert code == 0 and {p["number"] for p in stalled} == {55, 74}
    code, out, _ = r("viewer", "prs", "list", "--limit", "5", "-q")
    assert code == 0 and len(out.split()) == 5
    code, out, _ = r("viewer", "prs", "show", str(stalled[0]["id"]))
    assert code == 0 and "Waiting on" in out
    code, out, _ = r("member", "prs", "reviewers", "add", str(stalled[0]["id"]), "--user", "sam", "--dry-run")
    assert code == 0 and out.startswith("Would request sam")
    code, out, _ = r("member", "prs", "snooze", str(stalled[0]["id"]), "--days", "3")
    assert code == 0 and out.startswith("No reminders about this PR until")
    code, out, _ = r("viewer", "branches", "list", "--status", "stale", "-q")
    assert code == 0 and len(out.split()) == 7
    code, out, _ = r("viewer", "repos", "demo-org/billing-api")
    assert code == 0 and "Head branches aren't deleted after merge" in out
    code, out, _ = r("viewer", "workflows", "--problems")
    assert code == 0 and "Nightly build" in out
    assert r("viewer", "metrics", "--days", "90")[0] == 0 and r("viewer", "mvp")[0] == 0
    code, out, _ = r("admin", "admin", "settings", "set", "scanning", "STALE_DAYS=40")
    assert code == 0 and "Branches are stale after" in out
    code, out, _ = r("admin", "api", "GET", "/admin/settings")
    assert code == 0 and json.loads(out)["settings"]["STALE_DAYS"]["value"] == "40"
    code, out, _ = r("viewer", "tokens", "create", "--name", "cli test", "--read-only", "-q")
    assert code == 0 and out.strip().startswith("bwt_")


def test_exit_codes(live, monkeypatch, capsys):
    r = lambda who, *a: run(live, monkeypatch, capsys, who, *a)  # noqa: E731
    code, _, err = r("viewer", "scan")
    assert code == 3 and "member role" in err                   # not allowed
    assert r("viewer", "prs", "show", "999999")[0] == 4           # not found
    assert r("member", "prs", "snooze", "999999", "--days", "3")[0] == 4
    assert r("admin", "admin", "settings", "set", "scanning", "STALE_DAYS=soon")[0] == 5   # refused
    monkeypatch.setenv("BRANCHWARDEN_TOKEN", "bwt_wrong")
    assert cli.main(["whoami"]) == 3                               # bad token
    capsys.readouterr()


def test_login_checks_and_stores_the_address(live, monkeypatch, capsys):
    monkeypatch.delenv("BRANCHWARDEN_URL")
    monkeypatch.setenv("BRANCHWARDEN_TOKEN", live["tokens"]["viewer"])
    assert cli.main(["login", "--url", live["url"], "--yes"]) == 0
    assert "wasn't stored" in capsys.readouterr().out             # no keychain in the test
    assert cli.load_config()["url"] == live["url"]
    assert cli.main(["login", "--url", live["url"], "--yes", "--token", live["tokens"]["viewer"]]) == 0
    assert "shell history" in capsys.readouterr().err               # a typed token: warned about
    monkeypatch.delenv("BRANCHWARDEN_TOKEN")
    assert cli.main(["login", "--url", live["url"], "--yes"]) == 2  # no token and no terminal: no prompt, a clear error
    assert "Give a token" in capsys.readouterr().err
