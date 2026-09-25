from django.conf import settings
from ninja import NinjaAPI

from accounts.api import router as accounts_router
from accounts.auth import bearer_auth
from items.api import router as items_router
from jev.api import router as jev_router

api = NinjaAPI(
    title="Backend API",
    version="1.0.0",
    auth=bearer_auth,
)


@api.get("/health", auth=None)
def health(request):
    return {"status": "ok", "version": settings.APP_VERSION}


api.add_router("/auth", accounts_router)
api.add_router("/items", items_router)
api.add_router("/jev", jev_router)
