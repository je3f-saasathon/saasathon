from datetime import datetime

from ninja import Schema


class UserOut(Schema):
    id: int
    email: str
    name: str
    avatar_url: str
    role: str
    created_at: datetime


class RegisterIn(Schema):
    email: str
    password: str
    name: str | None = None


class LoginIn(Schema):
    email: str
    password: str


class TokenOut(Schema):
    token: str
    user: UserOut


class OkOut(Schema):
    ok: bool = True


class ProvidersOut(Schema):
    password: bool = True
    github: bool
    google: bool


class ErrorOut(Schema):
    detail: str


def user_to_out(user) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "role": user.role,
        "created_at": user.date_joined,
    }
