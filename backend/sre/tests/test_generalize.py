from io import StringIO

import pytest
from django.core.management import call_command

from sre.models import IncidentRun, Playbook, PlaybookRun, Runbook
from sre.tests.test_activities import FakeLLM, incident, project  # shared fixtures

pytestmark = pytest.mark.django_db

GENERIC_REPLY = {"title": "Connection pool exhaustion", "description": "generic", "symptoms": "s",
                 "keywords": ["TimeoutError"],
                 "steps": [{"type": "investigate", "instructions": "find the holder"},
                           {"type": "change", "instructions": "release connections"},
                           {"type": "verify", "instructions": "load test"}]}
LEGACY_STEPS = [{"type": "edit_file", "path": "db.py", "instructions": "POOL = 20"},
                {"type": "run_command", "command": "pytest tests/test_db.py"}]


@pytest.fixture
def legacy(project, incident):
    incident.classification = {"category": "timeout"}
    incident.telemetry = {"service_name": "shop-api"}
    incident.save()
    playbook = Playbook.objects.create(
        project=project, organization=project.organization, source_incident_run=incident,
        title="Raise pool size in db.py", description="specific", keywords=["pool"],
        steps=LEGACY_STEPS, status="confirmed",
    )
    incident.matched_playbook = playbook
    incident.save()
    PlaybookRun.objects.create(incident_run=incident, playbook=playbook, execution_mode="draft_only")
    return playbook


def _run(*args):
    out = StringIO()
    call_command("generalize_playbooks", *args, stdout=out, stderr=out)
    return out.getvalue()


def _state(playbook):
    playbook.refresh_from_db()
    return {f: getattr(playbook, f) for f in ("title", "description", "keywords", "steps", "status",
                                              "is_generic", "category", "legacy_snapshot")}


def test_dry_run_changes_nothing(legacy, project):
    human = Playbook.objects.create(project=project, organization=project.organization, title="human")
    before = _state(legacy)
    out = _run()
    assert "would  " in out and "skip" in out and "written by a person" in out
    assert _state(legacy) == before and not Runbook.objects.exists()
    assert _state(human)["is_generic"] is False


def test_apply_splits_into_a_generic_playbook_and_a_runbook(monkeypatch, legacy, incident):
    FakeLLM(monkeypatch, GENERIC_REPLY)
    out = _run("--apply")
    assert "1 generalized" in out
    playbook = Playbook.objects.get(id=legacy.id)
    assert playbook.is_generic and playbook.status == "unconfirmed"  # new text, not yet trusted
    assert playbook.title == "Connection pool exhaustion" and playbook.category == "timeout"
    assert [s["type"] for s in playbook.steps] == ["investigate", "change", "verify"]
    runbook = Runbook.objects.get(playbook=playbook)
    assert runbook.origin == "migrated" and runbook.status == "confirmed"  # keeps the old trust
    assert runbook.steps == LEGACY_STEPS and runbook.service_name == "shop-api"
    assert PlaybookRun.objects.get(incident_run=incident).runbook == runbook
    assert IncidentRun.objects.get(id=incident.id).matched_runbook == runbook


def test_revert_puts_everything_back(monkeypatch, legacy, incident):
    before = _state(legacy)
    FakeLLM(monkeypatch, GENERIC_REPLY)
    _run("--apply")
    assert "reverted 1" in _run("--revert")
    assert _state(legacy) == before
    assert not Runbook.objects.exists()
    assert PlaybookRun.objects.get(incident_run=incident).runbook is None
    assert IncidentRun.objects.get(id=incident.id).matched_runbook is None


def test_a_bad_rewrite_leaves_the_playbook_alone(monkeypatch, legacy):
    before = _state(legacy)
    FakeLLM(monkeypatch, {"title": "no steps", "steps": [{"type": "edit_file", "path": "x"}]})
    out = _run("--apply")
    assert "failed" in out and "1 failed" in out
    assert _state(legacy) == before and not Runbook.objects.exists()


def test_merge_folds_a_duplicate_into_a_builtin_and_reverts(monkeypatch, legacy):
    builtin = Playbook.objects.get(slug="timeout")
    builtin_before = _state(builtin)
    FakeLLM(monkeypatch, GENERIC_REPLY,
            {"duplicate_of": builtin.id, "confidence": 0.9, "reasoning": "same class"})
    assert "merged playbook" in _run("--apply", "--merge")
    runbook = Runbook.objects.get()
    assert runbook.playbook_id == builtin.id
    assert Playbook.objects.get(id=legacy.id).status == "failing"  # archived, not deleted
    assert _state(builtin) == builtin_before  # the shared built-in row isn't written to

    _run("--revert")
    assert not Runbook.objects.exists()
    assert Playbook.objects.get(id=legacy.id).status == "confirmed"
    assert _state(builtin) == builtin_before
