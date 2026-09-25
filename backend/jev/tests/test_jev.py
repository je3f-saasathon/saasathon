import json

import pytest
import responses

from accounts.models import AuthToken

pytestmark = pytest.mark.django_db


def auth_header(user):
    _, raw = AuthToken.issue(user)
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def payload():
    return {
        "state": "Help! My payouts have been failing for 3 days.",
        "questions": {
            "department": {
                "type": "choice",
                "instructions": "Which team should handle this?",
                "criteria": {"billing": "Payments", "technical": "Bugs"},
            }
        },
    }


@responses.activate
def test_run_returns_jev_result(client, make_user, settings):
    settings.CLOUDFLARE_ACCOUNT_ID = "test-account"
    settings.CLOUDFLARE_API_TOKEN = "test-token"
    user = make_user(email="agent@example.com")

    responses.add(
        responses.POST,
        "https://api.cloudflare.com/client/v4/accounts/test-account/ai/run",
        json={
            "success": True,
            "errors": [],
            "result": {
                "result": {
                    "model": "jev-1.13.0",
                    "answers": {"department": {"type": "choice", "choice": "billing"}},
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                }
            },
        },
        status=200,
    )

    resp = client.post(
        "/api/jev/run",
        data=json.dumps(payload()),
        content_type="application/json",
        **auth_header(user),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "jev-1.13.0"
    assert body["answers"]["department"]["choice"] == "billing"


@responses.activate
def test_run_surfaces_upstream_error(client, make_user, settings):
    settings.CLOUDFLARE_ACCOUNT_ID = "test-account"
    settings.CLOUDFLARE_API_TOKEN = "test-token"
    user = make_user(email="agent2@example.com")

    responses.add(
        responses.POST,
        "https://api.cloudflare.com/client/v4/accounts/test-account/ai/run",
        json={
            "success": False,
            "errors": [{"message": "Insufficient balance; add money to your gateway or use BYOK"}],
            "result": {},
        },
        status=402,
    )

    resp = client.post(
        "/api/jev/run",
        data=json.dumps(payload()),
        content_type="application/json",
        **auth_header(user),
    )
    assert resp.status_code == 502
    assert "Insufficient balance" in resp.json()["detail"]


def test_run_requires_auth(client):
    resp = client.post(
        "/api/jev/run",
        data=json.dumps(payload()),
        content_type="application/json",
    )
    assert resp.status_code == 401
