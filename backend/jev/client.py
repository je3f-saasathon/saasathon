import requests
from django.conf import settings


class JevError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def run_jev(
    state: str,
    questions: dict,
    *,
    account_id: str = "",
    api_token: str = "",
    model: str = "",
) -> dict:
    """Calls the Jev model via the Cloudflare Workers AI REST API.

    Credentials default to CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN / CLOUDFLARE_JEV_MODEL;
    callers with their own (e.g. a user's SRE model config) pass them in. The account also
    needs gateway balance (or BYOK), otherwise Cloudflare answers 402-style errors.
    """
    account_id = account_id or settings.CLOUDFLARE_ACCOUNT_ID
    api_token = api_token or settings.CLOUDFLARE_API_TOKEN
    if not account_id or not api_token:
        raise JevError("Jev is not configured: missing Cloudflare account id or API token", 400)
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run"
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
            json={
                "model": model or settings.CLOUDFLARE_JEV_MODEL,
                "input": {"state": state, "questions": questions},
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise JevError(f"Jev request failed: {exc}", 503) from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise JevError(
            f"Jev returned non-JSON (HTTP {response.status_code})", response.status_code
        ) from exc
    if not body.get("success"):
        message = "; ".join(e.get("message", "") for e in body.get("errors", []))
        raise JevError(message or "Jev request failed", response.status_code)
    return body["result"]["result"]
