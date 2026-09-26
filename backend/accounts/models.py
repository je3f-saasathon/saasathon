import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.utils import timezone


class CaseInsensitiveEmailField(models.EmailField):
    pass


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", "admin")
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """Custom user model. Email is the login identifier (case-insensitive,
    enforced via a functional unique index in the initial migration)."""

    email = CaseInsensitiveEmailField(unique=True)
    name = models.CharField(max_length=150, blank=True)
    avatar_url = models.URLField(blank=True, default="")
    role = models.CharField(max_length=32, default="user")

    github_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    google_sub = models.CharField(max_length=128, unique=True, null=True, blank=True)

    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "accounts_user"

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        # Emails are case-insensitive: normalize to lowercase so the
        # unique constraint on the field enforces case-insensitive
        # uniqueness on every backend (SQLite and Postgres alike).
        if self.email:
            self.email = self.email.lower()
        super().save(*args, **kwargs)

    def get_full_name(self):
        return self.name or self.email

    def get_short_name(self):
        return self.name or self.email


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _default_expiry():
    ttl_days = getattr(settings, "TOKEN_TTL_DAYS", 30)
    return timezone.now() + timedelta(days=ttl_days)


class AuthToken(models.Model):
    """Opaque bearer token. Only the SHA-256 hash of the raw token is
    stored; the raw value is returned exactly once, at issuance."""

    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="auth_tokens"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=_default_expiry)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounts_auth_token"

    def __str__(self):
        return f"AuthToken(user={self.user_id})"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @classmethod
    def issue(cls, user) -> tuple["AuthToken", str]:
        """Create a new token for user. Returns (token_obj, raw_token)."""
        raw_token = secrets.token_urlsafe(32)
        token = cls.objects.create(user=user, key_hash=_hash_token(raw_token))
        return token, raw_token

    @classmethod
    def get_valid(cls, raw_token: str) -> "AuthToken | None":
        key_hash = _hash_token(raw_token)
        try:
            token = cls.objects.select_related("user").get(key_hash=key_hash)
        except cls.DoesNotExist:
            return None
        if token.is_revoked or token.is_expired:
            return None
        return token

    def touch(self):
        self.last_used_at = timezone.now()
        self.save(update_fields=["last_used_at"])

    def revoke(self):
        self.revoked_at = timezone.now()
        self.save(update_fields=["revoked_at"])


CLI_LOGIN_TTL = timedelta(minutes=10)
# No 0/O/1/I: the user reads this code off a terminal and may type it.
USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _new_user_code() -> str:
    code = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
    return f"{code[:4]}-{code[4:]}"


def _cli_login_expiry():
    return timezone.now() + CLI_LOGIN_TTL


class CliLogin(models.Model):
    """A device-code login for the `buggly` CLI. The CLI holds the secret device code and
    polls; a signed-in user approves the short user code in the browser; the next poll
    gets a token and the row is deleted. Stored in the database, not the cache, because
    the cache is per gunicorn worker."""

    device_code_hash = models.CharField(max_length=64, unique=True)
    user_code = models.CharField(max_length=9, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
        related_name="cli_logins",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(default=_cli_login_expiry)

    class Meta:
        db_table = "accounts_cli_login"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @classmethod
    def start(cls) -> tuple["CliLogin", str]:
        """A new login. Returns (login, raw_device_code)."""
        cls.objects.filter(expires_at__lte=timezone.now()).delete()
        raw = secrets.token_urlsafe(32)
        for _ in range(5):
            code = _new_user_code()
            if not cls.objects.filter(user_code=code).exists():
                break
        return cls.objects.create(device_code_hash=_hash_token(raw), user_code=code), raw

    @classmethod
    def by_device_code(cls, raw: str) -> "CliLogin | None":
        return cls.objects.select_related("user").filter(device_code_hash=_hash_token(raw)).first()
