import requests
from django.conf import settings


class JevError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def run_jev(state: str, questions: dict) -> dict:
    """Calls the Jev model via the Cloudflare Workers AI REST API.

    Requires CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN to be set,
    and gateway balance (or BYOK) configured on the Cloudflare account.
    """
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{settings.CLOUDFLARE_ACCOUNT_ID}/ai/run"
    )
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {settings.CLOUDFLARE_API_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.CLOUDFLARE_JEV_MODEL,
            "input": {"state": state, "questions": questions},
        },
        timeout=30,
    )
    body = response.json()
    if not body.get("success"):
        message = "; ".join(e.get("message", "") for e in body.get("errors", []))
        raise JevError(message or "Jev request failed", response.status_code)
    return body["result"]["result"]
