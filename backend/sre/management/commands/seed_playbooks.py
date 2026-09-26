from django.core.management.base import BaseCommand

from sre.models import Playbook
from sre.playbook_library import BUILTIN_PLAYBOOKS


class Command(BaseCommand):
    help = "Creates or updates the built-in generic playbooks (sre/playbook_library.py), by slug."

    def handle(self, *args, **options):
        for data in BUILTIN_PLAYBOOKS:
            _, created = Playbook.objects.update_or_create(
                slug=data["slug"],
                defaults={**{k: v for k, v in data.items() if k != "slug"},
                          "organization": None, "project": None,
                          "origin": Playbook.Origin.BUILTIN, "is_generic": True,
                          "status": Playbook.Status.CONFIRMED},
            )
            self.stdout.write(f"{'created' if created else 'updated'} {data['slug']}")
