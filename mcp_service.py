#!/usr/bin/env python3
"""
mcp_service.py
~~~~~~~~~~~~~~
The mcp service (compose profile "mcp", or the branchwarden-mcp quadlet):
`branchwarden mcp --http` next to the app, for AI agents on other machines.

It reaches the dashboard over the stack's own network and pins the dashboard's
certificate by its fingerprint, read from the shared certs volume, so the
dashboard's self-signed certificate works without trusting anything else. It
listens with that same certificate and key, so agents see the certificate (and
fingerprint) they already know. Clients send their own API tokens; this keeps none.

When the certificate is replaced under Settings › TLS, this exits and its
restart policy starts it again with the new one.

With BRANCHWARDEN_TLS=off (a reverse proxy doing HTTPS), there's no certificate:
it reaches the dashboard over plain HTTP and listens on plain HTTP, for the
same proxy to put HTTPS in front of it. Its requests say they came in over
HTTPS (X-Forwarded-Proto), as its own clients' did, through the proxy.

    MCP_DASHBOARD_HOST  the dashboard's name on the stack's network (dashboard)
    MCP_UPSTREAM        or its whole address, overriding the above (https://dashboard:8443)
    MCP_PORT            the port to listen on (8765)
    MCP_ALLOWED_HOSTS   optional: only answer requests addressed to these names (comma-separated)
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

import tls

log = logging.getLogger("branchwarden.mcp")


def fingerprint(path: str = "") -> str:
    """The SHA-256 fingerprint of the certificate (the first, for a chain), as the CLI pins it."""
    with open(path or tls.CERT, "rb") as f:
        return tls.describe(f.read())["fingerprint"].replace(":", "")


def wait_for_certificate(timeout: float = 120) -> None:
    """The dashboard makes the certificate on its first start."""
    end = time.monotonic() + timeout
    while not (os.path.exists(tls.CERT) and os.path.exists(tls.KEY)):
        if time.monotonic() > end:
            sys.exit(f"No certificate in {tls.CERT_DIR} yet: is the dashboard running with the certs volume?")
        time.sleep(2)


def exit_when_replaced(pinned: str, every: float = 30) -> None:
    def watch():
        while True:
            time.sleep(every)
            try:
                now = fingerprint()
            except (OSError, ValueError):
                continue                      # half-written: look again next time
            if now != pinned:
                log.warning("The certificate was replaced: restarting to use the new one.")
                os._exit(0)
    threading.Thread(target=watch, daemon=True).start()


def upstream_url() -> str:
    """The dashboard as this container reaches it: the same port and scheme its own settings give it."""
    if os.getenv("MCP_UPSTREAM"):
        return os.environ["MCP_UPSTREAM"]
    host = os.getenv("MCP_DASHBOARD_HOST") or "dashboard"
    if tls.enabled():
        return f"https://{host}:{os.getenv('DASHBOARD_PORT') or '8443'}"
    return f"http://{host}:{os.getenv('DASHBOARD_PORT') or '8080'}"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
    from branchwarden_cli import mcp_server
    os.environ["BRANCHWARDEN_URL"] = upstream_url()
    allowed = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    port = int(os.getenv("MCP_PORT", "8765"))
    if not tls.enabled():
        log.info("BRANCHWARDEN_TLS is off: plain HTTP to the dashboard and on port %s, for the proxy to front.", port)
        os.environ["BRANCHWARDEN_CLI_ALLOW_HTTP"] = "1"
        os.environ["BRANCHWARDEN_CLI_FORWARDED_PROTO"] = "https"
        try:
            mcp_server.run_http(host="0.0.0.0", port=port, allow_http=True, allowed_hosts=allowed or None)
        except RuntimeError as exc:
            sys.exit(str(exc))
        return
    wait_for_certificate()
    pinned = fingerprint()
    os.environ["BRANCHWARDEN_FINGERPRINT"] = pinned
    exit_when_replaced(pinned)
    try:
        mcp_server.run_http(host="0.0.0.0", port=port, certfile=tls.CERT, keyfile=tls.KEY,
                            allowed_hosts=allowed or None)
    except RuntimeError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
