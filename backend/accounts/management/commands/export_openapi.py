import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from config.api import api


class Command(BaseCommand):
    help = "Writes the current django-ninja OpenAPI schema to backend/openapi.json"

    def handle(self, *args, **options):
        schema = api.get_openapi_schema()
        out_path = Path(settings.BASE_DIR) / "openapi.json"
        out_path.write_text(json.dumps(schema, indent=2, default=str) + "\n")
        self.stdout.write(self.style.SUCCESS(f"Wrote OpenAPI schema to {out_path}"))
