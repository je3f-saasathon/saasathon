import pytest


@pytest.fixture
def make_user(db):
    from accounts.models import User

    def _make(email="user@example.com", password="correct-horse-battery", **kwargs):
        return User.objects.create_user(email=email, password=password, **kwargs)

    return _make
