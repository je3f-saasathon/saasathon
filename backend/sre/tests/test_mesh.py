import json
from datetime import timedelta

import pytest
import responses
from django.utils import timezone

from sre import activities, temporal_client
from sre.crypto import encrypt
from sre.models import (
    IncidentRun, Organization, Project, ProjectMembership, ProjectRole, ServiceEdge, ServiceGraph,
    ServiceNode, UptraceCredential,
)
from sre.orgs import personal_org
from sre.services import mesh
from sre.temporal_types import GraphRefreshInput, IncidentInput

pytestmark = pytest.mark.django_db

API = "https://uptrace.buggly.dev"
SOURCE = "uptrace.buggly.dev/1"
TRACE_ID = "b71c368ef5d2c666186e42420e443e10"


def _span(id, service, parent=None, status="ok", system="", kind="server", time=0, repo="", **extra):
    attrs = {"service_name::str": service}
    if repo:
        attrs["vcs_repository_url_full::str"] = repo
    span = {"id": id, "traceId": TRACE_ID, "kind": kind, "system": system or f"httpserver:{service}",
            "name": f"op-{id}", "statusCode": status, "time": time, "attrs": attrs, **extra}
    if parent:
        span["parentId"] = parent
    return span


# api -> worker over HTTP; the worker throws, its DB call itself succeeded.
SPANS = [
    _span("a1", "api", status="error", time=1, repo="https://github.com/acme/api"),
    _span("a2", "api", "a1", status="error", system="httpclient:api", kind="client", time=2),
    _span("w1", "worker", "a2", status="error", time=3, repo="https://github.com/Acme/worker.git",
          statusMessage="IntegrityError", logs=[{
              "eventName": "exception", "system": "log:error",
              "displayName": "IntegrityError: duplicate key value",
              "attrs": {"exception_type::str": "IntegrityError",
                        "exception_stacktrace::str": "Traceback ...\nIntegrityError"}}]),
    _span("w2", "worker", "w1", system="db:postgresql", kind="client", time=4),
    _span("a3", "api", "a1", kind="internal", time=5),
]


@pytest.fixture(autouse=True)
def mesh_on(settings):
    settings.SRE_SERVICE_MESH_ENABLED = True
    settings.SRE_UPTRACE_FETCH_ENABLED = True


@pytest.fixture
def owner(make_user):
    return make_user()


def _project(owner, repo, org=None, source=SOURCE, **kwargs):
    project = Project.objects.create(
        name=repo, organization=org or personal_org(owner), github_installation_id="1",
        github_repo_owner="acme", github_repo_name=repo, uptrace_source_id=source, **kwargs,
    )
    ProjectMembership.objects.create(project=project, user=owner, role=ProjectRole.OWNER)
    return project


def _credential(org):
    return UptraceCredential.objects.create(organization=org, name="main", host="uptrace.buggly.dev",
                                            api_base_url=API, token_encrypted=encrypt("tok"))


def _incident(project, **kwargs):
    kwargs.setdefault("telemetry", {"trace_id": TRACE_ID, "service_name": "api"})
    return IncidentRun.objects.create(project=project, trace_id="uptrace-alert-1",
                                      temporal_workflow_id=f"w-{project.id}-{IncidentRun.objects.count()}",
                                      **kwargs)


def _mock_trace(spans=SPANS):
    responses.add(responses.GET, f"{API}/internal/v1/traces/1/{TRACE_ID}",
                  json={"id": TRACE_ID, "spans": spans})


# ---- the trace walk -------------------------------------------------------------------

def test_culprit_is_the_deepest_failed_span_without_failed_children():
    culprit = mesh.find_culprit(SPANS)
    assert culprit.span["id"] == "w1"
    assert culprit.path == ["api", "worker"]
    telemetry = mesh.culprit_telemetry(culprit, TRACE_ID)
    assert telemetry["exception_type"] == "IntegrityError"
    assert telemetry["message"] == "IntegrityError: duplicate key value"
    assert telemetry["service_name"] == "worker" and "Traceback" in telemetry["stacktrace"]


def test_a_failing_dependency_call_is_the_culprit_and_names_the_system():
    spans = [dict(s) for s in SPANS]
    spans[3] = {**spans[3], "statusCode": "error"}
    culprit = mesh.find_culprit(spans)
    assert culprit.span["id"] == "w2"
    assert mesh._dependency(culprit.span) == "db:postgresql"


def test_the_earliest_of_equally_deep_failures_wins():
    spans = [_span("r", "api", status="error"),
             _span("x", "billing", "r", status="error", time=9),
             _span("y", "worker", "r", status="error", time=3)]
    assert mesh.find_culprit(spans).span["id"] == "y"


