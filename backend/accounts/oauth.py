"""Shared helpers for GitHub / Google OAuth login.

Both providers follow the same Authorization Code shape:
  1. /login builds a signed short-lived `state` and redirects to the
     provider's authorize URL.
  2. /callback verifies `state`, exchanges `code` for an access token
     server-side, fetches the verified profile/email, finds-or-creates
     the local user (linking on a verified email), issues an AuthToken,
     and redirects to the frontend with the token in the URL fragment.

Errors always redirect to `{FRONTEND_URL}/login?error=<code>` — never
raise raw exceptions back through the callback view, since that would
either leak details or dead-end the browser with a Django error page.
"""

import time

import jwt
import requests
from django.conf import settings

from .models import User

STATE_TTL_SECONDS = 300


class OAuthError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def make_state(provider: str) -> str:
    payload = {"provider": provider, "iat": int(time.time())}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def verify_state(provider: str, state: str) -> None:
    try:
        payload = jwt.decode(state, settings.SECRET_KEY, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise OAuthError("invalid_state")

    if payload.get("provider") != provider:
        raise OAuthError("invalid_state")

    issued_at = payload.get("iat", 0)
    if time.time() - issued_at > STATE_TTL_SECONDS:
        raise OAuthError("state_expired")


def github_enabled() -> bool:
    return bool(settings.GITHUB_CLIENT_ID and settings.GITHUB_CLIENT_SECRET)


def google_enabled() -> bool:
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def github_authorize_url(state: str) -> str:
    params = {
        "client_id": settings.GITHUB_CLIENT_ID,
        "redirect_uri": settings.GITHUB_REDIRECT_URI,
        "scope": "read:user user:email",
        "state": state,
        "allow_signup": "true",
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    return f"https://github.com/login/oauth/authorize?{query}"


def github_exchange_code(code: str) -> str:
    resp = requests.post(
        "https://github.com/login/oauth/access_token",
        data={
            "client_id": settings.GITHUB_CLIENT_ID,
            "client_secret": settings.GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": settings.GITHUB_REDIRECT_URI,
        },
        headers={"Accept": "application/json"},
        timeout=10,
    )
    if resp.status_code != 200:
        raise OAuthError("github_token_exchange_failed")
    data = resp.json()
    access_token = data.get("access_token")
    if not access_token:
        raise OAuthError("github_token_exchange_failed")
    return access_token


def github_fetch_identity(access_token: str) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    user_resp = requests.get("https://api.github.com/user", headers=headers, timeout=10)
    if user_resp.status_code != 200:
        raise OAuthError("github_profile_fetch_failed")
    profile = user_resp.json()

    email = profile.get("email")
    verified = bool(email)

    emails_resp = requests.get(
        "https://api.github.com/user/emails", headers=headers, timeout=10
    )
    if emails_resp.status_code == 200:
        for entry in emails_resp.json():
            if entry.get("primary") and entry.get("verified"):
                email = entry.get("email")
                verified = True
                break
        else:
            if not verified:
                for entry in emails_resp.json():
                    if entry.get("verified"):
                        email = entry.get("email")
                        verified = True
                        break

    if not email or not verified:
        raise OAuthError("github_email_not_verified")

    return {
        "provider_id": str(profile.get("id")),
        "email": email,
        "email_verified": verified,
        "name": profile.get("name") or profile.get("login") or "",
        "avatar_url": profile.get("avatar_url") or "",
    }


def google_authorize_url(state: str) -> str:
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


def google_exchange_code(code: str) -> dict:
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "code": code,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        raise OAuthError("google_token_exchange_failed")
    return resp.json()


def google_fetch_identity(token_response: dict) -> dict:
    access_token = token_response.get("access_token")
    if not access_token:
        raise OAuthError("google_token_exchange_failed")

    resp = requests.get(
        "https://openidconnect.googleapis.com/v1/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if resp.status_code != 200:
        raise OAuthError("google_profile_fetch_failed")
    info = resp.json()

    email = info.get("email")
    email_verified = info.get("email_verified")
    # Google may return this as a bool or the string "true".
    email_verified = email_verified is True or email_verified == "true"

    if not email or not email_verified:
        raise OAuthError("google_email_not_verified")

    return {
        "provider_id": info.get("sub"),
        "email": email,
        "email_verified": email_verified,
        "name": info.get("name") or "",
        "avatar_url": info.get("picture") or "",
    }


def find_or_create_user(*, provider: str, identity: dict) -> User:
    """Account linking rule: identity key is email. A VERIFIED oauth
    email that matches an existing user links the provider id to that
    user. Never link on an unverified email (callers must already have
    guaranteed verified=True before calling this). New users get an
    unusable password (OAuth-only)."""

    email = identity["email"].lower()
    provider_field = "github_id" if provider == "github" else "google_sub"
    provider_id = identity["provider_id"]

    user = User.objects.filter(**{provider_field: provider_id}).first()
    if user:
        return user

    user = User.objects.filter(email__iexact=email).first()
    if user:
        setattr(user, provider_field, provider_id)
        if not user.avatar_url and identity.get("avatar_url"):
            user.avatar_url = identity["avatar_url"]
        if not user.name and identity.get("name"):
            user.name = identity["name"]
        user.save()
        return user

    user = User(
        email=email,
        name=identity.get("name") or "",
        avatar_url=identity.get("avatar_url") or "",
        **{provider_field: provider_id},
    )
    user.set_unusable_password()
    user.save()
    return user
