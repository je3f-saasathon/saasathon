from .base import *  # noqa: F401,F403

DEBUG = True
SECRET_KEY = "test-secret-key-that-is-long-enough-for-hmac-sha256"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

GITHUB_CLIENT_ID = "test-github-client-id"
GITHUB_CLIENT_SECRET = "test-github-client-secret"
GOOGLE_CLIENT_ID = "test-google-client-id"
GOOGLE_CLIENT_SECRET = "test-google-client-secret"
