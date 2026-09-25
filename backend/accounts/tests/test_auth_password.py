import json

import pytest

from accounts.models import AuthToken, User

pytestmark = pytest.mark.django_db


def post_json(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type="application/json")


def test_register_then_login_returns_tokens(client):
    resp = post_json(
        client,
        "/api/auth/register",
        {"email": "New@Example.com", "password": "s3curePassw0rd!", "name": "New User"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["user"]["email"] == "new@example.com"
    assert body["user"]["name"] == "New User"

    resp = post_json(
        client, "/api/auth/login", {"email": "new@example.com", "password": "s3curePassw0rd!"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["user"]["email"] == "new@example.com"


def test_login_wrong_password_returns_401(client, make_user):
    make_user(email="a@example.com", password="rightpassword1")
    resp = post_json(client, "/api/auth/login", {"email": "a@example.com", "password": "wrong"})
    assert resp.status_code == 401
    assert "detail" in resp.json()


def test_login_unknown_user_returns_401(client):
    resp = post_json(
        client, "/api/auth/login", {"email": "nobody@example.com", "password": "whatever123"}
    )
    assert resp.status_code == 401


def test_duplicate_email_registration_rejected(client, make_user):
    make_user(email="dupe@example.com")
    resp = post_json(
        client,
        "/api/auth/register",
        {"email": "dupe@example.com", "password": "anotherpassword1"},
    )
    assert resp.status_code == 400


def test_me_requires_valid_token(client, make_user):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401

    user = make_user()
    token, raw = AuthToken.issue(user)
    resp = client.get("/api/auth/me", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert resp.status_code == 200
    assert resp.json()["email"] == user.email


def test_me_rejects_expired_or_revoked_token(client, make_user):
    from django.utils import timezone
    from datetime import timedelta

    user = make_user()
    token, raw = AuthToken.issue(user)

    token.expires_at = timezone.now() - timedelta(days=1)
    token.save()
    resp = client.get("/api/auth/me", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert resp.status_code == 401

    token2, raw2 = AuthToken.issue(user)
    token2.revoke()
    resp = client.get("/api/auth/me", HTTP_AUTHORIZATION=f"Bearer {raw2}")
    assert resp.status_code == 401


def test_logout_revokes_token(client, make_user):
    user = make_user()
    token, raw = AuthToken.issue(user)

    resp = client.post("/api/auth/logout", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    token.refresh_from_db()
    assert token.is_revoked

    resp = client.get("/api/auth/me", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert resp.status_code == 401


def test_providers_reflects_env(client, settings):
    settings.GITHUB_CLIENT_ID = "id"
    settings.GITHUB_CLIENT_SECRET = "secret"
    settings.GOOGLE_CLIENT_ID = ""
    settings.GOOGLE_CLIENT_SECRET = ""

    resp = client.get("/api/auth/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"password": True, "github": True, "google": False}


def test_disabled_provider_returns_helpful_error(client, settings):
    settings.GITHUB_CLIENT_ID = ""
    settings.GITHUB_CLIENT_SECRET = ""

    resp = client.get("/api/auth/github/login")
    assert resp.status_code == 400
    assert "docs/AUTH.md" in resp.json()["detail"]
