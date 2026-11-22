"""
access.py
~~~~~~~~~
Who may do what: one rule for the dashboard and the API, so the two can't drift.

- read:  viewers and up see everything outside Settings.
- act:   members and up request reviewers, snooze reminders, ignore branches, start scans.
- admin: admins change settings and manage people.
- self:  anyone manages their own things (their API tokens), as on the dashboard.

An API token can also be read-only: then it only reads, whatever its owner's role.
"""

from __future__ import annotations

LEVEL = {"viewer": 0, "member": 1, "admin": 2}
NEEDS = {"read": "viewer", "act": "member", "admin": "admin", "self": "viewer"}
SCOPES = ("read", "write")


def allowed(role: str, need: str, scope: str = "write") -> bool:
    if need not in NEEDS:
        raise ValueError(need)
    if scope == "read" and need != "read":
        return False
    return LEVEL.get(role, -1) >= LEVEL[NEEDS[need]]
