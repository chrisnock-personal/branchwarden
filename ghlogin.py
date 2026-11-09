"""
ghlogin.py
~~~~~~~~~~
Sign in with GitHub (OAuth, with PKCE). Works with an OAuth App or with the
GitHub App's own client ID and secret.

The role comes from the person's GitHub teams at every sign-in, using three
lists in Settings › Users and access: admins, members and viewers. Each list
holds logins, org/team slugs or whole organisations. Anyone who matches none
of them is turned away: nobody gets in just by having a GitHub account. The
GitHub token is used only during sign-in and never stored.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Optional
from urllib.parse import urlencode

import requests

from github import GitHubError, USER_AGENT, web_url

TIMEOUT = 20
ROLE_ORDER = ("admin", "member", "viewer")


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_url(api_url: str, client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return f"{web_url(api_url)}/login/oauth/authorize?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "state": state, "scope": "read:org",
        "code_challenge": challenge, "code_challenge_method": "S256", "allow_signup": "false"})


def exchange(api_url: str, client_id: str, client_secret: str, code: str, redirect_uri: str, verifier: str) -> str:
    try:
        resp = requests.post(f"{web_url(api_url)}/login/oauth/access_token", timeout=TIMEOUT,
                             headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                             data={"client_id": client_id, "client_secret": client_secret, "code": code,
                                   "redirect_uri": redirect_uri, "code_verifier": verifier})
    except requests.RequestException as exc:
        raise GitHubError(f"can't reach GitHub ({type(exc).__name__})") from None
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code >= 400 or not body.get("access_token"):
        raise GitHubError(body.get("error_description") or body.get("error") or f"HTTP {resp.status_code}")
    return body["access_token"]


def identity(api_url: str, token: str) -> dict:
    """{id, login, name, teams: {org/team}, orgs: {org}}"""
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                      "User-Agent": USER_AGENT})

    def get(path: str):
        out, url = [], f"{api_url.rstrip('/')}{path}{'&' if '?' in path else '?'}per_page=100"
        while url:
            try:
                r = s.get(url, timeout=TIMEOUT)
            except requests.RequestException as exc:
                raise GitHubError(f"can't reach GitHub ({type(exc).__name__})") from None
            if r.status_code >= 400:
                raise GitHubError(f"HTTP {r.status_code} on {path}")
            data = r.json()
            if isinstance(data, dict):
                return data
            out.extend(data)
            url = r.links.get("next", {}).get("url")
        return out

    user = get("/user")
    teams, orgs = set(), set()
    try:
        for t in get("/user/teams"):
            org = (t.get("organization") or {}).get("login", "").lower()
            teams.add(f"{org}/{t['slug']}".lower())
            orgs.add(org)
        for m in get("/user/memberships/orgs?state=active"):
            orgs.add((m.get("organization") or {}).get("login", "").lower())
    except GitHubError:
        pass     # no read:org: only logins in the role lists can match
    return {"id": user["id"], "login": user["login"].lower(), "name": user.get("name") or user["login"],
            "teams": teams, "orgs": orgs - {""}}


def role_for(who: dict, mapping: dict[str, list[str]]) -> Optional[str]:
    """The highest role whose list names this login, one of their teams, or one of their orgs."""
    for role in ROLE_ORDER:
        for entry in mapping.get(role, []):
            e = entry.strip().lstrip("@").lower()
            if not e:
                continue
            if "/" in e:
                if e in who["teams"]:
                    return role
            elif e == who["login"] or e in who["orgs"]:
                return role
    return None


def noreply_email(user_id: int, login: str) -> str:
    """GitHub's own no-reply address: unique, and never a real inbox."""
    return f"{user_id}+{login}@users.noreply.github.com"
