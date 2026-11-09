#!/bin/sh
# Make the self-signed certificate on the first start (unless TLS is off), then
# run the dashboard under gunicorn (its control socket is off: certificate
# reloads use SIGHUP). The worker container runs worker.py instead.
set -e
python -u tls.py --ensure
exec gunicorn --config gunicorn.conf.py --no-control-socket
