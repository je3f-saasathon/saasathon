"""GitHub App connect flow: prove a user can access an installation before any project
may use it (a raw installation id would let a user point a project at someone else's).

Install/authorize redirects back to our callback with a `code`; we exchange it for a
user-to-server token (kept in memory only) and ask GitHub which installations of our
App that user can access."""

import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import jwt
import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

STATE_PURPOSE = "sre_github_connect"
STATE_TTL_SECONDS = 600
API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


class ConnectError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def configured() -> bool:
    return bool(settings.GITHUB_APP_SLUG and settings.GITHUB_APP_CLIENT_ID
                and settings.GITHUB_APP_CLIENT_SECRET)


def make_state(user_id: int) -> str:
    payload = {"purpose": STATE_PURPOSE, "user_id": user_id, "nonce": secrets.token_urlsafe(8),
               "iat": int(time.time())}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def verify_state(state: str) -> int:
    """Returns the user id the flow was started for."""
    try:
        payload = jwt.decode(state, settings.SECRET_KEY, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise ConnectError("invalid_state")
    if payload.get("purpose") != STATE_PURPOSE or not isinstance(payload.get("user_id"), int):
        raise ConnectError("invalid_state")
    if time.time() - payload.get("iat", 0) > STATE_TTL_SECONDS:
        raise ConnectError("state_expired")
    return payload["user_id"]


def install_url(state: str) -> str:
    return f"https://github.com/apps/{settings.GITHUB_APP_SLUG}/installations/new?" + urlencode(
        {"state": state}
    )


def authorize_url(state: str) -> str:
    return "https://github.com/login/oauth/authorize?" + urlencode(
        {"client_id": settings.GITHUB_APP_CLIENT_ID, "state": state}
    )


def exchange_code(code: str) -> str:
    resp = requests.post(
        "https://github.com/login/oauth/access_token",
        data={"client_id": settings.GITHUB_APP_CLIENT_ID,
              "client_secret": settings.GITHUB_APP_CLIENT_SECRET, "code": code},
        headers={"Accept": "application/json"},
        timeout=10,
    )
    token = resp.json().get("access_token") if resp.status_code == 200 else None
    if not token:
        raise ConnectError("token_exchange_failed")
    return token


def _paginate(url: str, token: str, key: str) -> list[dict]:
    items, page = [], 1
    while True:
        resp = requests.get(url, headers={**HEADERS, "Authorization": f"Bearer {token}"},
                            params={"per_page": 100, "page": page}, timeout=10)
        if resp.status_code != 200:
            raise ConnectError("github_api_failed")
        batch = resp.json().get(key, [])
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def user_installations(user_token: str) -> list[dict]:
    """Installations of *our* App that the token's GitHub user can access."""
    installations = _paginate(f"{API}/user/installations", user_token, "installations")
    return [
        {"installation_id": str(i["id"]), "account_login": i["account"]["login"],
         "account_type": i["account"].get("type", "")}
        for i in installations if i.get("app_slug") == settings.GITHUB_APP_SLUG
    ]


def installation_token(installation_id: str) -> str:
    from github import Auth, GithubIntegration

    if not (settings.GITHUB_APP_ID and settings.GITHUB_APP_PRIVATE_KEY_PATH):
        raise ImproperlyConfigured("GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY_PATH not set")
    private_key = Path(settings.GITHUB_APP_PRIVATE_KEY_PATH).read_text()
    integration = GithubIntegration(auth=Auth.AppAuth(settings.GITHUB_APP_ID, private_key))
    return integration.get_access_token(int(installation_id)).token


def installation_repos(installation_id: str) -> list[dict]:
    repos = _paginate(f"{API}/installation/repositories", installation_token(installation_id),
                      "repositories")
    return [
        {"owner": r["owner"]["login"], "name": r["name"],
         "default_branch": r.get("default_branch") or "main", "private": bool(r.get("private"))}
        for r in repos
    ]
