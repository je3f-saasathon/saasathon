from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.models import User

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo-password-123"


class Command(BaseCommand):
    help = "Creates the demo@example.com user (DEBUG only). Idempotent."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo_user can only run when DEBUG=true")

        user, created = User.objects.get_or_create(
            email=DEMO_EMAIL, defaults={"name": "Demo User"}
        )
        if created:
            user.set_password(DEMO_PASSWORD)
            user.save()
            self.stdout.write(
                self.style.SUCCESS(f"Created demo user {DEMO_EMAIL} / {DEMO_PASSWORD}")
            )
        else:
            self.stdout.write(f"Demo user {DEMO_EMAIL} already exists, nothing to do.")
