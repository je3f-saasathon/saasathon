"""Service mesh: which neighbour repos a fix agent gets, and how paths into them work."""

from pathlib import Path

import pytest
from django.utils import timezone

from sre.models import ExecutionMode, IncidentRun, Playbook, PlaybookRun, ServiceEdge, ServiceNode
from sre.services import executor as executor_module
from sre.services import mesh
from sre.services.executor import PlaybookExecutor
from sre.services.sandbox import Sandbox, SandboxError

from .test_mesh import SOURCE, _project

pytestmark = pytest.mark.django_db


def test_relative_and_absolute_paths():
    box = Sandbox(Path("/tmp/x"), name="unused")
    assert box._path("src/app.py") == "/workspace/src/app.py"
    assert box._path("/workspace/src/app.py") == "/workspace/src/app.py"
    assert box._path("/src/app.py") == "/workspace/src/app.py"  # as before: relative to the repo
    with pytest.raises(SandboxError):
        box._path("../etc/passwd")
    # Without neighbours, /neighbours is just a path in the repo.
    assert box._read_path("/neighbours/a/x.py") == "/workspace/neighbours/a/x.py"

    box = Sandbox(Path("/tmp/x"), name="unused", neighbours={"a": Path("/tmp/a")})
    assert box._read_path("/neighbours/a/x.py") == "/neighbours/a/x.py"
    assert box._read_path("/neighbours/../etc/passwd") == "/workspace/etc/passwd"
    # Writes never leave /workspace.
    assert box._path("/neighbours/a/x.py") == "/workspace/neighbours/a/x.py"


@pytest.fixture
def mesh_on(settings):
    settings.SRE_SERVICE_MESH_ENABLED = True
    settings.SRE_MAX_NEIGHBOUR_REPOS = 3


def _run_for(project, service="worker"):
    run = IncidentRun.objects.create(project=project, trace_id="t", temporal_workflow_id=f"w-{project.id}",
                                     root_cause={"service_name": service})
    playbook = Playbook.objects.create(title="fix", project=project)
    return PlaybookRun.objects.create(incident_run=run, playbook=playbook,
                                      execution_mode=ExecutionMode.DRAFT_ONLY)


def _link(project, *names):
    graph = mesh.graph_for(project.organization_id, SOURCE)
    now = timezone.now()
    nodes = [ServiceNode.objects.get_or_create(graph=graph, name=n, defaults={
        "first_seen_at": now, "last_seen_at": now})[0] for n in names]
    for client, server in zip(nodes, nodes[1:]):
        ServiceEdge.objects.create(graph=graph, client=client, server=server, first_seen_at=now,
                                   last_seen_at=now)


def test_neighbours_must_share_the_github_installation(make_user, mesh_on):
    owner = make_user()
    worker = _project(owner, "worker", service_names=["worker"])
    api = _project(owner, "api", service_names=["api"])
    billing = _project(owner, "billing", service_names=["billing"])
    billing.github_installation_id = "999"  # another GitHub account's repo
    billing.save()
    _link(worker, "api", "worker", "billing")

    executor = PlaybookExecutor(_run_for(worker), 1, "")
    assert executor._neighbour_projects() == [api]


def test_no_neighbours_with_the_mesh_off_or_the_limit_at_zero(make_user, settings):
    owner = make_user()
    worker = _project(owner, "worker", service_names=["worker"])
    _project(owner, "api", service_names=["api"])
    _link(worker, "api", "worker")
    executor = PlaybookExecutor(_run_for(worker), 1, "")
    settings.SRE_SERVICE_MESH_ENABLED = False
    assert executor._neighbour_projects() == []
    settings.SRE_SERVICE_MESH_ENABLED, settings.SRE_MAX_NEIGHBOUR_REPOS = True, 0
    assert executor._neighbour_projects() == []


def test_a_neighbour_that_fails_to_clone_is_left_out(make_user, mesh_on, monkeypatch, tmp_path):
    owner = make_user()
    worker = _project(owner, "worker", service_names=["worker"])
    _project(owner, "api", service_names=["api"])
    _project(owner, "billing", service_names=["billing"])
    _link(worker, "api", "worker", "billing")

    def clone_snapshot(repo, dest):
        if repo.project.github_repo_name == "billing":
            raise executor_module.GitError("403")
        dest.mkdir(parents=True)
        (dest / "x.py").write_text("x")
    monkeypatch.setattr(executor_module.GitHubRepo, "clone_snapshot", clone_snapshot)

    executor = PlaybookExecutor(_run_for(worker), 1, "")
    executor._clone_neighbours(tmp_path / "neighbours")
    assert list(executor.neighbours) == ["acme-api"]
    assert not (tmp_path / "neighbours" / "acme-billing").exists()
