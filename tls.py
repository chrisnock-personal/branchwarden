"""
tls.py
~~~~~~
HTTPS for the dashboard. On the first start a self-signed certificate is made
for the host's names and addresses; admins can upload their own under
Settings › TLS. The files live in the certs volume, apart from the database.

    python tls.py --ensure     # make a self-signed certificate if there's none (the entrypoint runs this)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import logging
import os
import shutil
import signal
import socket
import ssl
import sys
import tempfile
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

log = logging.getLogger("branchwarden.tls")

CERT_DIR = os.getenv("BRANCHWARDEN_CERT_DIR", "/certs")
CERT = os.path.join(CERT_DIR, "cert.pem")
KEY = os.path.join(CERT_DIR, "key.pem")


def enabled() -> bool:
    return os.getenv("BRANCHWARDEN_TLS", "on").lower() not in ("off", "false", "0", "no")


def _names() -> tuple[list[str], list[str]]:
    dns = {"localhost", socket.gethostname()}
    ips = {"127.0.0.1"}
    for item in os.getenv("TLS_HOSTNAMES", "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ips.add(str(ipaddress.ip_address(item)))
        except ValueError:
            dns.add(item)
    return sorted(dns), sorted(ips)


def make_self_signed(days: int = 825) -> tuple[bytes, bytes]:
    dns, ips = _names()
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns[0] if dns[0] != "localhost" else "Branchwarden"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Branchwarden (self-signed)")])
    now = dt.datetime.now(dt.timezone.utc)
    sans = [x509.DNSName(d) for d in dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))


def _write(cert_pem: bytes, key_pem: bytes) -> None:
    os.makedirs(CERT_DIR, exist_ok=True)
    for path, content, mode in ((KEY, key_pem, 0o600), (CERT, cert_pem, 0o644)):
        tmp = path + ".new"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp, path)


def ensure() -> bool:
    """Make a self-signed certificate if there's none. True if one was made."""
    if os.path.exists(CERT) and os.path.exists(KEY):
        return False
    cert, key = make_self_signed()
    _write(cert, key)
    info = describe()
    log.warning("Made a self-signed certificate for %s (SHA-256 %s). Upload your own under Settings › TLS.",
                ", ".join(info["names"]), info["fingerprint"])
    return True


def describe(pem: Optional[bytes] = None) -> dict:
    if pem is None:
        with open(CERT, "rb") as f:
            pem = f.read()
    certs = x509.load_pem_x509_certificates(pem)
    leaf = certs[0]
    try:
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(n) for n in san.get_values_for_type(x509.DNSName)] + \
                [str(n) for n in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    fp = leaf.fingerprint(hashes.SHA256()).hex().upper()
    expires = leaf.not_valid_after_utc
    return {"subject": leaf.subject.rfc4514_string(), "issuer": leaf.issuer.rfc4514_string(),
            "self_signed": leaf.subject == leaf.issuer, "names": names,
            "not_before": leaf.not_valid_before_utc, "expires": expires,
            "days_left": (expires - dt.datetime.now(dt.timezone.utc)).days,
            "fingerprint": ":".join(fp[i:i + 2] for i in range(0, len(fp), 2)), "chain": len(certs)}


def check(cert_pem: bytes, key_pem: bytes) -> Optional[str]:
    """Why this certificate and key can't be used, or None if they can."""
    try:
        info = describe(cert_pem)
    except (ValueError, IndexError):
        return "That isn't a PEM certificate."
    if info["days_left"] < 0:
        return "That certificate has expired."
    try:
        serialization.load_pem_private_key(key_pem, password=None)
    except (ValueError, TypeError):
        return "That isn't an unencrypted PEM private key."
    with tempfile.TemporaryDirectory() as d:
        c, k = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
        with open(c, "wb") as f:
            f.write(cert_pem)
        with open(k, "wb") as f:
            f.write(key_pem)
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(c, k)
        except ssl.SSLError:
            return "The private key doesn't match the certificate."
    return None


def install(cert_pem: bytes, key_pem: bytes) -> None:
    """Keep the current files as .previous, write the new ones and reload."""
    if os.path.exists(CERT):
        shutil.copy2(CERT, CERT + ".previous")
        shutil.copy2(KEY, KEY + ".previous")
    _write(cert_pem, key_pem)
    reload()


def restore_previous() -> bool:
    if not (os.path.exists(CERT + ".previous") and os.path.exists(KEY + ".previous")):
        return False
    with open(CERT + ".previous", "rb") as f:
        cert = f.read()
    with open(KEY + ".previous", "rb") as f:
        key = f.read()
    install(cert, key)
    return True


def has_previous() -> bool:
    return os.path.exists(CERT + ".previous")


def reload() -> None:
    """Ask gunicorn's master (this worker's parent) to start fresh workers,
    which load the new files."""
    if os.getenv("BRANCHWARDEN_UNDER_GUNICORN"):
        os.kill(os.getppid(), signal.SIGHUP)


def fingerprint_of(pem: bytes) -> str:
    return hashlib.sha256(pem).hexdigest()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s")
    if "--ensure" in sys.argv:
        if enabled():
            ensure()
    else:
        print(__doc__)
