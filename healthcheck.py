#!/usr/bin/env python3
"""
healthcheck.py
~~~~~~~~~~~~~~
Container health checks, for compose and the quadlets:

    python healthcheck.py worker      # the worker's heartbeat file is fresh
    python healthcheck.py dashboard   # /healthz answers 200 (it also checks the database)
    python healthcheck.py mcp         # the MCP server answers (401: it wants a token, so it's up)

The worker touches its heartbeat file every few seconds, and after each repo
while a scan runs, so a long scan still counts as alive. Exit 0 is healthy.
"""

from __future__ import annotations

import os
import ssl
import sys
import time
import urllib.request

HEARTBEAT = os.getenv("BRANCHWARDEN_HEARTBEAT", "/tmp/branchwarden-worker.alive")
STALE_SECONDS = 600


def beat() -> None:
    """Called by the worker: mark it alive."""
    try:
        with open(HEARTBEAT, "a"):
            pass
        os.utime(HEARTBEAT, None)
    except OSError:
        pass                      # a read-only /tmp shouldn't stop the worker


def worker_ok(now: float | None = None) -> bool:
    try:
        age = (now or time.time()) - os.path.getmtime(HEARTBEAT)
    except OSError:
        return False
    return age < STALE_SECONDS


def dashboard_ok() -> bool:
    import tls
    port = os.getenv("DASHBOARD_PORT", "8443" if tls.enabled() else "8080")
    scheme = "https" if tls.enabled() else "http"
    # Its own self-signed certificate on localhost: there's nothing to verify against.
    ctx = ssl._create_unverified_context() if scheme == "https" else None
    try:
        with urllib.request.urlopen(f"{scheme}://127.0.0.1:{port}/healthz", timeout=5, context=ctx) as resp:
            return resp.status == 200
    except OSError:
        return False


def mcp_ok() -> bool:
    import urllib.error
    port = os.getenv("MCP_PORT", "8765")
    req = urllib.request.Request(f"https://127.0.0.1:{port}/mcp", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5, context=ssl._create_unverified_context())
    except urllib.error.HTTPError as exc:
        return exc.code == 401
    except OSError:
        return False
    return False


if __name__ == "__main__":
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    if role == "worker":
        sys.exit(0 if worker_ok() else 1)
    if role == "dashboard":
        sys.exit(0 if dashboard_ok() else 1)
    if role == "mcp":
        sys.exit(0 if mcp_ok() else 1)
    sys.exit("usage: healthcheck.py worker|dashboard|mcp")
