from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from .orgs import create_personal_org


# Every way an account is made (email sign-up, GitHub/Google OAuth, createsuperuser) goes
# through User.save, so a receiver here covers them all without touching accounts/.
@receiver(post_save, sender=settings.AUTH_USER_MODEL, dispatch_uid="sre_personal_org")
def give_new_user_a_personal_org(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        create_personal_org(instance)
