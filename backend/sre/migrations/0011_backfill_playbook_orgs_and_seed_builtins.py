from django.db import migrations


def seed_builtins(Playbook):
    from sre.playbook_library import BUILTIN_PLAYBOOKS

    for data in BUILTIN_PLAYBOOKS:
        Playbook.objects.update_or_create(
            slug=data["slug"],
            defaults={**{k: v for k, v in data.items() if k != "slug"},
                      "organization": None, "project": None, "origin": "builtin",
                      "is_generic": True, "status": "confirmed"},
        )


def forwards(apps, schema_editor):
    """Existing playbooks join their project's org as legacy (still project-only)
    playbooks; generalize_playbooks turns them generic later. Then the built-ins."""
    Playbook = apps.get_model("sre", "Playbook")
    for playbook in Playbook.objects.filter(organization__isnull=True, slug__isnull=True):
        project = playbook.project
        playbook.organization_id = project.organization_id if project else None
        playbook.origin = "agent" if playbook.source_incident_run_id else "human"
        playbook.save(update_fields=["organization", "origin"])
    seed_builtins(Playbook)


def backwards(apps, schema_editor):
    """Undoing 0009 makes project required again, so playbooks without a project go:
    the built-ins, and generic org playbooks whose project was deleted."""
    Playbook = apps.get_model("sre", "Playbook")
    Playbook.objects.filter(project__isnull=True).delete()
    Playbook.objects.update(organization=None)


class Migration(migrations.Migration):
    dependencies = [("sre", "0010_generic_playbooks")]

    operations = [migrations.RunPython(forwards, backwards)]
