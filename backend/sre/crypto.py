from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _fernet() -> Fernet:
    if not settings.SRE_FIELD_ENCRYPTION_KEY:
        raise ImproperlyConfigured("SRE_FIELD_ENCRYPTION_KEY is not set")
    return Fernet(settings.SRE_FIELD_ENCRYPTION_KEY.encode())


def encrypt(value: str) -> bytes:
    return _fernet().encrypt(value.encode())


def decrypt(token: bytes) -> str:
    return _fernet().decrypt(bytes(token)).decode()