def test_no_failed_span_means_no_culprit():
    assert mesh.find_culprit([_span("a", "api"), _span("b", "worker", "a")]) is None


def test_a_parent_cycle_does_not_hang():
    spans = [_span("a", "api", "b", status="error"), _span("b", "worker", "a", status="error")]
    mesh.find_culprit(spans)  # both have a failed child; must simply return


# ---- mapping services to projects ---------------------------------------------------

def test_repo_attribute_wins_over_service_names(owner):
    api = _project(owner, "api", service_names=["worker"])
    worker = _project(owner, "worker")
    projects = mesh.mesh_projects(api.organization_id, SOURCE)
    assert mesh.map_service("worker", "git@github.com:ACME/worker.git", projects) == (worker, "vcs_attr")
    assert mesh.map_service("worker", "", projects) == (api, "service_names")
    assert mesh.map_service("unknown", "https://gitlab.com/acme/worker", projects) == (None, "")


def test_only_projects_in_the_same_org_and_uptrace_project_count(owner, make_user):
    _project(owner, "worker", source="uptrace.buggly.dev/2")
    other_org = Organization.objects.create(name="other")
    _project(make_user("x@x.com"), "worker", org=other_org)
    org_id = personal_org(owner).id
    assert mesh.map_service("worker", "https://github.com/acme/worker",
                            mesh.mesh_projects(org_id, SOURCE)) == (None, "")


# ---- localize (the pipeline step) ---------------------------------------------------

@responses.activate
def test_culprit_in_another_project_gets_a_linked_child(owner):
    api, worker = _project(owner, "api"), _project(owner, "worker")
    _credential(api.organization)
    _mock_trace()
    run = _incident(api)

    child = mesh.localize(run)

    assert child.project == worker and child.source == IncidentRun.Source.LINKED
    assert child.parent_incident_run == run
    assert child.telemetry["exception_type"] == "IntegrityError"
    assert child.temporal_workflow_id == f"sre-incident-{worker.id}-linked-{run.id}"
    run.refresh_from_db()
    assert run.root_cause["service_name"] == "worker"
    assert run.root_cause["path"] == ["api", "worker"]
    assert run.root_cause["project_id"] == worker.id and run.root_cause["mapped_by"] == "vcs_attr"
    assert run.root_cause["linked_incident_run_id"] == child.id

    # The trace's edges are in the graph: api -> worker and worker -> its database.
    graph = ServiceGraph.objects.get(organization=api.organization, source=SOURCE)
    edges = {(e.client.name, e.server.name, e.type) for e in graph.edges.select_related("client", "server")}
    assert edges == {("api", "worker", "http"), ("worker", "db:postgresql", "db")}
    assert graph.nodes.get(name="worker").repo_url == "https://github.com/Acme/worker.git"

    # A retried activity finds the same child.
    _mock_trace()
    assert mesh.localize(run).id == child.id
    assert IncidentRun.objects.filter(source=IncidentRun.Source.LINKED).count() == 1


@responses.activate
def test_culprit_in_the_same_project_just_records_the_root_cause(owner):
    api = _project(owner, "api", service_names=["worker"])
    _credential(api.organization)
    _mock_trace([{**s, "attrs": {"service_name::str": s["attrs"]["service_name::str"]}} for s in SPANS])
    run = _incident(api)
    assert mesh.localize(run) is None
    run.refresh_from_db()
    assert run.root_cause["project_id"] == api.id and run.root_cause["linked_incident_run_id"] is None


@responses.activate
def test_a_linked_incident_never_delegates_again(owner):
    api, _ = _project(owner, "api"), _project(owner, "worker")
    _credential(api.organization)
    _mock_trace()
    run = _incident(api, source=IncidentRun.Source.LINKED)
    assert mesh.localize(run) is None


@responses.activate
def test_unmapped_culprit_stays_here(owner):
    api = _project(owner, "api")
    _credential(api.organization)
    _mock_trace()
    run = _incident(api)
    assert mesh.localize(run) is None
    run.refresh_from_db()
    assert run.root_cause["service_name"] == "worker" and run.root_cause["project_id"] is None


@responses.activate
def test_uptrace_failure_leaves_the_incident_alone(owner):
    api, _ = _project(owner, "api"), _project(owner, "worker")
    _credential(api.organization)
    responses.add(responses.GET, f"{API}/internal/v1/traces/1/{TRACE_ID}", status=500)
    run = _incident(api)
    assert mesh.localize(run) is None
    run.refresh_from_db()
    assert run.root_cause == {}


def test_activity_is_a_no_op_with_the_mesh_off(owner, settings):
    settings.SRE_SERVICE_MESH_ENABLED = False
    run = _incident(_project(owner, "api"))
    assert activities.localize_root_cause(IncidentInput(run.id, run.project_id)).child_incident_run_id is None
    assert activities.list_graph_targets(GraphRefreshInput()) == []


