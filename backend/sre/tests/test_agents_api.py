import pytest

from sre import temporal_client
from sre.models import (
    IncidentRun, LLMUsage, Organization, OrganizationMembership, OrgRole, Playbook, Project,
    RemediationAgent, ScanRepo, ScanRun,
)
from sre.orgs import personal_org

from .test_api import GITHUB_SECRET, post_github

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def agents_on(settings):
    settings.SRE_REMEDIATION_AGENTS_ENABLED = True


@pytest.fixture
def temporal(monkeypatch):
    calls = {"schedules": [], "deleted": [], "scans": []}
    monkeypatch.setattr(temporal_client, "sync_agent_schedule",
                        lambda agent: calls["schedules"].append((agent.id, temporal_client._agent_cron(agent))))
    monkeypatch.setattr(temporal_client, "delete_agent_schedule", lambda agent_id: calls["deleted"].append(agent_id))
    monkeypatch.setattr(temporal_client, "start_scan", lambda wid, scan_run_id: calls["scans"].append((wid, scan_run_id)))
    return calls


@pytest.fixture
def org(make_user):
    owner = make_user("owner@x.com")
    org = Organization.objects.create(name="Acme")
    OrganizationMembership.objects.create(organization=org, user=owner, role=OrgRole.OWNER)
    org.owner = owner
    return org


def _project(org, name="shop", branch="main"):
    return Project.objects.create(name=name, organization=org, github_installation_id="1",
                                  github_repo_owner="acme", github_repo_name=name,
                                  github_default_branch=branch)


def _create(api, org, **body):
    return api.post(f"/organizations/{org.id}/agents", {"name": "sweeper", **body})


# ---- CRUD -----------------------------------------------------------------------------

def test_create_defaults_and_schedule_sync(org, api_for, temporal):
    body = _create(api_for(org.owner), org).json()
    assert (body["kind"], body["trigger"], body["execution_mode"]) == ("playbook_sweep", "on_merge", "advisory_only")
    assert body["project_ids"] == [] and body["enabled"] is True and body["last_scan_run_id"] is None
    assert temporal["schedules"] == [(body["id"], "")]  # no schedule for on_merge

    body = _create(api_for(org.owner), org, name="nightly", kind="runbook_variant", trigger="schedule",
                   schedule_cron="0 3 * * *").json()
    assert body["execution_mode"] == "draft_only"
    assert temporal["schedules"][-1] == (body["id"], "0 3 * * *")


@pytest.mark.parametrize("body", [
    {"execution_mode": "autonomous"},
    {"trigger": "schedule"},
    {"trigger": "schedule", "schedule_cron": "every day"},
    {"trigger": "branch_watch"},
    {"max_findings_per_repo": 0},
    {"max_findings_per_repo": 11},
    {"monthly_token_budget": -1},
    {"name": "  "},
])
def test_create_rejects_bad_agents(org, api_for, temporal, body):
    assert _create(api_for(org.owner), org, **body).status_code == 400
    assert not RemediationAgent.objects.exists()


def test_projects_and_playbooks_must_belong_to_the_org(org, api_for, temporal, make_user):
    other = Organization.objects.create(name="Other")
    api = api_for(org.owner)
    assert _create(api, org, project_ids=[_project(other).id]).status_code == 400
    other_playbook = Playbook.objects.create(title="x", organization=other, is_generic=True)
    assert _create(api, org, playbook_ids=[other_playbook.id]).status_code == 400
    builtin = Playbook.objects.create(title="b", origin="builtin", is_generic=True)
    own = Playbook.objects.create(title="o", organization=org, is_generic=True)
    body = _create(api, org, project_ids=[_project(org).id], playbook_ids=[builtin.id, own.id]).json()
    assert sorted(body["playbook_ids"]) == sorted([builtin.id, own.id])


def test_names_are_unique_per_org(org, api_for, temporal):
    api = api_for(org.owner)
    first = _create(api, org).json()
    assert _create(api, org).status_code == 409
    second = _create(api, org, name="other").json()
    assert api.patch(f"/agents/{second['id']}", {"name": "sweeper"}).status_code == 409
    assert api.patch(f"/agents/{first['id']}", {"name": "sweeper"}).status_code == 200


