"""
settings.py
~~~~~~~~~~~
Settings, users and the change history, kept in Postgres and edited in the
dashboard.

On the first start with an empty database, the GitHub settings found in .env
are imported once; from then on the database is what counts. Only the
database connection, the dashboard's address and the secret key stay in .env
(or, for the key, its own volume).

Secrets (the GitHub token, the App's private key, the webhook secret and each
channel's webhook URL) are encrypted with AES-256-GCM (see Encryption below)
and never shown again once saved.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.fernet import InvalidToken

import db

log = logging.getLogger("branchwarden.settings")

SETUP_LINK_HOURS = 24
MIN_PASSWORD = 12

# key: (label, default)
DEFAULTS = {
    "GITHUB_API_URL": ("GitHub API address", "https://api.github.com"),
    "GITHUB_APP_ENABLED": ("GitHub App switched on", "true"),
    "GITHUB_APP_ID": ("GitHub App ID", ""),
    "GITHUB_APP_PRIVATE_KEY": ("GitHub App private key", ""),
    "GITHUB_WEBHOOK_SECRET": ("Webhook secret", ""),
    "GITHUB_TOKEN_ENABLED": ("Personal access token switched on", "true"),
    "GITHUB_TOKEN": ("Personal access token", ""),
    "GITHUB_TOKEN_EXPIRES": ("Token expiry", ""),
    "REPO_INCLUDE": ("Only repos matching", "*"),
    "REPO_EXCLUDE": ("Skip repos matching", ""),
    "INCLUDE_ARCHIVED": ("Include archived repositories", "false"),
    "INCLUDE_FORKS": ("Include forks", "false"),
    "SCAN_INTERVAL_MINUTES": ("Full scan every (minutes)", "10"),
    "STALE_DAYS": ("Branches are stale after (days)", "30"),
    "NEVER_FLAG": ("Never flag branches matching", "main,master,develop,release/*,hotfix/*,gh-pages"),
    "WORK_DAYS": ("Working days", "mon,tue,wed,thu,fri"),
    "WORK_START": ("Working day starts", "09:00"),
    "WORK_END": ("Working day ends", "17:30"),
    "WORK_TZ": ("Time zone", "Europe/London"),
    "PUBLIC_URL": ("Branchwarden's address", ""),
}

SECRETS = {"GITHUB_APP_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET", "GITHUB_TOKEN"}

SCAN_INTERVALS = (("5", "5 minutes"), ("10", "10 minutes"), ("15", "15 minutes"),
                  ("30", "30 minutes"), ("60", "1 hour"))

# Read from .env on the first start only.
ENV_IMPORT = ("GITHUB_API_URL", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_WEBHOOK_SECRET",
              "GITHUB_TOKEN", "PUBLIC_URL")


def channel_url_key(channel_id: int) -> str:
    return f"CHANNEL_{channel_id}_URL"


def is_secret(key: str) -> bool:
    return key in SECRETS or bool(re.fullmatch(r"CHANNEL_\d+_URL", key))


def label(key: str) -> str:
    if key in DEFAULTS:
        return DEFAULTS[key][0]
    m = re.fullmatch(r"CHANNEL_(\d+)_URL", key)
    if m:
        return f"Channel {m.group(1)} webhook URL"
    return key


def get(values: dict, key: str) -> str:
    """A setting's value, or its default when nothing is stored."""
    if key in values:
        return values[key]
    return DEFAULTS.get(key, ("", ""))[1]


def flag(values: dict, key: str) -> bool:
    return get(values, key).lower() == "true"


def number(values: dict, key: str, fallback: int) -> int:
    try:
        return int(get(values, key))
    except ValueError:
        return fallback


def split_list(value: str) -> list[str]:
    return [v.strip() for v in (value or "").replace("\n", ",").split(",") if v.strip()]


def join_list(values) -> str:
    seen: list[str] = []
    for v in values:
        v = v.strip()
        if v and v not in seen:
            seen.append(v)
    return ",".join(seen)


# ─── Encryption ─────────────────────────────────────────────────────────────
#
# Secrets are sealed with AES-256-GCM. The key comes from BRANCHWARDEN_SECRET_KEY
# if .env sets it; otherwise one is generated on the first start and kept in
# KEY_FILE, a volume of its own, apart from the database. To rotate, put the old
# value in BRANCHWARDEN_SECRET_KEY_PREVIOUS: secrets still open with it, and are
# sealed again with the new key on the next start (see reseal()).

