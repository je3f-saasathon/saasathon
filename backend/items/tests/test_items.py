import json

import pytest

from accounts.models import AuthToken
from items.models import Item

pytestmark = pytest.mark.django_db


def auth_header(user):
    _, raw = AuthToken.issue(user)
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def test_items_crud_is_owner_scoped(client, make_user):
    owner = make_user(email="owner@example.com")
    other = make_user(email="other@example.com")

    headers = auth_header(owner)

    resp = client.post(
        "/api/items",
        data=json.dumps({"title": "First item", "description": "desc"}),
        content_type="application/json",
        **headers,
    )
    assert resp.status_code == 201
    item_id = resp.json()["id"]
    assert resp.json()["owner_id"] == owner.id

    resp = client.get("/api/items", **headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "First item"

    resp = client.get(f"/api/items/{item_id}", **headers)
    assert resp.status_code == 200

    other_headers = auth_header(other)
    resp = client.get(f"/api/items/{item_id}", **other_headers)
    assert resp.status_code == 404

    resp = client.patch(
        f"/api/items/{item_id}",
        data=json.dumps({"title": "Updated title"}),
        content_type="application/json",
        **headers,
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Updated title"

    resp = client.delete(f"/api/items/{item_id}", **headers)
    assert resp.status_code == 204
    assert not Item.objects.filter(id=item_id).exists()


def test_items_require_auth(client):
    resp = client.get("/api/items")
    assert resp.status_code == 401
