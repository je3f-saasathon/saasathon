from django.conf import settings
from django.db import migrations


def forwards(apps, schema_editor):
    """Every existing user gets a personal org (new ones get it from sre/signals.py), and
    each project joins the personal org of its earliest owner. Project collaborators are
    not added to that org: project access still comes from project roles."""
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    Organization = apps.get_model("sre", "Organization")
    OrganizationMembership = apps.get_model("sre", "OrganizationMembership")
    Project = apps.get_model("sre", "Project")
    ProjectMembership = apps.get_model("sre", "ProjectMembership")

    org_by_user = {}
    for user in User.objects.order_by("id"):
        existing = OrganizationMembership.objects.filter(
            user=user, organization__is_personal=True
        ).first()
        if existing:
            org_by_user[user.id] = existing.organization_id
            continue
        org = Organization.objects.create(name=f"{user.email}'s workspace", is_personal=True)
        OrganizationMembership.objects.create(organization=org, user=user, role="owner")
        org_by_user[user.id] = org.id

    for project in Project.objects.filter(organization__isnull=True):
        owner = (ProjectMembership.objects.filter(project=project, role="owner")
                 .order_by("created_at", "id").first())
        if owner is not None:
            project.organization_id = org_by_user[owner.user_id]
            project.save(update_fields=["organization"])


def backwards(apps, schema_editor):
    Organization = apps.get_model("sre", "Organization")
    Project = apps.get_model("sre", "Project")
    Project.objects.update(organization=None)
    Organization.objects.filter(is_personal=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("sre", "0006_organizations"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
