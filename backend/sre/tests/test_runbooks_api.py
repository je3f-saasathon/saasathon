import pytest

from sre.models import IncidentRun, Playbook, PlaybookRun, Runbook

pytestmark = pytest.mark.django_db

STEPS = [{"type": "edit_file", "path": "catalog/views.py", "instructions": "guard customer"},
         {"type": "run_command", "command": "uv run pytest tests/test_orders.py"},
         {"type": "investigate", "instructions": "generic steps don't belong in a runbook"},
         {"type": "create_pr"}]


@pytest.fixture
def runbooks_on(settings):
    settings.SRE_RUNBOOKS_ENABLED = True


@pytest.fixture
def setup(make_user, make_project, add_member):
    owner, viewer, outsider = make_user("o@x.com"), make_user("v@x.com"), make_user("x@x.com")
    project = make_project(owner)
    add_member(project, viewer, "viewer")
    return {"owner": owner, "viewer": viewer, "outsider": outsider, "project": project,
            "builtin": Playbook.objects.get(slug="null-reference")}


def _create(api, setup, **extra):
    body = {"playbook_id": setup["builtin"].id, "title": "Guard Order.customer",
            "area": "orders API", "keywords": ["Loyalty_Tier"], "steps": STEPS, **extra}
    return api.post(f"/projects/{setup['project'].id}/runbooks", body)


def test_runbooks_are_off_without_the_flag(setup, api_for):
    assert _create(api_for(setup["owner"]), setup).status_code == 404


def test_create_and_read_a_runbook(setup, api_for, runbooks_on):
    resp = _create(api_for(setup["owner"]), setup)
    assert resp.status_code == 201
    runbook = resp.json()
    assert runbook["status"] == "confirmed" and runbook["origin"] == "human"
    assert (runbook["repo_owner"], runbook["repo_name"]) == ("acme", "shop")
    assert runbook["keywords"] == ["loyalty_tier"]
    # Only the specific step types survive: no generic steps, no PR step.
    assert [s["type"] for s in runbook["steps"]] == ["edit_file", "run_command"]

    viewer = api_for(setup["viewer"])
    assert viewer.get(f"/runbooks/{runbook['id']}").status_code == 200
    listed = viewer.get(f"/projects/{setup['project'].id}/runbooks?playbook_id={setup['builtin'].id}")
    assert listed.json()["total"] == 1
    assert api_for(setup["outsider"]).get(f"/runbooks/{runbook['id']}").status_code == 404


def test_writes_need_project_admin(setup, api_for, runbooks_on):
    runbook_id = _create(api_for(setup["owner"]), setup).json()["id"]
    viewer = api_for(setup["viewer"])
    assert _create(viewer, setup).status_code == 403
    assert viewer.patch(f"/runbooks/{runbook_id}", {"title": "x"}).status_code == 403
    assert viewer.delete(f"/runbooks/{runbook_id}").status_code == 403


def test_runbook_must_use_a_playbook_the_project_can_see(setup, api_for, runbooks_on, make_user,
                                                         make_project):
    other = make_project(make_user("z@x.com"), name="other")
    hidden = Playbook.objects.create(organization=other.organization, is_generic=True, title="h")
    assert _create(api_for(setup["owner"]), setup, playbook_id=hidden.id).status_code == 400


def test_patch_status_resets_the_failure_count(setup, api_for, runbooks_on):
    runbook_id = _create(api_for(setup["owner"]), setup).json()["id"]
    Runbook.objects.filter(id=runbook_id).update(status="failing", consecutive_failure_count=3)
    resp = api_for(setup["owner"]).patch(f"/runbooks/{runbook_id}", {"status": "confirmed"})
    assert resp.json()["consecutive_failure_count"] == 0


def test_a_playbook_used_by_runbooks_cant_be_deleted(setup, api_for, runbooks_on):
    api = api_for(setup["owner"])
    playbook = api.post(f"/projects/{setup['project'].id}/playbooks", {"title": "org pb"}).json()
    runbook_id = _create(api, setup, playbook_id=playbook["id"]).json()["id"]
    assert api.delete(f"/playbooks/{playbook['id']}").status_code == 409
    assert api.delete(f"/runbooks/{runbook_id}").status_code == 204
    assert api.delete(f"/playbooks/{playbook['id']}").status_code == 204


def test_incident_run_shows_its_runbook_and_telemetry(setup, api_for, runbooks_on):
    project = setup["project"]
    runbook = Runbook.objects.create(project=project, playbook=setup["builtin"], title="rb")
    run = IncidentRun.objects.create(project=project, trace_id="t", temporal_workflow_id="w",
                                     matched_playbook=setup["builtin"], matched_runbook=runbook,
                                     telemetry={"exception_type": "AttributeError"})
    playbook_run = PlaybookRun.objects.create(incident_run=run, playbook=setup["builtin"],
                                              runbook=runbook, execution_mode="draft_only")
    api = api_for(setup["owner"])
    body = api.get(f"/incident-runs/{run.id}").json()
    assert body["runbook"] == {"id": runbook.id, "title": "rb", "status": "unconfirmed",
                               "source": "matched"}
    assert body["matched_runbook_id"] == runbook.id
    assert body["telemetry"] == {"exception_type": "AttributeError"}
    assert api.get(f"/playbook-runs/{playbook_run.id}").json()["runbook_id"] == runbook.id
