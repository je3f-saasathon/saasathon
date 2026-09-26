from django.conf import settings
from django.contrib.auth import authenticate
from django.core.cache import cache
from django.http import HttpRequest
from django.shortcuts import redirect
from ninja import Router
from ninja.errors import HttpError

from .auth import bearer_auth
from .models import AuthToken, User
from .oauth import (
    OAuthError,
    find_or_create_user,
    github_authorize_url,
    github_enabled,
    github_exchange_code,
    github_fetch_identity,
    google_authorize_url,
    google_enabled,
    google_exchange_code,
    google_fetch_identity,
    make_state,
    verify_state,
)
from .schemas import IssuedTokenOut, LoginIn, MeOut, OkOut, ProvidersOut, RegisterIn, TokenOut, user_to_out

router = Router(tags=["auth"])

LOGIN_THROTTLE_LIMIT = 5
LOGIN_THROTTLE_WINDOW_SECONDS = 60


def _throttle_key(request: HttpRequest, email: str) -> str:
    ip = request.META.get("REMOTE_ADDR", "unknown")
    return f"login-throttle:{ip}:{email.lower()}"


def _is_throttled(request: HttpRequest, email: str) -> bool:
    key = _throttle_key(request, email)
    attempts = cache.get(key, 0)
    return attempts >= LOGIN_THROTTLE_LIMIT


def _register_attempt(request: HttpRequest, email: str) -> None:
    key = _throttle_key(request, email)
    attempts = cache.get(key, 0)
    cache.set(key, attempts + 1, LOGIN_THROTTLE_WINDOW_SECONDS)


@router.post("/register", response={200: TokenOut, 400: dict}, auth=None)
def register(request, payload: RegisterIn):
    email = payload.email.strip().lower()
    if User.objects.filter(email__iexact=email).exists():
        raise HttpError(400, "An account with this email already exists")

    user = User.objects.create_user(
        email=email, password=payload.password, name=payload.name or ""
    )
    _, raw_token = AuthToken.issue(user)
    return 200, {"token": raw_token, "user": user_to_out(user)}


@router.post("/login", response={200: TokenOut, 401: dict}, auth=None)
def login(request, payload: LoginIn):
    email = payload.email.strip().lower()

    if _is_throttled(request, email):
        raise HttpError(401, "Too many login attempts. Try again shortly.")

    _register_attempt(request, email)

    user = authenticate(request, username=email, password=payload.password)
    if user is None or not user.is_active:
        # Generic failure message regardless of "no such user" vs
        # "wrong password" — avoids user enumeration.
        raise HttpError(401, "Invalid email or password")

    _, raw_token = AuthToken.issue(user)
    return 200, {"token": raw_token, "user": user_to_out(user)}


@router.get("/me", response=MeOut, auth=bearer_auth)
def me(request):
    return {"user": user_to_out(request.auth)}


@router.post("/tokens", response=IssuedTokenOut, auth=bearer_auth)
def issue_token(request):
    """A new token for scripts (the API, the SRE lab), shown once. It's separate from the
    browser's session, so logging out doesn't end it; it expires like any other token."""
    token, raw = AuthToken.issue(request.auth)
    return {"token": raw, "expires_at": token.expires_at}


@router.post("/logout", response=OkOut, auth=bearer_auth)
def logout(request):
    request.auth_token.revoke()
    return {"ok": True}


@router.get("/providers", response=ProvidersOut, auth=None)
def providers(request):
    return {
        "password": True,
        "github": github_enabled(),
        "google": google_enabled(),
    }


@router.get("/github/login", auth=None)
def github_login(request):
    if not github_enabled():
        raise HttpError(
            400,
            "GitHub login is not configured on this server. See docs/AUTH.md "
            "for how to set GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET.",
        )
    state = make_state("github")
    return redirect(github_authorize_url(state))


@router.get("/github/callback", auth=None)
def github_callback(request, code: str = "", state: str = ""):
    try:
        if not github_enabled():
            raise OAuthError("provider_disabled")
        verify_state("github", state)
        access_token = github_exchange_code(code)
        identity = github_fetch_identity(access_token)
        user = find_or_create_user(provider="github", identity=identity)
        _, raw_token = AuthToken.issue(user)
    except OAuthError as exc:
        return redirect(f"{settings.FRONTEND_URL}/login?error={exc.code}")

    return redirect(f"{settings.FRONTEND_URL}/auth/callback#token={raw_token}")


@router.get("/google/login", auth=None)
def google_login(request):
    if not google_enabled():
        raise HttpError(
            400,
            "Google login is not configured on this server. See docs/AUTH.md "
            "for how to set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.",
        )
    state = make_state("google")
    return redirect(google_authorize_url(state))


@router.get("/google/callback", auth=None)
def google_callback(request, code: str = "", state: str = ""):
    try:
        if not google_enabled():
            raise OAuthError("provider_disabled")
        verify_state("google", state)
        token_response = google_exchange_code(code)
        identity = google_fetch_identity(token_response)
        user = find_or_create_user(provider="google", identity=identity)
        _, raw_token = AuthToken.issue(user)
    except OAuthError as exc:
        return redirect(f"{settings.FRONTEND_URL}/login?error={exc.code}")

    return redirect(f"{settings.FRONTEND_URL}/auth/callback#token={raw_token}")
