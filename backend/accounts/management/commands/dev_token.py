from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.management.commands.seed_demo_user import DEMO_EMAIL
from accounts.models import AuthToken, User


class Command(BaseCommand):
    help = "Mints a bearer token for the demo user and prints it (DEBUG only)."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("dev_token can only run when DEBUG=true")

        try:
            user = User.objects.get(email=DEMO_EMAIL)
        except User.DoesNotExist:
            raise CommandError(
                f"demo user {DEMO_EMAIL} does not exist yet; run "
                "`uv run python manage.py seed_demo_user` first"
            )

        _token, raw_token = AuthToken.issue(user)
        self.stdout.write(raw_token)
