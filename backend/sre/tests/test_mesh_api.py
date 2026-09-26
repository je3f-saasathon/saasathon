import pytest
from django.utils import timezone

from sre import temporal_client
from sre.models import IncidentRun, Organization, OrganizationMembership, OrgRole, ServiceEdge, ServiceNode
from sre.orgs import personal_org
from sre.services import mesh

from .test_mesh import SOURCE, _project

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def mesh_on(settings):
    settings.SRE_SERVICE_MESH_ENABLED = True


@pytest.fixture
def github_ok(monkeypatch):
    from sre import api as sre_api
    monkeypatch.setattr(sre_api, "_check_github_repo", lambda *a: None)


@pytest.fixture
def owner(make_user):
    return make_user()


# ---- service_names on projects ----------------------------------------------------------

def _create(api, **extra):
    return api.post("/projects", {"name": "p", "github_installation_id": "1", "github_repo_owner": "acme",
                                  "github_repo_name": "shop", **extra})


def test_service_names_are_cleaned_on_create_and_update(owner, api_for, github_ok):
    api = api_for(owner)
    body = _create(api, service_names=[" api ", "api", "api-cron"]).json()
    assert body["service_names"] == ["api", "api-cron"]
    resp = api.patch(f"/projects/{body['id']}", {"service_names": []})
    assert resp.status_code == 200 and resp.json()["service_names"] == []


def test_a_service_name_belongs_to_one_project_per_org(owner, api_for, github_ok, make_user):
    api = api_for(owner)
    first = _create(api, service_names=["worker"]).json()
    assert _create(api, service_names=["worker"]).status_code == 409
    second = _create(api).json()
    assert api.patch(f"/projects/{second['id']}", {"service_names": ["worker"]}).status_code == 409
    # The same project can keep its own names.
    assert api.patch(f"/projects/{first['id']}", {"service_names": ["worker", "w2"]}).status_code == 200
    # Another org is free to use the name.
    assert _create(api_for(make_user("x@x.com")), service_names=["worker"]).status_code == 201


def test_bad_service_names_are_rejected(owner, api_for, github_ok):
    api = api_for(owner)
    assert _create(api, service_names=[""]).status_code == 400
    assert _create(api, service_names=["x" * 256]).status_code == 400
    assert _create(api, service_names=[f"s{i}" for i in range(51)]).status_code == 400


def test_service_names_need_admin(owner, api_for, make_user, add_member):
    project = _project(owner, "api")
    viewer = make_user("v@x.com")
    add_member(project, viewer, "viewer")
    assert api_for(viewer).patch(f"/projects/{project.id}", {"service_names": ["a"]}).status_code == 403


# ---- the service graph ----------------------------------------------------------------------

def _graph(owner):
    api_project, worker_project = _project(owner, "api"), _project(owner, "worker", service_names=["worker"])
    graph = mesh.graph_for(api_project.organization_id, SOURCE)
    now = timezone.now()
    api = ServiceNode.objects.create(graph=graph, name="api", repo_url="https://github.com/acme/api",
                                     first_seen_at=now, last_seen_at=now)
    worker = ServiceNode.objects.create(graph=graph, name="worker", first_seen_at=now, last_seen_at=now)
    db = ServiceNode.objects.create(graph=graph, name="db:postgresql", kind="system",
                                    first_seen_at=now, last_seen_at=now)
    ServiceEdge.objects.create(graph=graph, client=api, server=worker, type="http", count=10,
                               error_count=2, first_seen_at=now, last_seen_at=now)
    ServiceEdge.objects.create(graph=graph, client=worker, server=db, type="db", count=4,
                               first_seen_at=now, last_seen_at=now)
    return api_project, worker_project


def test_graph_shows_nodes_mapped_to_projects(owner, api_for):
    api_project, worker_project = _graph(owner)
    org_id = personal_org(owner).id
    body = api_for(owner).get(f"/organizations/{org_id}/service-graph").json()
    assert body["source"] == SOURCE and body["refreshed_at"] is None
    nodes = {n["name"]: n for n in body["nodes"]}
    assert (nodes["api"]["project_id"], nodes["api"]["mapped_by"]) == (api_project.id, "vcs_attr")
    assert (nodes["worker"]["project_name"], nodes["worker"]["mapped_by"]) == ("worker", "service_names")
    assert (nodes["db:postgresql"]["kind"], nodes["db:postgresql"]["project_id"]) == ("system", None)
    http = next(e for e in body["edges"] if e["type"] == "http")
    assert (http["client_id"], http["server_id"]) == (nodes["api"]["id"], nodes["worker"]["id"])
    assert http["error_rate"] == 0.2


def test_graph_needs_a_source_when_the_org_uses_several(owner, api_for):
    _graph(owner)
    _project(owner, "other", source="uptrace.buggly.dev/2")
    api = api_for(owner)
    org_id = personal_org(owner).id
    assert api.get(f"/organizations/{org_id}/service-graph").status_code == 400
    body = api.get(f"/organizations/{org_id}/service-graph?source=uptrace.buggly.dev/2").json()
    assert body["nodes"] == [] and body["edges"] == []


def test_graph_is_hidden_from_non_members_and_with_the_mesh_off(owner, api_for, make_user, settings):
    _graph(owner)
    org_id = personal_org(owner).id
    assert api_for(make_user("x@x.com")).get(f"/organizations/{org_id}/service-graph").status_code == 404
    settings.SRE_SERVICE_MESH_ENABLED = False
    assert api_for(owner).get(f"/organizations/{org_id}/service-graph").status_code == 404


def test_refresh_needs_org_admin_and_starts_the_workflow(owner, api_for, make_user, monkeypatch):
    started = []
    monkeypatch.setattr(temporal_client, "start_graph_refresh", lambda org, source: started.append((org, source)))
    org = Organization.objects.create(name="Acme")
    OrganizationMembership.objects.create(organization=org, user=owner, role=OrgRole.OWNER)
    member = make_user("m@x.com")
    OrganizationMembership.objects.create(organization=org, user=member, role=OrgRole.MEMBER)

    assert api_for(member).post(f"/organizations/{org.id}/service-graph/refresh").status_code == 403
    assert api_for(owner).post(f"/organizations/{org.id}/service-graph/refresh?source={SOURCE}").status_code == 202
    assert started == [(org.id, SOURCE)]


def test_refresh_reports_temporal_being_down(owner, api_for, monkeypatch):
    def down(*a):
        raise RuntimeError("no temporal")
    monkeypatch.setattr(temporal_client, "start_graph_refresh", down)
    org_id = personal_org(owner).id
    assert api_for(owner).post(f"/organizations/{org_id}/service-graph/refresh").status_code == 503


# ---- incidents -------------------------------------------------------------------------

def test_incidents_show_source_parent_and_root_cause(owner, api_for):
    api_project, worker_project = _graph(owner)
    parent = IncidentRun.objects.create(project=api_project, trace_id="t", temporal_workflow_id="p",
                                        status="delegated", root_cause={"service_name": "worker"})
    child = IncidentRun.objects.create(project=worker_project, trace_id="t", temporal_workflow_id="c",
                                       source="linked", parent_incident_run=parent)
    api = api_for(owner)
    body = api.get(f"/incident-runs/{parent.id}").json()
    assert (body["source"], body["status"], body["root_cause"]) == ("alert", "delegated", {"service_name": "worker"})
    body = api.get(f"/incident-runs/{child.id}").json()
    assert (body["source"], body["parent_incident_run_id"]) == ("linked", parent.id)
