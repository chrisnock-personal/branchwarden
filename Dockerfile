FROM python:3.15-slim

LABEL org.opencontainers.image.title="Branchwarden" \
      org.opencontainers.image.description="Watches GitHub pull requests and branches, and reminds teams in Teams and Slack"

WORKDIR /app

COPY requirements.txt requirements-mcp.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-mcp.txt
# The CLI, for the mcp service (branchwarden mcp --http); its dependencies are pinned above.
COPY cli/ /tmp/cli/
RUN pip install --no-cache-dir --no-deps /tmp/cli && rm -rf /tmp/cli

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
