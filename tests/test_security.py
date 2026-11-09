import hashlib
import hmac
import os

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

import github
import settings
import tls


def test_secrets_roundtrip_and_are_not_plaintext():
    sealed = settings.encrypt("github_pat_secret")
    assert sealed.startswith("v1:") and "github_pat" not in sealed
    assert settings.decrypt(sealed) == "github_pat_secret"
    assert settings.encrypt("x") != settings.encrypt("x")       # fresh nonce each time


def test_rotation_opens_old_secrets(monkeypatch):
    old = settings.encrypt("value")
    monkeypatch.setattr(settings, "_key_cache", ["n" * 64])
    with pytest.raises(settings.InvalidToken):
        settings.decrypt(old)
    monkeypatch.setenv("BRANCHWARDEN_SECRET_KEY_PREVIOUS", os.environ["BRANCHWARDEN_SECRET_KEY"])
    assert settings.decrypt(old) == "value"


def test_key_file_generated_once(tmp_path, monkeypatch):
    path = tmp_path / "secret.key"
    monkeypatch.setattr(settings, "KEY_FILE", str(path))
    monkeypatch.setattr(settings, "_key_cache", [])
    monkeypatch.delenv("BRANCHWARDEN_SECRET_KEY")
    first = settings.secret_key()
    assert first and len(first) == 64 and oct(path.stat().st_mode)[-3:] == "600"
    monkeypatch.setattr(settings, "_key_cache", [])
    assert settings.secret_key() == first


def test_short_key_refused(monkeypatch):
    monkeypatch.setattr(settings, "_key_cache", ["short"])
    assert "too short" in settings.secret_key_problem()


def test_webhook_signature():
    body = b'{"x":1}'
    sig = "sha256=" + hmac.new(b"s3cret-s3cret-s3cret", body, hashlib.sha256).hexdigest()
    assert github.verify_signature("s3cret-s3cret-s3cret", body, sig)
    assert not github.verify_signature("s3cret-s3cret-s3cret", body + b" ", sig)
    assert not github.verify_signature("", body, sig)          # no secret set: refuse, don't allow
    assert not github.verify_signature("s3cret-s3cret-s3cret", body, "")


def test_app_jwt():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    token = github.app_jwt("12345", pem)
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "12345" and claims["exp"] - claims["iat"] == 600


def test_graphql_url():
    assert github.graphql_url("https://api.github.com") == "https://api.github.com/graphql"
    assert github.graphql_url("https://ghe.example.com/api/v3") == "https://ghe.example.com/api/graphql"


def test_self_signed_certificate(monkeypatch):
    monkeypatch.setenv("TLS_HOSTNAMES", "branchwarden.lan,192.168.1.195")
    cert, key = tls.make_self_signed()
    info = tls.describe(cert)
    assert info["self_signed"] and "branchwarden.lan" in info["names"] and "192.168.1.195" in info["names"]
    assert tls.check(cert, key) is None
    other = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    assert "doesn't match" in tls.check(cert, other)
    assert "isn't a PEM certificate" in tls.check(b"junk", key)


def test_channel_urls_must_be_https_and_the_right_service():
    import admin
    assert admin._channel_url_problem("slack", "http://hooks.slack.com/services/x")
    assert admin._channel_url_problem("slack", "https://evil.example.com/x")
    assert admin._channel_url_problem("slack", "https://hooks.slack.com/services/T/B/x") is None
    assert admin._channel_url_problem("teams", "https://prod-01.westeurope.logic.azure.com:443/workflows/x") is None
    assert admin._channel_url_problem("teams", "https://169.254.169.254/latest")


def test_safe_next():
    import admin
    assert admin._safe_next("//evil.com") == "/"
    assert admin._safe_next("/\\evil.com") == "/"
    assert admin._safe_next("https://evil.com") == "/"
    assert admin._safe_next("/pulls?state=ci") == "/pulls?state=ci"
