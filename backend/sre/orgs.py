from django.db import transaction

from .models import Organization, OrganizationMembership, OrgRole


def create_personal_org(user) -> Organization:
    with transaction.atomic():
        org = Organization.objects.create(name=f"{user.email}'s workspace", is_personal=True)
        OrganizationMembership.objects.create(organization=org, user=user, role=OrgRole.OWNER)
    return org


def personal_org(user) -> Organization:
    """The user's personal org. Users from before orgs existed were backfilled, but one
    created some other way (e.g. a raw fixture) gets one on first use."""
    org = Organization.objects.filter(is_personal=True, memberships__user=user).first()
    return org or create_personal_org(user)
