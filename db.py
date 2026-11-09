"""
db.py
~~~~~
Postgres persistence for Branchwarden.

Tables
------
targets          – GitHub organisations and accounts to scan
repos            – their repositories, as last scanned
pull_requests    – open PRs, and PRs that were open once (kept to report merges and closes)
branches         – every branch, with its newest PR, as last scanned
ignored_branches – branches someone chose not to flag
channels         – Teams and Slack channels; their webhook URLs are secrets in settings
rules            – event, reminder and digest rules (fixed set, editable parameters)
people           – GitHub login → name, Teams and Slack identities, for @mentions
notifications    – delivery queue: one row per message per channel
reminders        – how often each reminder has gone out for each PR
scan_log         – one row per scan, for the dashboard
scan_requests    – repos to rescan soon (from webhooks and reviewer requests)
org_cache        – org members and teams, for the reviewer picker
assignments      – reviewers Branchwarden or someone in the dashboard requested
snoozes          – reminders someone has put off for a PR
threads          – the Slack message each PR's thread hangs off, per channel
daily            – one row a day of headline counts, for the trend charts
settings, users, settings_log – see settings.py
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor

log = logging.getLogger("branchwarden.db")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://{user}:{password}@{host}:{port}/{dbname}".format(
        user=os.getenv("POSTGRES_USER", "branchwarden"),
        password=os.getenv("POSTGRES_PASSWORD", "branchwarden"),
        host=os.getenv("POSTGRES_HOST", "db"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "branchwarden"),
    ),
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS targets (
    id              SERIAL PRIMARY KEY,
    login           TEXT UNIQUE NOT NULL,          -- organisation or user login, lower case
    kind            TEXT NOT NULL DEFAULT 'org' CHECK (kind IN ('org', 'user')),
    via             TEXT NOT NULL DEFAULT 'auto' CHECK (via IN ('auto', 'app', 'token')),
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    repo_count      INTEGER NOT NULL DEFAULT 0,
    skipped_count   INTEGER NOT NULL DEFAULT 0,
    used            TEXT,                          -- app | token: what the last scan used
    last_scan_at    TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS repos (
    id              SERIAL PRIMARY KEY,
    target_id       INTEGER NOT NULL REFERENCES targets (id) ON DELETE CASCADE,
    owner           TEXT NOT NULL,
    name            TEXT NOT NULL,
    full_name       TEXT UNIQUE NOT NULL,          -- owner/name, lower case
    url             TEXT,
    default_branch  TEXT,
    archived        BOOLEAN NOT NULL DEFAULT FALSE,
    fork            BOOLEAN NOT NULL DEFAULT FALSE,
    private         BOOLEAN NOT NULL DEFAULT FALSE,
    included        BOOLEAN NOT NULL DEFAULT TRUE, -- passes the include/skip rules
    codeowners      TEXT,
    scanned_once    BOOLEAN NOT NULL DEFAULT FALSE,-- no events from a repo's first scan
    last_scan_at    TIMESTAMPTZ,
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS pull_requests (
    id              SERIAL PRIMARY KEY,
    repo_id         INTEGER NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
    number          INTEGER NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    author          TEXT,
    state           TEXT NOT NULL DEFAULT 'open',  -- open | merged | closed
    draft           BOOLEAN NOT NULL DEFAULT FALSE,
    head_ref        TEXT,
    head_oid        TEXT,
    base_ref        TEXT,
    review_decision TEXT,                          -- APPROVED | CHANGES_REQUESTED | REVIEW_REQUIRED | NULL
    mergeable       TEXT,                          -- MERGEABLE | CONFLICTING | UNKNOWN
    ci_state        TEXT,                          -- SUCCESS | FAILURE | ERROR | PENDING | EXPECTED | NULL
    additions       INTEGER NOT NULL DEFAULT 0,
    deletions       INTEGER NOT NULL DEFAULT 0,
    changed_files   INTEGER NOT NULL DEFAULT 0,
    files           JSONB NOT NULL DEFAULT '[]',   -- changed paths (first 100)
    labels          JSONB NOT NULL DEFAULT '[]',
    requested       JSONB NOT NULL DEFAULT '[]',   -- [{"user": login} | {"team": "org/slug"}]
    reviews         JSONB NOT NULL DEFAULT '[]',   -- latest per reviewer: [{"login", "state", "at"}]
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ,
    ready_at        TIMESTAMPTZ,                   -- last marked ready for review
    review_requested_at TIMESTAMPTZ,               -- last review request
    last_commit_at  TIMESTAMPTZ,
    merged_at       TIMESTAMPTZ,
    merged_by       TEXT,
    closed_at       TIMESTAMPTZ,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    seen_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (repo_id, number)
);

CREATE INDEX IF NOT EXISTS idx_pull_requests_open ON pull_requests (repo_id) WHERE state = 'open';

CREATE TABLE IF NOT EXISTS branches (
    id              SERIAL PRIMARY KEY,
    repo_id         INTEGER NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    head_oid        TEXT,
    last_commit_at  TIMESTAMPTZ,
    last_committer  TEXT,                          -- GitHub login, or the git author name
    protected       BOOLEAN NOT NULL DEFAULT FALSE,
    ahead           INTEGER,                       -- commits not in the default branch
    behind          INTEGER,
    pr_number       INTEGER,
    pr_state        TEXT,                          -- OPEN | MERGED | CLOSED
    pr_title        TEXT,
    pr_head_oid     TEXT,
    pr_merged_at    TIMESTAMPTZ,
    pr_closed_at    TIMESTAMPTZ,
    pr_merged_by    TEXT,
    seen_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (repo_id, name)
);

CREATE TABLE IF NOT EXISTS ignored_branches (
    repo_id         INTEGER NOT NULL REFERENCES repos (id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    ignored_by      TEXT,
    ignored_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (repo_id, name)
);

CREATE TABLE IF NOT EXISTS channels (
    id              SERIAL PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('teams', 'slack')),
    name            TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    repos           TEXT NOT NULL DEFAULT '',      -- comma-separated repo patterns
    teams           TEXT NOT NULL DEFAULT '',      -- comma-separated org/team slugs
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rules (
    key             TEXT PRIMARY KEY,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    params          JSONB NOT NULL DEFAULT '{}',
    last_run_at     TIMESTAMPTZ,                   -- digests: when it last went out
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS people (
    login           TEXT PRIMARY KEY,              -- GitHub login, lower case
    name            TEXT NOT NULL DEFAULT '',
    teams_id        TEXT NOT NULL DEFAULT '',      -- Teams user principal name (usually the email)
    slack_id        TEXT NOT NULL DEFAULT '',      -- Slack member ID, U…
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS notifications (
    id              SERIAL PRIMARY KEY,
    channel_id      INTEGER NOT NULL REFERENCES channels (id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,                 -- opened | review | merged | closed | reminder:<rule> | digest | assigned | test
    pr_id           INTEGER REFERENCES pull_requests (id) ON DELETE SET NULL,
    payload         JSONB NOT NULL,                -- the message, rendered per channel at send time
    status          TEXT NOT NULL DEFAULT 'pending', -- pending | sent | failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_notifications_pending ON notifications (next_attempt_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_notifications_channel ON notifications (channel_id, created_at DESC);

CREATE TABLE IF NOT EXISTS reminders (
    rule            TEXT NOT NULL,
    pr_id           INTEGER NOT NULL REFERENCES pull_requests (id) ON DELETE CASCADE,
    since           TIMESTAMPTZ NOT NULL,          -- the start of the state it reminds about
    count           INTEGER NOT NULL DEFAULT 0,
    last_at         TIMESTAMPTZ,
    PRIMARY KEY (rule, pr_id)
);

CREATE TABLE IF NOT EXISTS scan_log (
    id              SERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    trigger         TEXT NOT NULL DEFAULT 'schedule', -- schedule | webhook | manual
    repos           INTEGER NOT NULL DEFAULT 0,
    prs             INTEGER NOT NULL DEFAULT 0,
    branches        INTEGER NOT NULL DEFAULT 0,
    events          INTEGER NOT NULL DEFAULT 0,
    rate_remaining  INTEGER,
    rate_limit      INTEGER,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS scan_requests (
    id              SERIAL PRIMARY KEY,
    full_name       TEXT NOT NULL,                 -- owner/name, lower case; '*' for everything
    reason          TEXT NOT NULL,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS webhook_log (
    id              SERIAL PRIMARY KEY,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event           TEXT NOT NULL,
    action          TEXT,
    repo            TEXT,
    status          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS org_cache (
    login           TEXT PRIMARY KEY,
    members         JSONB NOT NULL DEFAULT '[]',
    teams           JSONB NOT NULL DEFAULT '[]',
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- First review, for the metrics (latestReviews only has the newest per reviewer).
ALTER TABLE pull_requests ADD COLUMN IF NOT EXISTS first_review_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_pull_requests_ended ON pull_requests (repo_id, merged_at) WHERE state <> 'open';

CREATE TABLE IF NOT EXISTS assignments (
    id              SERIAL PRIMARY KEY,
    pr_id           INTEGER NOT NULL REFERENCES pull_requests (id) ON DELETE CASCADE,
    reviewer        TEXT NOT NULL,                 -- login, or org/team slug
    by_rule         BOOLEAN NOT NULL DEFAULT FALSE,-- auto-assigned, not by a person
    by_name         TEXT,
    assigned_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_assignments_reviewer ON assignments (reviewer, assigned_at DESC);

CREATE TABLE IF NOT EXISTS snoozes (
    pr_id           INTEGER PRIMARY KEY REFERENCES pull_requests (id) ON DELETE CASCADE,
    until           TIMESTAMPTZ NOT NULL,
    by_name         TEXT,
    at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS threads (
    channel_id      INTEGER NOT NULL REFERENCES channels (id) ON DELETE CASCADE,
    pr_id           INTEGER NOT NULL REFERENCES pull_requests (id) ON DELETE CASCADE,
    ts              TEXT NOT NULL,                 -- Slack's message timestamp
    PRIMARY KEY (channel_id, pr_id)
);

-- Slack channels sent through a bot token (for threads) instead of a webhook.
ALTER TABLE channels ADD COLUMN IF NOT EXISTS slack_channel TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS daily (
    day             DATE PRIMARY KEY,
    open_prs        INTEGER NOT NULL,
    needs_review    INTEGER NOT NULL,
    stale_branches  INTEGER NOT NULL,
    left_branches   INTEGER NOT NULL
);

-- Settings edited in the dashboard (see settings.py). Secrets are encrypted.
CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    secret          BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by      TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    email           TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL,
    password_hash   TEXT,
    setup_token     TEXT,
    setup_expires   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login      TIMESTAMPTZ
);

-- People who sign in with GitHub: their role comes from their teams at each sign-in.
ALTER TABLE users ADD COLUMN IF NOT EXISTS github_id BIGINT UNIQUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS github_login TEXT;

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('admin', 'member', 'viewer'));

-- Who changed what. Secret values are never written here.
CREATE TABLE IF NOT EXISTS settings_log (
    id              SERIAL PRIMARY KEY,
    at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor           TEXT NOT NULL,
    what            TEXT NOT NULL
);
"""


@contextmanager
def get_conn():
    """Yield a psycopg2 connection; commit on clean exit, roll back on error."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema() -> None:
    """Create tables and indexes if they don't exist. Serialised with an
    advisory lock, as the worker and dashboard both call it at start."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(7347001)")
            cur.execute(SCHEMA_SQL)
    log.info("Database schema ready")


def rows(conn, sql: str, params=None) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def one(conn, sql: str, params=None):
    found = rows(conn, sql, params)
    return found[0] if found else None


def execute(conn, sql: str, params=None) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def request_scan(conn, full_name: str, reason: str) -> None:
    """Ask the worker to rescan one repo ('*' for all) soon."""
    execute(conn, "INSERT INTO scan_requests (full_name, reason) VALUES (lower(%s), %s)",
            (full_name, reason))