KEY_FILE = os.getenv("BRANCHWARDEN_KEY_FILE", "/keys/secret.key")
SEALED_PREFIX = "v1:"
_key_cache: list[str] = []


class SettingsError(Exception):
    pass


def _key_from_file() -> Optional[str]:
    try:
        with open(KEY_FILE) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.error("Can't read %s: %s", KEY_FILE, exc)
        return None
    folder = os.path.dirname(KEY_FILE)
    if not folder or not os.path.isdir(folder):
        return None
    try:
        # O_EXCL: if the worker and the dashboard start together, one writes it.
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
        log.warning("Generated a new secret key in %s. Back it up: stored secrets can't be "
                    "read without it.", KEY_FILE)
    except FileExistsError:
        pass
    except OSError as exc:
        log.error("Can't write %s: %s", KEY_FILE, exc)
        return None
    with open(KEY_FILE) as f:
        return f.read().strip() or None


def secret_key() -> Optional[str]:
    if not _key_cache:
        key = os.getenv("BRANCHWARDEN_SECRET_KEY", "").strip() or _key_from_file()
        if key:
            _key_cache.append(key)
    return _key_cache[0] if _key_cache else None


def secret_key_problem() -> Optional[str]:
    key = secret_key()
    if not key:
        return (f"No secret key: set BRANCHWARDEN_SECRET_KEY in .env, or give the container a writable "
                f"{os.path.dirname(KEY_FILE)} volume so one can be generated")
    if len(key) < 32:
        return "BRANCHWARDEN_SECRET_KEY is too short: use at least 32 random characters (openssl rand -hex 32)"
    return None


def _derive(material: str, purpose: str) -> bytes:
    return hashlib.sha256(f"branchwarden:{purpose}:{material}".encode()).digest()


def _derived(purpose: str) -> bytes:
    problem = secret_key_problem()
    if problem:
        raise SettingsError(problem)
    return _derive(secret_key(), purpose)


def session_key() -> bytes:
    return _derived("session")


def _aead_keys() -> list[bytes]:
    keys = [_derived("settings")]
    previous = os.getenv("BRANCHWARDEN_SECRET_KEY_PREVIOUS", "").strip()
    if previous:
        keys.append(_derive(previous, "settings"))
    return keys


