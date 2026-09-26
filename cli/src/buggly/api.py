"""A tiny JSON client for the buggly API (stdlib only)."""

import json
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "buggly-cli"  # Cloudflare blocks Python's default User-Agent


class ApiError(Exception):
    def __init__(self, status: int, detail: str, body: dict | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.body = body or {}


def call(base_url: str, method: str, path: str, body: dict | None = None, token: str = "",
         params: dict | None = None) -> tuple[int, dict]:
    url = f"{base_url}/api{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, _json(resp.read())
    except urllib.error.HTTPError as exc:
        payload = _json(exc.read())
        raise ApiError(exc.code, payload.get("detail") or exc.reason, payload) from None
    except urllib.error.URLError as exc:
        raise ApiError(0, f"Could not reach {base_url}: {exc.reason}") from None


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
