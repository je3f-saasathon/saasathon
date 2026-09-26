from django.apps import AppConfig


class SreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sre"

    def ready(self):
        from . import signals  # connects the receivers