def test_telemetry_fetch_keeps_a_linked_incidents_own_telemetry(owner):
    run = _incident(_project(owner, "worker"), source=IncidentRun.Source.LINKED,
                    telemetry={"trace_id": TRACE_ID, "service_name": "worker"})
    activities.fetch_incident_telemetry(IncidentInput(run.id, run.project_id))
    run.refresh_from_db()
    assert run.telemetry["service_name"] == "worker"


# ---- refreshing the graph from Uptrace ----------------------------------------------

UPTRACE_GRAPH = {"edges": [
    {"type": "http", "clientAttr": "", "clientName": "<http-client>", "serverAttr": "service_name",
     "serverName": "api", "count": 19, "errorCount": 5, "durationAvg": 22.8, "durationMax": 78.5,
     "rate": 0.3},
    {"type": "db", "clientAttr": "service_name", "clientName": "worker", "serverAttr": "_system",
     "serverName": "db:postgresql", "count": 46, "errorCount": 0, "durationAvg": 0.35,
     "durationMax": 13.0, "rate": 0.7},
], "totalEdges": 2}
REPOS = {"groups": [
    {"service_name::str": "worker", "vcs_repository_url_full::str": "https://github.com/acme/worker"},
    {"service_name::str": "api", "vcs_repository_url_full::str": None},
], "hasMore": False}


@responses.activate
def test_refresh_copies_uptraces_graph_and_expires_old_edges(owner):
    api = _project(owner, "api")
    _credential(api.organization)
    graph = mesh.graph_for(api.organization_id, SOURCE)
    old = timezone.now() - timedelta(days=30)
    a = ServiceNode.objects.create(graph=graph, name="old-a", first_seen_at=old, last_seen_at=old)
    b = ServiceNode.objects.create(graph=graph, name="old-b", first_seen_at=old, last_seen_at=old)
    ServiceEdge.objects.create(graph=graph, client=a, server=b, first_seen_at=old, last_seen_at=old)
    responses.add(responses.GET, f"{API}/internal/v1/service-graph/1", json=UPTRACE_GRAPH)
    responses.add(responses.GET, f"{API}/internal/v1/spans/1/groups", json=REPOS)

    assert mesh.refresh_graph(api.organization_id, SOURCE) is True

    graph.refresh_from_db()
    assert graph.refreshed_at is not None
    assert set(graph.nodes.values_list("name", "kind")) == {
        ("<http-client>", "system"), ("api", "service"), ("worker", "service"), ("db:postgresql", "system")}
    edge = graph.edges.get(server__name="api")
    assert (edge.count, edge.error_count, edge.error_rate) == (19, 5, 5 / 19)
    assert graph.nodes.get(name="worker").repo_url == "https://github.com/acme/worker"
    # Both calls asked for a one-hour window.
    assert "time_gte=" in responses.calls[0].request.url


def test_refresh_without_a_credential_does_nothing(owner):
    api = _project(owner, "api")
    assert mesh.refresh_graph(api.organization_id, SOURCE) is False
    assert not ServiceGraph.objects.exists()


def test_refresh_targets_are_the_pinned_org_sources(owner):
    _project(owner, "api")
    _project(owner, "worker")
    _project(owner, "web", source="")
    assert mesh.refresh_targets() == [(personal_org(owner).id, SOURCE)]


# ---- neighbours -----------------------------------------------------------------------

def test_neighbours_are_mapped_services_on_either_side(owner):
    api, worker, billing = _project(owner, "api"), _project(owner, "worker"), _project(owner, "billing")
    api.service_names, billing.service_names = ["api"], ["billing"]
    api.save(), billing.save()
    graph = mesh.graph_for(api.organization_id, SOURCE)
    now = timezone.now()
    node = lambda name, repo="": ServiceNode.objects.create(graph=graph, name=name, repo_url=repo,
                                                            first_seen_at=now, last_seen_at=now)
    n_api, n_worker, n_billing = node("api"), node("worker", "https://github.com/acme/worker"), node("billing")
    n_db = ServiceNode.objects.create(graph=graph, name="db:postgresql", kind="system",
                                      first_seen_at=now, last_seen_at=now)
    for client, server in [(n_api, n_worker), (n_worker, n_billing), (n_worker, n_db)]:
        ServiceEdge.objects.create(graph=graph, client=client, server=server, first_seen_at=now,
                                   last_seen_at=now)
    assert set(mesh.neighbour_projects(worker, "worker", 3)) == {api, billing}
    assert mesh.neighbour_projects(worker, "worker", 1) in ([api], [billing])
    assert mesh.neighbour_projects(worker, "nope", 3) == []
