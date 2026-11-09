"""Gunicorn settings for the dashboard: HTTPS (TLS 1.2+) unless BRANCHWARDEN_TLS=off.

One process with threads: the first-run setup code and the sign-in throttle
live in its memory.
"""

import os
import ssl

import tls

wsgi_app = "wsgi:app"
workers = 1
threads = 8
accesslog = "-"
bind = [f"0.0.0.0:{os.getenv('DASHBOARD_PORT', '8443' if tls.enabled() else '8080')}"]



def post_fork(server, worker):
    # Lets tls.reload() know its parent is gunicorn's master.
    os.environ["BRANCHWARDEN_UNDER_GUNICORN"] = "1"


if tls.enabled():
    certfile = tls.CERT
    keyfile = tls.KEY

    def ssl_context(conf, default_ssl_context_factory):
        ctx = default_ssl_context_factory()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx
