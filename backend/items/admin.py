from django.contrib import admin

from .models import Item


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "owner", "created_at", "updated_at")
    search_fields = ("title", "owner__email")
