import os
import warnings

from .base import *  # noqa: F401,F403

DEBUG = False

# In production ALLOWED_HOSTS should be set explicitly via env. We warn
# rather than hard-fail so image builds (e.g. `collectstatic` at build
# time, before runtime env vars exist) don't break.
if not os.environ.get("ALLOWED_HOSTS"):
    warnings.warn(
        "ALLOWED_HOSTS is not set; falling back to base.py's default. "
        "Set it explicitly for real production traffic.",
        stacklevel=1,
    )

SECURE_SSL_REDIRECT = False  # handled by the tunnel/reverse proxy
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