def test_update_resyncs_the_schedule_and_can_disable(org, api_for, temporal):
    api = api_for(org.owner)
    agent = _create(api, org, trigger="schedule", schedule_cron="0 3 * * *").json()
    body = api.patch(f"/agents/{agent['id']}", {"enabled": False}).json()
    assert body["enabled"] is False
    assert temporal["schedules"][-1] == (agent["id"], "")  # disabled: schedule removed
    assert api.patch(f"/agents/{agent['id']}", {"execution_mode": "autonomous"}).status_code == 400


def test_a_temporal_failure_undoes_the_save(org, api_for, monkeypatch):
    def down(agent):
        raise RuntimeError("temporal down")
    monkeypatch.setattr(temporal_client, "sync_agent_schedule", down)
    assert _create(api_for(org.owner), org).status_code == 503
    assert not RemediationAgent.objects.exists()


def test_roles_and_visibility(org, api_for, temporal, make_user, settings):
    member = make_user("m@x.com")
    OrganizationMembership.objects.create(organization=org, user=member, role=OrgRole.MEMBER)
    outsider = make_user("o@x.com")
    agent = _create(api_for(org.owner), org).json()

    assert api_for(member).get(f"/agents/{agent['id']}").status_code == 200
    assert len(api_for(member).get(f"/organizations/{org.id}/agents").json()) == 1
    assert _create(api_for(member), org, name="x").status_code == 403
    assert api_for(member).post(f"/agents/{agent['id']}/run").status_code == 403
    assert api_for(member).delete(f"/agents/{agent['id']}").status_code == 403
    assert api_for(outsider).get(f"/agents/{agent['id']}").status_code == 404

    settings.SRE_REMEDIATION_AGENTS_ENABLED = False
    assert api_for(org.owner).get(f"/agents/{agent['id']}").status_code == 404
    assert api_for(org.owner).get(f"/organizations/{org.id}/agents").status_code == 404


def test_delete_removes_the_schedule(org, api_for, temporal):
    agent = _create(api_for(org.owner), org).json()
    assert api_for(org.owner).delete(f"/agents/{agent['id']}").status_code == 204
    assert temporal["deleted"] == [agent["id"]] and not RemediationAgent.objects.exists()


# ---- running -----------------------------------------------------------------------------

def test_run_scans_every_covered_project(org, api_for, temporal):
    a, b = _project(org, "a"), _project(org, "b", branch="develop")
    api = api_for(org.owner)
    agent = _create(api, org).json()
    resp = api.post(f"/agents/{agent['id']}/run")
    assert resp.status_code == 202
    body = resp.json()
    assert (body["trigger"], body["status"]) == ("manual", "running")
    assert sorted(r["project_id"] for r in body["repos"]) == [a.id, b.id]
    assert ScanRepo.objects.get(project=b).branch == "develop"
    [(workflow_id, scan_run_id)] = temporal["scans"]
    assert scan_run_id == body["id"] and ScanRun.objects.get(id=scan_run_id).temporal_workflow_id == workflow_id

    assert api.post(f"/agents/{agent['id']}/run").status_code == 409  # one still running
    assert api.get(f"/agents/{agent['id']}").json()["last_scan_run_id"] == body["id"]


def test_run_only_covers_chosen_projects_and_needs_the_agent_enabled(org, api_for, temporal):
    chosen = _project(org, "a")
    _project(org, "b")
    api = api_for(org.owner)
    agent = _create(api, org, project_ids=[chosen.id], enabled=False).json()
    assert api.post(f"/agents/{agent['id']}/run").status_code == 409
    api.patch(f"/agents/{agent['id']}", {"enabled": True})
    body = api.post(f"/agents/{agent['id']}/run").json()
    assert [r["project_id"] for r in body["repos"]] == [chosen.id]


def test_run_with_temporal_down_leaves_nothing_behind(org, api_for, temporal, monkeypatch):
    _project(org)
    agent = _create(api_for(org.owner), org).json()
    def down(*a):
        raise RuntimeError("temporal down")
    monkeypatch.setattr(temporal_client, "start_scan", down)
    assert api_for(org.owner).post(f"/agents/{agent['id']}/run").status_code == 503
    assert not ScanRun.objects.exists()


