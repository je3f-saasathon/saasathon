import json
import time

import jwt
import pytest
import responses
from django.conf import settings

from accounts.models import AuthToken, User
from accounts.oauth import make_state

pytestmark = pytest.mark.django_db


def _extract_token_from_redirect(location: str) -> str:
    assert "#token=" in location
    return location.split("#token=")[1]


@responses.activate
def test_github_callback_creates_user_and_token(client):
    responses.add(
        responses.POST,
        "https://github.com/login/oauth/access_token",
        json={"access_token": "gh-access-token"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.github.com/user",
        json={"id": 555, "login": "octocat", "name": "Octo Cat", "avatar_url": "http://x/a.png"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.github.com/user/emails",
        json=[{"email": "octo@example.com", "primary": True, "verified": True}],
        status=200,
    )

    state = make_state("github")
    resp = client.get(f"/api/auth/github/callback?code=abc&state={state}")
    assert resp.status_code == 302
    token = _extract_token_from_redirect(resp["Location"])

    user = User.objects.get(email="octo@example.com")
    assert user.github_id == "555"
    assert AuthToken.get_valid(token).user == user


@responses.activate
def test_github_callback_links_existing_verified_email(client, make_user):
    existing = make_user(email="octo@example.com", password="somepassword1")
    assert existing.github_id is None

    responses.add(
        responses.POST,
        "https://github.com/login/oauth/access_token",
        json={"access_token": "gh-access-token"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.github.com/user",
        json={"id": 999, "login": "octocat", "name": "", "avatar_url": ""},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.github.com/user/emails",
        json=[{"email": "octo@example.com", "primary": True, "verified": True}],
        status=200,
    )

    state = make_state("github")
    resp = client.get(f"/api/auth/github/callback?code=abc&state={state}")
    assert resp.status_code == 302

    existing.refresh_from_db()
    assert existing.github_id == "999"
    assert User.objects.filter(email="octo@example.com").count() == 1


@responses.activate
def test_github_callback_does_not_link_unverified_email(client, make_user):
    existing = make_user(email="octo@example.com", password="somepassword1")

    responses.add(
        responses.POST,
        "https://github.com/login/oauth/access_token",
        json={"access_token": "gh-access-token"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.github.com/user",
        json={"id": 111, "login": "octocat", "name": "", "avatar_url": ""},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.github.com/user/emails",
        json=[{"email": "octo@example.com", "primary": True, "verified": False}],
        status=200,
    )

    state = make_state("github")
    resp = client.get(f"/api/auth/github/callback?code=abc&state={state}")
    assert resp.status_code == 302
    assert "error=" in resp["Location"]

    existing.refresh_from_db()
    assert existing.github_id is None


def test_github_callback_rejects_state_mismatch(client):
    bad_state = jwt.encode(
        {"provider": "google", "iat": int(time.time())}, settings.SECRET_KEY, algorithm="HS256"
    )
    resp = client.get(f"/api/auth/github/callback?code=abc&state={bad_state}")
    assert resp.status_code == 302
    assert "error=invalid_state" in resp["Location"]


@responses.activate
def test_google_callback_creates_user_and_token(client):
    responses.add(
        responses.POST,
        "https://oauth2.googleapis.com/token",
        json={"access_token": "g-access-token"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://openidconnect.googleapis.com/v1/userinfo",
        json={
            "sub": "g-123",
            "email": "googler@example.com",
            "email_verified": True,
            "name": "Googler",
            "picture": "http://x/p.png",
        },
        status=200,
    )

    state = make_state("google")
    resp = client.get(f"/api/auth/google/callback?code=abc&state={state}")
    assert resp.status_code == 302
    token = _extract_token_from_redirect(resp["Location"])

    user = User.objects.get(email="googler@example.com")
    assert user.google_sub == "g-123"
    assert AuthToken.get_valid(token).user == user


@responses.activate
def test_google_callback_unverified_email_rejected(client):
    responses.add(
        responses.POST,
        "https://oauth2.googleapis.com/token",
        json={"access_token": "g-access-token"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://openidconnect.googleapis.com/v1/userinfo",
        json={
            "sub": "g-456",
            "email": "unverified@example.com",
            "email_verified": False,
            "name": "Nope",
        },
        status=200,
    )

    state = make_state("google")
    resp = client.get(f"/api/auth/google/callback?code=abc&state={state}")
    assert resp.status_code == 302
    assert "error=" in resp["Location"]
    assert not User.objects.filter(email="unverified@example.com").exists()
