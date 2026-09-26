import json
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import AuthToken, CliLogin

pytestmark = pytest.mark.django_db


def post_json(client, path, payload, token=None):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
    return client.post(path, data=json.dumps(payload), content_type="application/json", **headers)


@pytest.fixture
def user_token(make_user):
    user = make_user()
    return user, AuthToken.issue(user)[1]


def start(client):
    resp = client.post("/api/auth/cli/start")
    assert resp.status_code == 200
    return resp.json()


def test_start_approve_poll_issues_one_token(client, user_token, settings):
    settings.FRONTEND_URL = "http://frontend"
    user, token = user_token
    body = start(client)
    assert body["verification_url"] == f"http://frontend/cli?code={body['user_code']}"
    assert body["expires_in"] == 600

    poll = post_json(client, "/api/auth/cli/poll", {"device_code": body["device_code"]})
    assert poll.status_code == 202 and poll.json() == {"status": "pending"}

    # Typed by hand: lower case, no dash.
    typed = body["user_code"].replace("-", "").lower()
    assert post_json(client, "/api/auth/cli/approve", {"user_code": typed}, token).status_code == 200

    poll = post_json(client, "/api/auth/cli/poll", {"device_code": body["device_code"]})
    assert poll.status_code == 200
    assert poll.json()["user"]["email"] == user.email
    assert AuthToken.get_valid(poll.json()["token"]).user == user
    # Used up: a second poll gets nothing.
    assert post_json(client, "/api/auth/cli/poll", {"device_code": body["device_code"]}).status_code == 410
    assert not CliLogin.objects.exists()


def test_approve_needs_a_signed_in_user(client):
    body = start(client)
    assert post_json(client, "/api/auth/cli/approve", {"user_code": body["user_code"]}).status_code == 401


def test_a_code_is_approved_once(client, user_token, make_user):
    _, token = user_token
    other = AuthToken.issue(make_user(email="other@example.com"))[1]
    body = start(client)
    assert post_json(client, "/api/auth/cli/approve", {"user_code": body["user_code"]}, token).status_code == 200
    assert post_json(client, "/api/auth/cli/approve", {"user_code": body["user_code"]}, other).status_code == 409


def test_unknown_or_expired_codes(client, user_token):
    _, token = user_token
    assert post_json(client, "/api/auth/cli/approve", {"user_code": "AAAA-BBBB"}, token).status_code == 404
    assert post_json(client, "/api/auth/cli/poll", {"device_code": "nope"}).status_code == 410

    body = start(client)
    CliLogin.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert post_json(client, "/api/auth/cli/approve", {"user_code": body["user_code"]}, token).status_code == 404
    assert post_json(client, "/api/auth/cli/poll", {"device_code": body["device_code"]}).status_code == 410
    # Expired rows are cleaned up by the next start.
    start(client)
    assert CliLogin.objects.count() == 1
