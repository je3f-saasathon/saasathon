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

SRE_FIELD_ENCRYPTION_KEY = "q0pS4fWn2d0z7lqK8o4rYk3VtX1bLJ2mN9cEo5hA6uI="
SRE_ALLOW_PRIVATE_LLM_URLS = False
LANGFUSE_PUBLIC_KEY = ""
LANGFUSE_SECRET_KEY = ""
