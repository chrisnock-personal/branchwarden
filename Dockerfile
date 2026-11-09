FROM python:3.12-slim

LABEL org.opencontainers.image.title="Branchwarden" \
      org.opencontainers.image.description="Watches GitHub pull requests and branches, and reminds teams in Teams and Slack"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py entrypoint.sh ./
COPY templates/ templates/
COPY static/ static/

# Run as an unprivileged user; the certs and keys volumes are created owned by it.
RUN useradd --system --uid 10001 --home /app branchwarden \
 && mkdir -p /certs /keys && chown branchwarden /certs /keys && chmod 700 /keys
USER branchwarden

ENV PYTHONUNBUFFERED=1
EXPOSE 8443
# Default: the worker. docker-compose.yml runs the dashboard with entrypoint.sh.
ENTRYPOINT ["python", "-u", "worker.py"]
