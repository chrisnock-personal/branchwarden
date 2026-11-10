"""
github.py
~~~~~~~~~
Talking to GitHub: as a GitHub App installation or with a personal access
token, over GraphQL for scanning and REST for the few writes (requesting
reviewers) and lookups (installations, org members and teams).

Branchwarden never pushes, and never deletes branches.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import jwt
import requests

log = logging.getLogger("branchwarden.github")

TIMEOUT = 30
from version import VERSION

USER_AGENT = f"Branchwarden/{VERSION}"


class GitHubError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


@dataclass
class RateLimit:
    remaining: Optional[int] = None
    limit: Optional[int] = None
    reset_at: Optional[str] = None


def graphql_url(api_url: str) -> str:
    api = api_url.rstrip("/")
    if api.endswith("/api/v3"):           # GitHub Enterprise Server
        return api[: -len("/v3")] + "/graphql"
    return api + "/graphql"


def web_url(api_url: str) -> str:
    """github.com, or a GitHub Enterprise Server's own address, from its API address."""
    api = api_url.rstrip("/")
    if api in ("https://api.github.com", ""):
        return "https://github.com"
    return api[: -len("/api/v3")] if api.endswith("/api/v3") else api


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    """Check a webhook's X-Hub-Signature-256 header."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def app_jwt(app_id: str, private_key: str, now: Optional[float] = None) -> str:
    now = int(now or time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, private_key,
                      algorithm="RS256")


# ─── A connection: one token ────────────────────────────────────────────────

class Client:
    """REST and GraphQL calls with one bearer token."""

    def __init__(self, api_url: str, token_source, label: str):
        self.api_url = api_url.rstrip("/")
        self._token_source = token_source   # callable returning a token
        self.label = label                  # "app" or "token"
        self.rate = RateLimit()
        self.token_expires: Optional[str] = None
        self.session = requests.Session()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token_source()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": USER_AGENT}

    def _note(self, resp: requests.Response) -> None:
        if resp.headers.get("x-ratelimit-remaining"):
            try:
                self.rate.remaining = int(resp.headers["x-ratelimit-remaining"])
                self.rate.limit = int(resp.headers.get("x-ratelimit-limit", "0")) or None
            except ValueError:
                pass
        if resp.headers.get("github-authentication-token-expiration"):
            self.token_expires = resp.headers["github-authentication-token-expiration"]

    def rest(self, method: str, path: str, **kwargs) -> requests.Response:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        try:
            resp = self.session.request(method, url, headers=self._headers(), timeout=TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            raise GitHubError(f"can't reach GitHub ({type(exc).__name__})") from None
        self._note(resp)
        if resp.status_code >= 400:
            raise GitHubError(_error_text(resp), resp.status_code)
        return resp

    def paged(self, path: str, limit: int = 1000) -> list:
        """Every item of a paginated REST list (up to limit)."""
        out: list = []
        url = f"{self.api_url}{path}{'&' if '?' in path else '?'}per_page=100"
        while url and len(out) < limit:
            resp = self.rest("GET", url)
            data = resp.json()
            out.extend(data if isinstance(data, list) else data.get("installations", []))
            url = resp.links.get("next", {}).get("url")
        return out

    def graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        try:
            resp = self.session.post(graphql_url(self.api_url), headers=self._headers(), timeout=60,
                                     json={"query": query, "variables": variables or {}})
        except requests.RequestException as exc:
            raise GitHubError(f"can't reach GitHub ({type(exc).__name__})") from None
        self._note(resp)
        if resp.status_code >= 400:
            raise GitHubError(_error_text(resp), resp.status_code)
        body = resp.json()
        data = body.get("data") or {}
        if body.get("errors"):
            msgs = "; ".join(e.get("message", "?") for e in body["errors"])
            types = {e.get("type") for e in body["errors"]}
            # NOT_FOUND or FORBIDDEN inside an otherwise good answer (a deleted
            # team, or a team the token can't see) leaves that part empty.
            if not data or not types <= {"NOT_FOUND", "FORBIDDEN"}:
                raise GitHubError(msgs)
        rl = data.get("rateLimit")
        if rl:
            self.rate = RateLimit(rl.get("remaining"), rl.get("limit"), rl.get("resetAt"))
        return data

    # ── The calls Branchwarden makes ──

    def request_reviewers(self, full_name: str, number: int, users: list[str], teams: list[str]) -> dict:
        body: dict = {}
        if users:
            body["reviewers"] = users
        if teams:
            body["team_reviewers"] = [t.split("/", 1)[-1] for t in teams]
        return self.rest("POST", f"/repos/{full_name}/pulls/{number}/requested_reviewers", json=body).json()

    def remove_reviewers(self, full_name: str, number: int, users: list[str], teams: list[str]) -> None:
        body = {"reviewers": users, "team_reviewers": [t.split("/", 1)[-1] for t in teams]}
        self.rest("DELETE", f"/repos/{full_name}/pulls/{number}/requested_reviewers", json=body)

    def org_members(self, org: str) -> list[str]:
        return sorted(m["login"].lower() for m in self.paged(f"/orgs/{org}/members"))

    def org_teams(self, org: str) -> list[dict]:
        return [{"slug": f"{org}/{t['slug']}".lower(), "name": t.get("name") or t["slug"]}
                for t in self.paged(f"/orgs/{org}/teams")]

    def team_members(self, team_slug: str) -> list[str]:
        org, _, slug = team_slug.partition("/")
        return sorted(m["login"].lower() for m in self.paged(f"/orgs/{org}/teams/{slug}/members"))

    def collaborators(self, full_name: str) -> list[str]:
        return sorted(m["login"].lower() for m in self.paged(f"/repos/{full_name}/collaborators"))

    def viewer(self) -> dict:
        return self.rest("GET", "/user").json()


def _error_text(resp: requests.Response) -> str:
    try:
        body = resp.json()
        msg = body.get("message", "")
        errors = body.get("errors")
        if errors:
            details = "; ".join(e.get("message", str(e)) if isinstance(e, dict) else str(e) for e in errors)
            msg = f"{msg}: {details}" if msg else details
    except ValueError:
        msg = resp.text[:200]
    return f"HTTP {resp.status_code}" + (f", {msg}" if msg else "")


# ─── The App ────────────────────────────────────────────────────────────────

@dataclass
class App:
    api_url: str
    app_id: str
    private_key: str
    _installations: dict = field(default_factory=dict)     # login -> installation id
    _installations_at: float = 0.0
    _tokens: dict = field(default_factory=dict)            # installation id -> (token, expiry epoch)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _app_client(self) -> Client:
        return Client(self.api_url, lambda: app_jwt(self.app_id, self.private_key), "app")

    def details(self) -> dict:
        return self._app_client().rest("GET", "/app").json()

    def installations(self, refresh: bool = False) -> dict[str, dict]:
        """{account login: {id, kind, repos ('all'|'selected'), permissions}}, cached 10 minutes."""
        with self._lock:
            if refresh or not self._installations or time.time() - self._installations_at > 600:
                found = {}
                for inst in self._app_client().paged("/app/installations"):
                    acct = inst.get("account") or {}
                    found[acct.get("login", "").lower()] = {
                        "id": inst["id"], "kind": "org" if acct.get("type") == "Organization" else "user",
                        "repos": inst.get("repository_selection", "all"),
                        "permissions": inst.get("permissions", {})}
                self._installations = found
                self._installations_at = time.time()
            return dict(self._installations)

    def _token(self, installation_id: int) -> str:
        with self._lock:
            token, expires = self._tokens.get(installation_id, ("", 0))
            if token and expires - time.time() > 300:
                return token
            resp = self._app_client().rest("POST", f"/app/installations/{installation_id}/access_tokens")
            data = resp.json()
            exp = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
            self._tokens[installation_id] = (data["token"], exp)
            return data["token"]

    def client_for(self, login: str) -> Optional[Client]:
        inst = self.installations().get(login.lower())
        if not inst:
            return None
        return Client(self.api_url, lambda: self._token(inst["id"]), "app")


# ─── Choosing a connection ──────────────────────────────────────────────────

class Connections:
    """The App and the token, as currently set; picks one per account."""

    def __init__(self, values: dict):
        import settings  # here, so tests can use this module without a database
        self.api_url = settings.get(values, "GITHUB_API_URL") or "https://api.github.com"
        self.app: Optional[App] = None
        self.token: Optional[Client] = None
        if settings.flag(values, "GITHUB_APP_ENABLED") and values.get("GITHUB_APP_ID") \
                and values.get("GITHUB_APP_PRIVATE_KEY"):
            self.app = App(self.api_url, values["GITHUB_APP_ID"], values["GITHUB_APP_PRIVATE_KEY"])
        if settings.flag(values, "GITHUB_TOKEN_ENABLED") and values.get("GITHUB_TOKEN"):
            pat = values["GITHUB_TOKEN"]
            self.token = Client(self.api_url, lambda: pat, "token")

    @property
    def configured(self) -> bool:
        return bool(self.app or self.token)

    def client_for(self, login: str, via: str = "auto") -> Client:
        """The App's installation on this account if there is one (and via
        allows it), otherwise the token."""
        problems = []
        if via in ("auto", "app") and self.app:
            try:
                c = self.app.client_for(login)
                if c:
                    return c
                problems.append(f"the GitHub App isn't installed on {login}")
            except GitHubError as exc:
                problems.append(f"GitHub App: {exc}")
        elif via == "app":
            problems.append("the GitHub App isn't set up")
        if via in ("auto", "token") and self.token:
            return self.token
        if via == "token":
            problems.append("no personal access token is set")
        if not problems:
            problems.append("neither a GitHub App nor a token is set up")
        raise GitHubError("; ".join(problems))
