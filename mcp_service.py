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

    MCP_UPSTREAM        the dashboard, as this container reaches it (https://dashboard:8443)
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s %(message)s")
    from branchwarden_cli import mcp_server
    wait_for_certificate()
    pinned = fingerprint()
    os.environ["BRANCHWARDEN_URL"] = os.getenv("MCP_UPSTREAM", "https://dashboard:8443")
    os.environ["BRANCHWARDEN_FINGERPRINT"] = pinned
    exit_when_replaced(pinned)
    allowed = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    try:
        mcp_server.run_http(host="0.0.0.0", port=int(os.getenv("MCP_PORT", "8765")), certfile=tls.CERT,
                            keyfile=tls.KEY, allowed_hosts=allowed or None)
    except RuntimeError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