def test_scan_run_shows_findings_and_usage(org, api_for, temporal):
    project = _project(org)
    api = api_for(org.owner)
    agent = _create(api, org).json()
    scan_id = api.post(f"/agents/{agent['id']}/run").json()["id"]
    repo = ScanRepo.objects.get(scan_run_id=scan_id)
    ScanRepo.objects.filter(id=repo.id).update(status="succeeded", finding_count=1)
    incident = IncidentRun.objects.create(project=project, trace_id="s", temporal_workflow_id="s",
                                          source="scan", scan_run_id=scan_id, scan_kind="playbook_sweep")
    LLMUsage.objects.create(scan_repo=repo, step="repository_scan", model="m", input_tokens=10, output_tokens=2)

    body = api.get(f"/scan-runs/{scan_id}").json()
    assert body["repos"][0]["incident_run_ids"] == [incident.id]
    assert body["finding_count"] == 1 and body["usage"]["total_tokens"] == 12
    listing = api.get(f"/agents/{agent['id']}/scan-runs").json()
    assert listing["total"] == 1 and listing["scan_runs"][0]["id"] == scan_id
    assert api.get(f"/agents/{agent['id']}").json()["tokens_this_month"] == 12


# ---- GitHub triggers ---------------------------------------------------------------------

@pytest.fixture
def github_secret(settings):
    settings.GITHUB_APP_WEBHOOK_SECRET = GITHUB_SECRET


def _merged(repo="acme/shop", base="main", sha="a" * 40, merged=True):
    return {"action": "closed", "repository": {"full_name": repo},
            "pull_request": {"merged": merged, "merge_commit_sha": sha, "html_url": "u",
                             "base": {"ref": base}, "head": {"ref": "feature"}}}


def test_a_merge_into_the_default_branch_starts_on_merge_agents(client, org, api_for, temporal, github_secret):
    project = _project(org)
    api = api_for(org.owner)
    _create(api, org)  # on_merge, all projects
    _create(api, org, name="nightly", trigger="schedule", schedule_cron="0 3 * * *")

    resp = post_github(client, _merged())
    assert resp.status_code == 200 and "Started scan runs" in resp.json()["detail"]
    repo = ScanRepo.objects.get()
    assert (repo.project, repo.base_sha, repo.head_sha) == (project, "a" * 40 + "^1", "a" * 40)
    assert repo.scan_run.trigger == "on_merge" and len(temporal["scans"]) == 1

    # Redelivery: nothing new.
    assert post_github(client, _merged()).status_code == 202
    assert ScanRun.objects.count() == 1


@pytest.mark.parametrize("payload", [
    _merged(merged=False), _merged(base="feature-x"), _merged(repo="acme/other"),
])
def test_other_prs_start_nothing(client, org, api_for, temporal, github_secret, payload):
    _project(org)
    _create(api_for(org.owner), org)
    assert post_github(client, payload).status_code == 202
    assert not ScanRun.objects.exists()


def _push(ref="refs/heads/release/1.2", before="b" * 40, after="c" * 40, **extra):
    return {"ref": ref, "before": before, "after": after, "repository": {"full_name": "acme/shop"},
            "commits": [{"id": after}], **extra}


def test_a_push_to_a_watched_branch_starts_branch_watch_agents(client, org, api_for, temporal, github_secret):
    project = _project(org)
    _create(api_for(org.owner), org, trigger="branch_watch", branch_pattern="release/*")

    assert post_github(client, _push(), event="push").status_code == 200
    repo = ScanRepo.objects.get()
    assert (repo.branch, repo.base_sha, repo.head_sha) == ("release/1.2", "b" * 40, "c" * 40)

    # A new branch is compared with the default branch; unwatched branches and deletions are ignored.
    post_github(client, _push(ref="refs/heads/release/2", before="0" * 40, after="d" * 40, created=True),
                event="push")
    assert ScanRepo.objects.get(head_sha="d" * 40).base_sha == "main"
    assert post_github(client, _push(ref="refs/heads/main", after="e" * 40), event="push").status_code == 202
    assert post_github(client, _push(after="0" * 40, deleted=True), event="push").status_code == 202
    assert ScanRun.objects.count() == 2


def test_triggers_are_off_with_the_flag_off(client, org, api_for, temporal, github_secret, settings):
    _project(org)
    _create(api_for(org.owner), org)
    settings.SRE_REMEDIATION_AGENTS_ENABLED = False
    assert post_github(client, _merged()).status_code == 202
    assert not ScanRun.objects.exists()


def test_a_trigger_with_temporal_down_asks_for_a_redelivery(client, org, api_for, temporal, github_secret,
                                                            monkeypatch):
    _project(org)
    _create(api_for(org.owner), org)
    def down(*a):
        raise RuntimeError("temporal down")
    monkeypatch.setattr(temporal_client, "start_scan", down)
    assert post_github(client, _merged()).status_code == 503
    assert not ScanRun.objects.exists()
