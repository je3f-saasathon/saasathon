from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router

from .models import Item
from .schemas import ItemCreateIn, ItemListOut, ItemOut, ItemUpdateIn

router = Router(tags=["items"])


@router.get("", response=ItemListOut)
def list_items(request: HttpRequest, page: int = 1, page_size: int = 20):
    page = max(page, 1)
    page_size = max(1, min(page_size, 100))

    qs = Item.objects.filter(owner=request.auth)
    total = qs.count()
    start = (page - 1) * page_size
    items = list(qs[start : start + page_size])
    return {"items": items, "total": total}


@router.post("", response={201: ItemOut})
def create_item(request: HttpRequest, payload: ItemCreateIn):
    item = Item.objects.create(
        owner=request.auth,
        title=payload.title,
        description=payload.description or "",
    )
    return 201, item


@router.get("/{item_id}", response=ItemOut)
def get_item(request: HttpRequest, item_id: int):
    return get_object_or_404(Item, id=item_id, owner=request.auth)


@router.patch("/{item_id}", response=ItemOut)
def update_item(request: HttpRequest, item_id: int, payload: ItemUpdateIn):
    item = get_object_or_404(Item, id=item_id, owner=request.auth)
    if payload.title is not None:
        item.title = payload.title
    if payload.description is not None:
        item.description = payload.description
    item.save()
    return item


@router.delete("/{item_id}", response={204: None})
def delete_item(request: HttpRequest, item_id: int):
    item = get_object_or_404(Item, id=item_id, owner=request.auth)
    item.delete()
    return 204, None
