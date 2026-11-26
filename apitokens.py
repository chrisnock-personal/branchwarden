"""
apitokens.py
~~~~~~~~~~~~
Tokens for the API. A personal token belongs to a person and acts with their
role as it is at each request (change the role and the token follows); a
service token is made by an admin for a script, with a fixed role. Either can
be read-only. A token is shown once, when it's made; only its SHA-256 hash and
first characters are kept. Each can expire and be revoked, and records when it
was last used.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import access
import db
import settings

PREFIX = "bwt_"
# Someone who signs in with GitHub has their role worked out again (from their teams) at each
# sign-in. Their tokens only work while that's recent, so leaving the teams ends API access too.
GITHUB_FRESH = timedelta(days=30)
SHOWN = 12          # characters kept to tell tokens apart: bwt_ and 8 more
MAX_DAYS = 3650


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(conn, name: str, scope: str, actor: str, *, user_id: Optional[int] = None, role: Optional[str] = None,
           expires_days: Optional[int] = None) -> tuple[Optional[dict], str]:
    """(the stored row, the token itself), or (None, reason) when the input is wrong."""
    name = (name or "").strip()
    if not name or len(name) > 80:
        return None, "Give the token a name (80 characters at most)."
    if scope not in access.SCOPES:
        return None, "The scope is read or write."
    if (user_id is None) == (role is None) or (role is not None and role not in settings.ROLES):
        return None, "A token is either a person's (no role) or a service token with a role."
    # bool is an int subclass in Python: a JSON true would mean one day, false would mean never.
    if expires_days is not None and (isinstance(expires_days, bool) or not isinstance(expires_days, int)
                                     or not 1 <= expires_days <= MAX_DAYS):
        return None, f"Expiry is a number of days from 1 to {MAX_DAYS}, or none."
    token = PREFIX + secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=expires_days) if expires_days else None
    row = db.one(conn, """INSERT INTO api_tokens (user_id, name, role, scope, token_hash, prefix, created_by, expires_at)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                 (user_id, name, role, scope, _hash(token), token[:SHOWN], actor, expires))
    settings.history_add(conn, actor, f"API token {token[:SHOWN]}… made: {name} ({scope}"
                         + (f", service, {role}" if role else "") + (f", {expires_days} days" if expires_days else "") + ")")
    return public(row), token


def public(row: dict) -> dict:
    return {"id": row["id"], "name": row["name"], "prefix": row["prefix"], "scope": row["scope"],
            "role": row.get("role"), "service": row["user_id"] is None, "created_by": row.get("created_by"),
            "created_at": row["created_at"], "expires_at": row["expires_at"], "last_used_at": row["last_used_at"],
            "revoked_at": row["revoked_at"]}


def mine(conn, user_id: int) -> list[dict]:
    return [public(r) for r in db.rows(conn, """SELECT * FROM api_tokens WHERE user_id = %s AND revoked_at IS NULL
                                                 ORDER BY created_at DESC""", (user_id,))]


def service(conn) -> list[dict]:
    return [public(r) for r in db.rows(conn, """SELECT * FROM api_tokens WHERE user_id IS NULL AND revoked_at IS NULL
                                                 ORDER BY created_at DESC""")]


def revoke(conn, token_id: int, actor: str, *, user_id: Optional[int] = None) -> bool:
    """Revoke a person's own token (user_id given) or a service token (user_id None)."""
    where = "user_id = %s" if user_id is not None else "user_id IS NULL"
    row = db.one(conn, f"""UPDATE api_tokens SET revoked_at = NOW()
                            WHERE id = %s AND {where} AND revoked_at IS NULL RETURNING prefix, name""",
                 (token_id, user_id) if user_id is not None else (token_id,))
    if row:
        settings.history_add(conn, actor, f"API token {row['prefix']}… revoked: {row['name']}")
    return bool(row)


def authenticate(conn, token: str, now: Optional[datetime] = None, why: Optional[list] = None) -> Optional[dict]:
    """Who a token acts as: {token_id, prefix, name, scope, role, user_id, user_name, github_login,
    via_github, actor}, or None if it's unknown, revoked, expired, or its owner has gone or (signing in
    with GitHub) hasn't signed in for GITHUB_FRESH. A reason goes into `why`, when given, for the owner's
    sake: it never says more than that the token exists."""
    if not token or not token.startswith(PREFIX) or len(token) > 200:
        return None
    now = now or datetime.now(timezone.utc)
    row = db.one(conn, """SELECT t.*, u.name AS user_name, u.role AS user_role, u.github_login,
                                 (u.github_id IS NOT NULL) AS via_github, u.last_login,
                                 (u.password_hash IS NOT NULL OR u.github_id IS NOT NULL) AS active
                            FROM api_tokens t LEFT JOIN users u ON u.id = t.user_id
                           WHERE t.token_hash = %s""", (_hash(token),))
    if not row or row["revoked_at"] or (row["expires_at"] and row["expires_at"] <= now):
        return None
    if row["user_id"] is not None and not (row["user_role"] and row["active"]):
        return None
    if row["via_github"] and (not row["last_login"] or now - row["last_login"] > GITHUB_FRESH):
        if why is not None:
            why.append("This token's owner signs in with GitHub and hasn't for 30 days: sign in to the "
                       "dashboard again (which checks your GitHub teams), and the token works again.")
        return None
    if not row["last_used_at"] or now - row["last_used_at"] > timedelta(minutes=1):
        db.execute(conn, "UPDATE api_tokens SET last_used_at = %s WHERE id = %s", (now, row["id"]))
    service_token = row["user_id"] is None
    who = row["name"] if service_token else row["user_name"]
    return {"token_id": row["id"], "prefix": row["prefix"], "name": row["name"], "scope": row["scope"],
            "role": row["role"] if service_token else row["user_role"], "user_id": row["user_id"],
            "user_name": row["user_name"], "github_login": row["github_login"], "via_github": bool(row["via_github"]),
            "service": service_token, "actor": f"{who} (API token {row['prefix']}…)"}


def revoke_all(conn, user_id: int, actor: str, reason: str) -> int:
    """Revoke every token a person has (when they've lost access)."""
    n = db.execute(conn, "UPDATE api_tokens SET revoked_at = NOW() WHERE user_id = %s AND revoked_at IS NULL",
                   (user_id,))
    if n:
        settings.history_add(conn, actor, f"API tokens revoked ({n}): {reason}")
    return n