def encrypt(value: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(_aead_keys()[0]).encrypt(nonce, value.encode(), b"branchwarden-setting")
    return SEALED_PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode()


def decrypt(value: str) -> str:
    """Open a sealed value with the current key or the previous one."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not value.startswith(SEALED_PREFIX):
        raise InvalidToken
    raw = base64.urlsafe_b64decode(value[len(SEALED_PREFIX):])
    for key in _aead_keys():
        try:
            return AESGCM(key).decrypt(raw[:12], raw[12:], b"branchwarden-setting").decode()
        except InvalidTag:
            continue
    raise InvalidToken


def reseal(conn) -> int:
    """Seal every secret again with the current key (after a rotation)."""
    if not os.getenv("BRANCHWARDEN_SECRET_KEY_PREVIOUS", "").strip():
        return 0
    n = 0
    for r in db.rows(conn, "SELECT key, value FROM settings WHERE secret"):
        try:
            plain = decrypt(r["value"])
        except InvalidToken:
            continue
        db.execute(conn, "UPDATE settings SET value = %s WHERE key = %s", (encrypt(plain), r["key"]))
        n += 1
    if n:
        log.info("Sealed %d secrets again with the current key; BRANCHWARDEN_SECRET_KEY_PREVIOUS "
                 "can be removed now", n)
    return n


# ─── Stored values ──────────────────────────────────────────────────────────

def load(conn, unreadable: Optional[set] = None) -> dict[str, str]:
    """Every stored setting, secrets decrypted. A secret that can't be
    decrypted (the key has changed) is left out, and named in `unreadable`."""
    values = {}
    for r in db.rows(conn, "SELECT key, value, secret FROM settings"):
        if r["secret"]:
            try:
                values[r["key"]] = decrypt(r["value"])
            except InvalidToken:
                log.error("Can't decrypt %s: the secret key has changed since it was "
                          "saved. Enter it again in the dashboard.", r["key"])
                if unreadable is not None:
                    unreadable.add(r["key"])
        else:
            values[r["key"]] = r["value"]
    return values


def secret_meta(conn) -> dict[str, dict]:
    """When and by whom each secret was last set: all the dashboard shows of it."""
    return {r["key"]: r for r in db.rows(
        conn, "SELECT key, updated_at, updated_by FROM settings WHERE secret")}


def save(conn, changes: dict[str, Optional[str]], actor: str, *, record: bool = True) -> list[str]:
    """Store changes (None or "" removes a setting) and record each in the
    change history, secrets without their value. Returns the history lines."""
    current = load(conn)
    lines = []
    with conn.cursor() as cur:
        for key, value in changes.items():
            value = (value or "").strip() or None
            old = current.get(key)
            if (old or None) == value:
                continue
            name = label(key)
            if value is None:
                cur.execute("DELETE FROM settings WHERE key = %s", (key,))
                line = f"{name}: removed"
            else:
                secret = is_secret(key)
                cur.execute("""
                    INSERT INTO settings (key, value, secret, updated_at, updated_by)
                    VALUES (%s, %s, %s, NOW(), %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                        secret = EXCLUDED.secret, updated_at = NOW(), updated_by = EXCLUDED.updated_by
                """, (key, encrypt(value) if secret else value, secret, actor))
                if secret:
                    line = f"{name}: replaced" if old else f"{name}: set"
                elif old is None:
                    line = f"{name}: set to {value}"
                else:
                    line = f"{name}: {old} → {value}"
            lines.append(line)
            if record:
                history_add(conn, actor, line)
    return lines


def bootstrap() -> None:
    """On the very first start, import GitHub settings and targets from .env."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(7347002)")
        if db.one(conn, "SELECT 1 AS x FROM settings_log WHERE what LIKE 'Imported%%'"):
            return
        found = {}
        for key in ENV_IMPORT:
            value = os.getenv(key, "").strip()
            if key == "GITHUB_APP_PRIVATE_KEY" and not value and os.getenv("GITHUB_APP_PRIVATE_KEY_FILE"):
                try:
                    with open(os.environ["GITHUB_APP_PRIVATE_KEY_FILE"]) as f:
                        value = f.read().strip()
                except OSError as exc:
                    log.error("Can't read GITHUB_APP_PRIVATE_KEY_FILE: %s", exc)
            if value:
                found[key] = value.replace("\\n", "\n") if key == "GITHUB_APP_PRIVATE_KEY" else value
        if found and secret_key_problem() is None:
            save(conn, found, "Branchwarden", record=False)
        targets = []
        for item in split_list(os.getenv("GITHUB_TARGETS", "")):
            login, _, kind = item.partition(":")
            kind = kind if kind in ("org", "user") else "org"
            db.execute(conn, """INSERT INTO targets (login, kind) VALUES (lower(%s), %s)
                                ON CONFLICT (login) DO NOTHING""", (login, kind))
            targets.append(login)
        names = [label(k) for k in found] + ([f"targets {', '.join(targets)}"] if targets else [])
        history_add(conn, "Branchwarden", "Imported from .env: " + (", ".join(names) or "nothing"))


def imported_summary(conn) -> str:
    row = db.one(conn, "SELECT what FROM settings_log WHERE what LIKE 'Imported%%' ORDER BY id LIMIT 1")
    return row["what"].split(": ", 1)[1] if row else "nothing"


def history_add(conn, actor: str, what: str) -> None:
    db.execute(conn, "INSERT INTO settings_log (actor, what) VALUES (%s, %s)", (actor, what))


def history(conn, limit: int = 50) -> list[dict]:
    return db.rows(conn, "SELECT at, actor, what FROM settings_log ORDER BY id DESC LIMIT %s", (limit,))


# ─── Users ──────────────────────────────────────────────────────────────────

ROLES = ("admin", "member", "viewer")


@dataclass
class NewLink:
    user_id: int
    token: str = field(repr=False)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def user_count(conn) -> int:
    return db.one(conn, "SELECT COUNT(*) AS n FROM users")["n"]


def users(conn) -> list[dict]:
    return db.rows(conn, """SELECT id, email, name, role, last_login, setup_expires,
                                   password_hash IS NOT NULL AS active
                              FROM users ORDER BY role, name""")


def user(conn, user_id: int) -> Optional[dict]:
    return db.one(conn, "SELECT id, email, name, role FROM users WHERE id = %s", (user_id,))


def admin_count(conn) -> int:
    return db.one(conn, "SELECT COUNT(*) AS n FROM users WHERE role = 'admin'")["n"]


def password_problem(password: str, confirm: Optional[str] = None) -> Optional[str]:
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if confirm is not None and password != confirm:
        return "The two passwords don't match."
    return None


def create_user(conn, email: str, name: str, role: str, actor: str,
                password: Optional[str] = None) -> NewLink:
    """Create an account. Without a password, it gets a setup link instead,
    returned once and stored only as a hash."""
    from werkzeug.security import generate_password_hash
    if role not in ROLES:
        raise ValueError(role)
    token = None if password else secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO users (email, name, role, password_hash, setup_token,
                                          setup_expires, last_login)
                       VALUES (lower(%s), %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (email.strip(), name.strip(), role,
                     generate_password_hash(password) if password else None,
                     _token_hash(token) if token else None,
                     now + timedelta(hours=SETUP_LINK_HOURS) if token else None,
                     now if password else None))
        user_id = cur.fetchone()[0]
    history_add(conn, actor, f"Added {name.strip()} ({role})"
                + ("" if password else " and created a setup link"))
    return NewLink(user_id, token or "")


def new_setup_link(conn, user_id: int, actor: str) -> NewLink:
    token = secrets.token_urlsafe(32)
    with conn.cursor() as cur:
        cur.execute("""UPDATE users SET setup_token = %s, setup_expires = %s
                        WHERE id = %s RETURNING name""",
                    (_token_hash(token), datetime.now(timezone.utc) + timedelta(hours=SETUP_LINK_HOURS),
                     user_id))
        name = cur.fetchone()[0]
    history_add(conn, actor, f"Created a setup link for {name}")
    return NewLink(user_id, token)


def user_by_token(conn, token: str) -> Optional[dict]:
    return db.one(conn, """SELECT id, email, name, role FROM users
                            WHERE setup_token = %s AND setup_expires > NOW()""", (_token_hash(token),))


def set_password(conn, user_id: int, password: str) -> None:
    from werkzeug.security import generate_password_hash
    db.execute(conn, """UPDATE users SET password_hash = %s, setup_token = NULL, setup_expires = NULL,
                               last_login = NOW() WHERE id = %s""",
               (generate_password_hash(password), user_id))


_dummy: list[str] = []


def _dummy_hash() -> str:
    if not _dummy:
        from werkzeug.security import generate_password_hash
        _dummy.append(generate_password_hash(secrets.token_hex(16)))
    return _dummy[0]


def check_login(conn, email: str, password: str) -> Optional[dict]:
    from werkzeug.security import check_password_hash
    row = db.one(conn, "SELECT id, name, role, password_hash FROM users WHERE email = lower(%s)",
                 (email.strip(),))
    # Hash something either way, so a wrong email takes as long as a wrong password.
    stored = row["password_hash"] if row and row["password_hash"] else _dummy_hash()
    if not check_password_hash(stored, password) or not row or not row["password_hash"]:
        return None
    db.execute(conn, "UPDATE users SET last_login = NOW() WHERE id = %s", (row["id"],))
    return {k: row[k] for k in ("id", "name", "role")}


def set_role(conn, user_id: int, role: str, actor: str) -> Optional[str]:
    """Returns a reason when the change isn't allowed."""
    if role not in ROLES:
        return "Unknown role."
    target = user(conn, user_id)
    if not target or target["role"] == role:
        return None
    if target["role"] == "admin" and admin_count(conn) <= 1:
        return "There must always be at least one admin."
    db.execute(conn, "UPDATE users SET role = %s WHERE id = %s", (role, user_id))
    history_add(conn, actor, f"{target['name']}: role changed from {target['role']} to {role}")
    return None


def delete_user(conn, user_id: int, actor: str) -> Optional[str]:
    target = user(conn, user_id)
    if not target:
        return None
    if target["role"] == "admin" and admin_count(conn) <= 1:
        return "There must always be at least one admin."
    db.execute(conn, "DELETE FROM users WHERE id = %s", (user_id,))
    history_add(conn, actor, f"Removed {target['name']}")
    return None
