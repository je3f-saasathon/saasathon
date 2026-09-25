from ninja.security import HttpBearer

from .models import AuthToken


class BearerAuth(HttpBearer):
    """Validates opaque bearer tokens: checks the hash exists, isn't
    revoked, and isn't expired. Sets request.auth to the User."""

    def authenticate(self, request, token: str):
        auth_token = AuthToken.get_valid(token)
        if auth_token is None:
            return None
        auth_token.touch()
        request.auth_token = auth_token
        return auth_token.user


bearer_auth = BearerAuth()
