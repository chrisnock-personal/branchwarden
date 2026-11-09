"""
wsgi.py
~~~~~~~
The dashboard under gunicorn (see gunicorn.conf.py, which serves HTTPS).
"""

from dashboard import app, configure_app

configure_app()

__all__ = ["app"]
