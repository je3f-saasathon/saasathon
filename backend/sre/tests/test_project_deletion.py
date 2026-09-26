import pytest

from sre import temporal_client
from sre.models import (
    IncidentRun, Playbook, PlaybookRun, Project, ProjectRole, RemediationAgent, Runbook, ScanRepo,
    ScanRun,
)
from sre.orgs import personal_org

pytestmark = pytest.mark.django_db


@pytest.fixture
def stopped(monkeypatch):
    calls = []
    monkeypatch.setattr(temporal_client, "stop_project_work",
                        lambda wids, agent_ids, reason: calls.append((sorted(wids), sorted(agent_ids))))
    return calls


def _incident(project, status, n=[0]):
    n[0] += 1
    return IncidentRun.objects.create(project=project, trace_id=f"t{n[0]}", status=status,
                                      temporal_workflow_id=f"sre-incident-{project.id}-t{n[0]}")


def _scan_run(agent, *projects, wid="scan-1"):
    run = ScanRun.objects.create(agent=agent, trigger="manual", temporal_workflow_id=wid)
    for p in projects:
        ScanRepo.objects.create(scan_run=run, project=p, branch="main")
    return run


def test_owner_delete_cancels_unfinished_incidents_then_deletes(api_for, make_user, make_project, stopped):
    owner = make_user()
    project = make_project(owner)
    waiting = [_incident(project, s) for s in ("running", "awaiting_approval", "rejected")]
    done = _incident(project, "succeeded")
    playbook = Playbook.objects.create(project=project, organization=project.organization,
                                       title="t", keywords=["x"])
    PlaybookRun.objects.create(incident_run=waiting[1], playbook=playbook, execution_mode="draft_only",
                               status="pending_approval", pr_url="https://github.com/acme/shop/pull/1")

    assert api_for(owner).delete(f"/projects/{project.id}").status_code == 204
    assert stopped == [(sorted(r.temporal_workflow_id for r in waiting), [])]
    assert done.temporal_workflow_id not in stopped[0][0]
    assert not Project.objects.filter(id=project.id).exists()
    assert not IncidentRun.objects.exists() and not PlaybookRun.objects.exists()


def test_generic_playbooks_stay_legacy_ones_go(api_for, make_user, make_project, stopped):
    owner = make_user()
    project = make_project(owner)
    generic = Playbook.objects.create(project=project, organization=project.organization,
                                      is_generic=True, title="generic", keywords=["x"])
    legacy = Playbook.objects.create(project=project, organization=project.organization,
                                     title="legacy", keywords=["x"])
    Runbook.objects.create(project=project, playbook=generic, title="rb")

    assert api_for(owner).delete(f"/projects/{project.id}").status_code == 204
    generic.refresh_from_db()
    assert generic.project_id is None
    assert not Playbook.objects.filter(id=legacy.id).exists()
    assert not Runbook.objects.exists()
    assert stopped == []  # nothing was running: Temporal isn't asked


def test_temporal_down_deletes_nothing(api_for, make_user, make_project, monkeypatch):
    def down(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(temporal_client, "stop_project_work", down)
    owner = make_user()
    project = make_project(owner)
    _incident(project, "awaiting_approval")
    resp = api_for(owner).delete(f"/projects/{project.id}")
    assert resp.status_code == 503
    assert "nothing was deleted" in resp.json()["detail"]
    assert Project.objects.filter(id=project.id).exists() and IncidentRun.objects.count() == 1


def test_temporal_down_with_nothing_running_still_deletes(api_for, make_user, make_project, monkeypatch):
    def down(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(temporal_client, "stop_project_work", down)
    owner = make_user()
    project = make_project(owner)
    _incident(project, "failed")
    assert api_for(owner).delete(f"/projects/{project.id}").status_code == 204


def test_only_the_owner_can_delete(api_for, make_user, make_project, add_member, stopped):
    owner, admin = make_user(), make_user("admin@x.com")
    project = make_project(owner)
    add_member(project, admin, ProjectRole.ADMIN)
    assert api_for(admin).delete(f"/projects/{project.id}").status_code == 403
    assert Project.objects.filter(id=project.id).exists()


def test_scan_runs_single_project_run_cancelled_shared_run_continues(api_for, make_user, make_project, stopped):
    owner = make_user()
    project, other = make_project(owner), make_project(owner, name="other")
    agent = RemediationAgent.objects.create(organization=personal_org(owner), name="sweeper")
    alone = _scan_run(agent, project, wid="scan-alone")
    shared = _scan_run(agent, project, other, wid="scan-shared")
    finished = _scan_run(agent, project, wid="scan-done")
    ScanRun.objects.filter(id=finished.id).update(status="succeeded")

    assert api_for(owner).delete(f"/projects/{project.id}").status_code == 204
    assert stopped == [(["scan-alone"], [])]
    alone.refresh_from_db()
    assert alone.status == "failed" and "project was deleted" in alone.error_message
    assert alone.finished_at is not None
    shared.refresh_from_db()
    assert shared.status == "running"
    assert list(shared.repos.values_list("project_id", flat=True)) == [other.id]


def test_agents_lose_the_project_and_one_left_with_none_is_disabled(api_for, make_user, make_project, stopped):
    owner = make_user()
    project, other = make_project(owner), make_project(owner, name="other")
    org = personal_org(owner)
    only = RemediationAgent.objects.create(organization=org, name="only", trigger="schedule",
                                           schedule_cron="0 3 * * *")
    only.projects.set([project])
    both = RemediationAgent.objects.create(organization=org, name="both")
    both.projects.set([project, other])
    everything = RemediationAgent.objects.create(organization=org, name="all")

    assert api_for(owner).delete(f"/projects/{project.id}").status_code == 204
    assert stopped == [([], [only.id])]  # its schedule is removed
    only.refresh_from_db(); both.refresh_from_db(); everything.refresh_from_db()
    assert only.enabled is False and list(only.projects.all()) == []
    assert both.enabled is True and list(both.projects.values_list("id", flat=True)) == [other.id]
    assert everything.enabled is True


def test_linked_child_in_another_project_keeps_running(api_for, make_user, make_project, stopped):
    owner = make_user()
    project, other = make_project(owner), make_project(owner, name="other")
    parent = _incident(project, "delegated")
    child = _incident(other, "running")
    IncidentRun.objects.filter(id=child.id).update(parent_incident_run=parent, source="linked")

    assert api_for(owner).delete(f"/projects/{project.id}").status_code == 204
    assert stopped == []
    child.refresh_from_db()
    assert child.status == "running" and child.parent_incident_run_id is None
