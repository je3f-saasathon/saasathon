from datetime import datetime

from ninja import Schema


class ItemOut(Schema):
    id: int
    owner_id: int
    title: str
    description: str
    created_at: datetime
    updated_at: datetime


class ItemListOut(Schema):
    items: list[ItemOut]
    total: int


class ItemCreateIn(Schema):
    title: str
    description: str | None = ""


class ItemUpdateIn(Schema):
    title: str | None = None
    description: str | None = None
