"""Remediation agents' scanner (services/scanning.py), with GitHub, the sandbox and the
model faked."""

from datetime import timedelta

import pytest
import responses
from django.utils import timezone

from sre import activities
from sre.crypto import encrypt
from sre.models import (
    AgentKind, ExecutionMode, IncidentRun, LLMProviderConfig, LLMUsage, Playbook, Project,
    ProjectMembership, ProjectRole, RemediationAgent, Runbook, ScanRepo, ScanRun, ServiceEdge,
    ServiceNode, UptraceCredential,
)
from sre.orgs import personal_org
from sre.services import mesh, scanning
from sre.temporal_types import RunInput, SearchInput

from .test_activities import FakeLLM, FakeSandbox

pytestmark = pytest.mark.django_db

SOURCE = "uptrace.buggly.dev/1"


class FakeRepo:
    files = {"src/orders.py": "def total(order):\n    return order.customer.tier\n"}
    compared = []
    head = "c0ffee"

    def __init__(self, project):
        self.project = project

    def head_sha(self, branch):
        return FakeRepo.head

    def clone_snapshot(self, dest, branch=""):
        for path, content in self.files.items():
            (dest / path).parent.mkdir(parents=True, exist_ok=True)
            (dest / path).write_text(content)

    def compare(self, base, head):
        FakeRepo.compared.append((base, head))
        return [{"path": "src/orders.py", "status": "modified", "patch": "+ return order.customer.tier"}]


@pytest.fixture
def fake_infra(monkeypatch, settings, tmp_path):
    settings.SRE_WORKDIR = str(tmp_path)
    settings.SRE_RUNBOOKS_ENABLED = True
    FakeSandbox.instances = []
    FakeRepo.compared = []
    FakeRepo.head = "c0ffee"
    monkeypatch.setattr(scanning, "GitHubRepo", FakeRepo)
    monkeypatch.setattr(scanning, "Sandbox", FakeSandbox)


@pytest.fixture
def owner(make_user):
    return make_user()


def _project(owner, name="shop", installation="1", **kwargs):
    project = Project.objects.create(
        name=name, organization=personal_org(owner), github_installation_id=installation,
        github_repo_owner="acme", github_repo_name=name, uptrace_source_id=SOURCE, **kwargs)
    ProjectMembership.objects.create(project=project, user=owner, role=ProjectRole.OWNER)
    config = LLMProviderConfig.objects.create(owner=owner, name=f"c-{name}", provider="anthropic",
                                              model="claude-sonnet-5", api_key_encrypted=encrypt("k"))
    project.default_llm_config = config
    project.save()
    return project


def _agent(owner, kind=AgentKind.PLAYBOOK_SWEEP, mode=ExecutionMode.DRAFT_ONLY, name="", **kwargs):
    return RemediationAgent.objects.create(organization=personal_org(owner), name=name or f"a-{kind}",
                                           kind=kind, execution_mode=mode, **kwargs)


def _scan_repo(agent, project, base="", head="", trigger="manual"):
    scan_run = ScanRun.objects.create(agent=agent, trigger=trigger,
                                      temporal_workflow_id=f"scan-{ScanRun.objects.count()}")
    return ScanRepo.objects.create(scan_run=scan_run, project=project, branch="main",
                                   base_sha=base, head_sha=head)


def _builtin(category="null_reference"):
    return Playbook.objects.create(title=f"Fix {category}", category=category, is_generic=True,
                                   origin=Playbook.Origin.BUILTIN, status=Playbook.Status.CONFIRMED)


FINDING = {"category": "null_reference", "title": "customer can be None",
           "message": "total() crashes for guest orders", "location": "src/orders.py:2",
           "evidence": "order.customer is None for guests"}


# ---- findings ------------------------------------------------------------------------

def test_findings_must_point_at_a_real_file_and_known_ids(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x")
    material = scanning.Material("playbooks", "", [1], playbook_ids={7}, runbook_ids={9})
    raw = [
        {**FINDING, "location": "/workspace/src/a.py:1", "playbook_id": 7, "runbook_id": 5},
        {**FINDING, "location": "src/missing.py:1"},
        {**FINDING, "location": "../../etc/passwd:1"},
        {**FINDING, "location": "src/a.py:3", "category": "made_up", "playbook_id": "x"},
        "not a dict",
    ]
    findings = scanning.clean_findings(raw, material, AgentKind.PLAYBOOK_SWEEP, tmp_path, limit=5)
    assert [(f.location, f.category, f.playbook_id, f.runbook_id) for f in findings] == [
        ("src/a.py:1", "null_reference", 7, None), ("src/a.py:3", "other", None, None)]
    assert all(f.evidence_kind == "code" for f in findings)
    assert len(scanning.clean_findings(raw, material, AgentKind.PLAYBOOK_SWEEP, tmp_path, limit=1)) == 1


def test_evidence_kind_depends_on_the_agent_kind(tmp_path):
    (tmp_path / "a.py").write_text("x")
    group = {"group_id": "42", "service_name": "api", "trace_id": "t"}
    material = scanning.Material("x", "", [1], runbook_ids={9}, groups={"42": group})
    raw = [{**FINDING, "location": "a.py:1", "runbook_id": 9, "group_id": "42"}]
    kinds = {kind: scanning.clean_findings(raw, material, kind, tmp_path, 3)[0].evidence_kind
             for kind in AgentKind.values}
    assert kinds == {"playbook_sweep": "code", "runbook_variant": "runbook", "find_quiet": "trace"}


@pytest.mark.parametrize("agent_mode,evidence,code_prs,expected", [
    ("draft_only", "code", False, "advisory_only"),
    ("draft_only", "code", True, "draft_only"),  # the agent lets code-only findings open PRs
    ("advisory_only", "code", True, "advisory_only"),  # ...but never past its own mode
    ("draft_only", "runbook", False, "draft_only"),
    ("advisory_only", "trace", False, "advisory_only"),
])
def test_mode_cap_is_the_lower_of_agent_and_evidence(owner, agent_mode, evidence, code_prs, expected):
    project = _project(owner)
    scan_repo = _scan_repo(_agent(owner, mode=agent_mode, code_findings_open_prs=code_prs), project)
    finding = scanning.Finding("null_reference", "t", "m", "src/a.py:1", "e", evidence)
    [run] = scanning.RepositoryScanner(scan_repo).record([finding])
    assert run.execution_mode_cap == expected
    assert run.source == IncidentRun.Source.SCAN and run.scan_run == scan_repo.scan_run


def test_a_finding_is_only_ever_raised_once(owner):
    project = _project(owner)
    agent = _agent(owner)
    finding = scanning.Finding("null_reference", "t", "m", "src/a.py:1", "e", "code")
    assert len(scanning.RepositoryScanner(_scan_repo(agent, project)).record([finding])) == 1
    assert scanning.RepositoryScanner(_scan_repo(agent, project)).record([finding]) == []
    assert IncidentRun.objects.count() == 1


def test_a_finding_whose_fix_was_rejected_is_raised_again(owner):
    """A closed PR leaves the bug in the repo, so the next scan that finds it starts fresh
    work instead of being deduped away for good."""
    project = _project(owner)
    agent = _agent(owner)
    finding = scanning.Finding("null_reference", "t", "m", "src/a.py:1", "e", "code")
    [first] = scanning.RepositoryScanner(_scan_repo(agent, project)).record([finding])
    IncidentRun.objects.filter(id=first.id).update(status=IncidentRun.Status.REJECTED)

    [second] = scanning.RepositoryScanner(_scan_repo(agent, project)).record([finding])
    assert second.id != first.id
    assert second.trace_id == f"{first.trace_id}-r2"
    assert second.temporal_workflow_id == f"sre-incident-{project.id}-{second.trace_id}"
    # Only a rejection reopens the question: while the new one runs, the same finding is it.
    assert scanning.RepositoryScanner(_scan_repo(agent, project)).record([finding]) == []

    IncidentRun.objects.filter(id=second.id).update(status=IncidentRun.Status.REJECTED)
    [third] = scanning.RepositoryScanner(_scan_repo(agent, project)).record([finding])
    assert third.trace_id == f"{first.trace_id}-r3"
    assert IncidentRun.objects.count() == 3


@pytest.mark.parametrize("status", ["succeeded", "failed", "no_anomaly"])
def test_a_finding_that_was_decided_some_other_way_stays_deduped(owner, status):
    project = _project(owner)
    agent = _agent(owner)
    finding = scanning.Finding("null_reference", "t", "m", "src/a.py:1", "e", "code")
    [first] = scanning.RepositoryScanner(_scan_repo(agent, project)).record([finding])
    IncidentRun.objects.filter(id=first.id).update(status=status)
    assert scanning.RepositoryScanner(_scan_repo(agent, project)).record([finding]) == []
    assert IncidentRun.objects.count() == 1
SERVICES = """import os


class Members:
    def add(self, project_id):
        project = Project.objects.get(pk=project_id)
        return project.members.create()

    def remove(self, project_id):
        return None


def invite(email):
    send_mail(email)
"""


def test_enclosing_symbol_is_the_innermost_function_or_class(tmp_path):
    path = tmp_path / "services.py"
    path.write_text(SERVICES)
    assert scanning.enclosing_symbol(path, "services.py:7") == "Members.add"
    assert scanning.enclosing_symbol(path, "services.py:6-8") == "Members.add"
    assert scanning.enclosing_symbol(path, "services.py:14") == "invite"
    assert scanning.enclosing_symbol(path, "services.py:1") == ""  # module level
    assert scanning.enclosing_symbol(path, "services.py") == ""  # no line
    (tmp_path / "broken.py").write_text("def (:")
    assert scanning.enclosing_symbol(tmp_path / "broken.py", "broken.py:1") == ""
    (tmp_path / "app.js").write_text("function f() {}")
    assert scanning.enclosing_symbol(tmp_path / "app.js", "app.js:1") == ""


def test_the_same_bug_keeps_its_fingerprint_when_the_line_moves(tmp_path):
    (tmp_path / "services.py").write_text(SERVICES)
    material = scanning.Material("playbooks", "", [1], playbook_ids={7})

    def fingerprint(location, category="validation", playbook_id=7):
        raw = [{**FINDING, "location": location, "category": category, "playbook_id": playbook_id}]
        [finding] = scanning.clean_findings(raw, material, AgentKind.PLAYBOOK_SWEEP, tmp_path, 3)
        return finding.fingerprint

    same = fingerprint("services.py:6")
    assert fingerprint("services.py:7") == same
    assert fingerprint("services.py:7", category="database") == same  # the playbook anchors it
    assert fingerprint("services.py:11") != same  # another method
    assert fingerprint("services.py:7", playbook_id=None) != same


def test_one_reply_reporting_a_bug_twice_raises_it_once(tmp_path):
    (tmp_path / "services.py").write_text(SERVICES)
    material = scanning.Material("playbooks", "", [1], playbook_ids={7})
    raw = [{**FINDING, "location": loc, "playbook_id": 7} for loc in ("services.py:6", "services.py:7", "services.py:14")]
    findings = scanning.clean_findings(raw, material, AgentKind.PLAYBOOK_SWEEP, tmp_path, 3)
    assert [f.location for f in findings] == ["services.py:6", "services.py:14"]


# ---- material per kind --------------------------------------------------------------

def test_sweep_uses_the_agents_playbooks_or_every_visible_one(owner, settings):
    settings.SRE_RUNBOOKS_ENABLED = True
    project = _project(owner)
    Playbook.objects.update(status="failing")  # the seeded built-ins
    a, b = _builtin("null_reference"), _builtin("timeout")
    Playbook.objects.filter(id=_builtin("database").id).update(status="failing")
    agent = _agent(owner)
    assert scanning.sweep_material(agent, project).playbook_ids == {a.id, b.id}
    agent.playbooks.set([b])
    assert scanning.sweep_material(agent, project).playbook_ids == {b.id}


def test_variant_runbooks_come_from_the_same_github_account_neighbours_first(owner, settings):
    settings.SRE_RUNBOOKS_ENABLED = True
    playbook = _builtin()
    api = _project(owner, "api", service_names=["api"])
    worker = _project(owner, "worker", service_names=["worker"])
    other = _project(owner, "billing", service_names=["billing"])
    foreign = _project(owner, "foreign", installation="999")
    rb = lambda p: Runbook.objects.create(project=p, playbook=playbook, title=f"rb-{p.name}")
    own, from_worker, from_billing, from_foreign = rb(api), rb(worker), rb(other), rb(foreign)
    graph = mesh.graph_for(api.organization_id, SOURCE)
    now = timezone.now()
    n_api, n_worker = (ServiceNode.objects.create(graph=graph, name=n, first_seen_at=now, last_seen_at=now)
                       for n in ("api", "worker"))
    ServiceEdge.objects.create(graph=graph, client=n_api, server=n_worker, first_seen_at=now, last_seen_at=now)

    material = scanning.variant_material(_agent(owner, AgentKind.RUNBOOK_VARIANT), api)
    assert [c["id"] for c in material.data] == [from_worker.id, own.id, from_billing.id]
    assert from_foreign.id not in material.runbook_ids


@responses.activate
def test_quiet_errors_are_this_repos_services_minus_ones_already_raised(owner, settings):
    settings.SRE_UPTRACE_FETCH_ENABLED = True
    project = _project(owner, "api", service_names=["api"])
    UptraceCredential.objects.create(organization=project.organization, name="m", host="uptrace.buggly.dev",
                                     api_base_url="https://uptrace.buggly.dev", token_encrypted=encrypt("t"))
    IncidentRun.objects.create(project=project, trace_id="x", temporal_workflow_id="x",
                               telemetry={"group_id": "1"})
    responses.add(responses.GET, "https://uptrace.buggly.dev/internal/v1/spans/1/groups", json={"groups": [
        {"_group_id": "1", "service_name::str": "api", "count()": 50, "_display_name": "already raised"},
        {"_group_id": "2", "service_name::str": "api", "count()": 3, "_display_name": "KeyError: 'sku'",
         "exception_type::str": "KeyError", "_trace_id": "abc"},
        {"_group_id": "3", "service_name::str": "worker", "count()": 9, "_display_name": "other repo"},
    ]})
    material = scanning.quiet_material(project)
    assert list(material.groups) == ["2"]
    assert material.data[0]["message"] == "KeyError: 'sku'" and material.data[0]["count"] == 3
    assert "system=log%3Aerror" in responses.calls[0].request.url


# ---- a whole scan ----------------------------------------------------------------------

def test_scan_records_findings_and_usage(owner, fake_infra, monkeypatch):
    project = _project(owner)
    playbook = _builtin()
    llm = FakeLLM(monkeypatch,
                  {"action": "read_file", "path": "src/orders.py"},
                  {"action": "finish", "findings": [{**FINDING, "playbook_id": playbook.id}]},
                  usage={"input": 100, "output": 20})
    scan_repo = _scan_repo(_agent(owner), project)
    with activities.scan_usage_scope(scan_repo.id, "repository_scan"):
        created = scanning.scan_repository(scan_repo)

    [run] = created
    assert run.telemetry["location"] == "src/orders.py:2"
    assert run.telemetry["suggested_playbook_id"] == playbook.id
    assert run.execution_mode_cap == "advisory_only"  # a sweep finding: the scanner's word only
    scan_repo.refresh_from_db()
    assert (scan_repo.status, scan_repo.finding_count) == ("succeeded", 1)
    assert FakeSandbox.instances[0].network == "none"
    assert "Fix null_reference" in llm.prompts[0][1][0]["content"]
    rows = LLMUsage.objects.filter(scan_repo=scan_repo)
    assert [(u.incident_run, u.input_tokens, u.step) for u in rows] == [(None, 100, "repository_scan")] * 2
    assert scanning.agent_tokens_this_month(scan_repo.scan_run.agent) == 240


def test_diff_scans_look_at_the_change_and_skip_empty_ones(owner, fake_infra, monkeypatch):
    project = _project(owner)
    _builtin()
    llm = FakeLLM(monkeypatch, {"action": "finish", "findings": []})
    scan_repo = _scan_repo(_agent(owner), project, base="abc^1", head="abc")
    assert scanning.scan_repository(scan_repo) == []
    assert FakeRepo.compared == [("abc^1", "abc")]
    assert "only look for bugs this change" in llm.prompts[0][1][0]["content"].lower()

    monkeypatch.setattr(FakeRepo, "compare", lambda self, b, h: [])
    FakeSandbox.instances = []
    assert scanning.scan_repository(_scan_repo(_agent(owner, name="x"), project, "a", "b")) == []
    assert FakeSandbox.instances == []  # nothing changed: no sandbox, no tokens


def test_a_scheduled_scan_skips_a_commit_the_agent_already_scanned(owner, fake_infra, monkeypatch):
    project = _project(owner)
    _builtin()
    FakeLLM(monkeypatch, *[{"action": "finish", "findings": []}] * 3)
    agent = _agent(owner)
    first = _scan_repo(agent, project, trigger="schedule")
    scanning.scan_repository(first)
    first.refresh_from_db()
    assert (first.status, first.head_sha) == ("succeeded", "c0ffee")

    again = _scan_repo(agent, project, trigger="schedule")
    FakeSandbox.instances = []
    assert scanning.scan_repository(again) == []
    again.refresh_from_db()
    assert again.status == "skipped" and f"since scan run {first.scan_run_id}" in again.error
    assert FakeSandbox.instances == []  # no clone, no tokens

    scanning.scan_repository(manual := _scan_repo(agent, project))  # asked for by hand: scans
    FakeRepo.head = "beef"
    scanning.scan_repository(moved := _scan_repo(agent, project, trigger="schedule"))  # new commits
    manual.refresh_from_db(), moved.refresh_from_db()
    assert (manual.status, moved.status) == ("succeeded", "succeeded")
    assert scanning.finish_scan_run(again.scan_run) == "succeeded"


def test_nothing_to_hunt_for_spends_nothing(owner, fake_infra):
    project = _project(owner)  # no services, so no quiet errors to look into
    scan_repo = _scan_repo(_agent(owner, AgentKind.FIND_QUIET), project)
    assert scanning.scan_repository(scan_repo) == []
    assert FakeSandbox.instances == []
    scan_repo.refresh_from_db()
    assert scan_repo.status == "succeeded"


def test_budget_reached_fails_the_repo_without_scanning(owner, fake_infra):
    project = _project(owner)
    _builtin()
    agent = _agent(owner, monthly_token_budget=100)
    earlier = _scan_repo(agent, project)
    LLMUsage.objects.create(scan_repo=earlier, step="repository_scan", input_tokens=150)
    scan_repo = _scan_repo(agent, project)
    assert scanning.scan_repository(scan_repo) == []
    scan_repo.refresh_from_db()
    assert scan_repo.status == "failed" and "budget" in scan_repo.error


def test_running_out_of_turns_fails_the_repo(owner, fake_infra, monkeypatch):
    project = _project(owner)
    _builtin()
    monkeypatch.setattr(scanning, "MAX_TURNS", 2)
    FakeLLM(monkeypatch, {"action": "list_files"}, {"action": "list_files"})
    scan_repo = _scan_repo(_agent(owner), project)
    assert scanning.scan_repository(scan_repo) == []
    scan_repo.refresh_from_db()
    assert scan_repo.status == "failed" and "out of turns" in scan_repo.error


def test_finish_scan_run_sums_up_the_repos(owner):
    project, other = _project(owner), _project(owner, "b")
    agent = _agent(owner)
    scan_repo = _scan_repo(agent, project)
    ScanRepo.objects.create(scan_run=scan_repo.scan_run, project=other, status="succeeded")
    # The first repo never finished: counted as failed, so the run is partial.
    assert scanning.finish_scan_run(scan_repo.scan_run) == "partial"
    scan_repo.refresh_from_db()
    assert scan_repo.status == "failed" and scan_repo.scan_run.finished_at is not None


# ---- scan incidents in the pipeline ---------------------------------------------------

def _scan_incident(project, cap="draft_only", **telemetry):
    return IncidentRun.objects.create(project=project, trace_id="scan-x", temporal_workflow_id="scan-x",
                                      source="scan", execution_mode_cap=cap, telemetry=telemetry)


def test_scan_incidents_never_run_autonomous(owner, settings):
    settings.SRE_RUNBOOKS_ENABLED = True
    project = _project(owner, default_execution_mode="autonomous")
    playbook = _builtin()
    runbook = Runbook.objects.create(project=project, playbook=playbook, title="r", status="confirmed")
    run = _scan_incident(project, cap="draft_only")
    info = activities.create_playbook_run(RunInput(run.id, playbook.id, runbook.id))
    assert info.execution_mode == "draft_only"

    advisory = IncidentRun.objects.create(project=project, trace_id="s2", temporal_workflow_id="s2",
                                          source="scan", execution_mode_cap="advisory_only")
    assert activities.create_playbook_run(RunInput(advisory.id, playbook.id)).execution_mode == "advisory_only"


def test_the_scanners_suggestions_go_first(owner, settings):
    settings.SRE_RUNBOOKS_ENABLED = True
    project, other = _project(owner), _project(owner, "other")
    suggested, runbook_playbook = _builtin("timeout"), _builtin("database")
    foreign_runbook = Runbook.objects.create(project=other, playbook=runbook_playbook, title="r")
    run = _scan_incident(project, suggested_playbook_id=suggested.id,
                         suggested_runbook_id=foreign_runbook.id)
    candidates = activities.find_candidates(SearchInput(project.id, ["nothing"], run.id, "other"))
    # Another repo's runbook can't be followed here; its playbook stands in for it.
    assert candidates.playbook_ids[:2] == [runbook_playbook.id, suggested.id]
    assert foreign_runbook.id not in candidates.runbook_ids


def test_scan_tokens_on_our_keys_count_toward_the_projects_cap(owner):
    from sre.llm import platform

    project = _project(owner)
    scan_repo = _scan_repo(_agent(owner), project)
    LLMUsage.objects.create(scan_repo=scan_repo, step="repository_scan", billed_to="platform",
                            input_tokens=70, output_tokens=5)
    LLMUsage.objects.create(scan_repo=scan_repo, step="repository_scan", billed_to="user", input_tokens=999)
    assert platform.tokens_this_month(project) == 75


def test_the_anomaly_check_reviews_a_scan_finding_as_a_claim(owner, monkeypatch):
    from sre.services.triage import AnomalyChecker

    project = _project(owner)
    llm = FakeLLM(monkeypatch, {"is_anomaly": False, "reasoning": "handled upstream"})
    result = AnomalyChecker(_scan_incident(project, message="x")).check()
    assert result.is_anomaly is False
    assert "automated code scanner" in llm.prompts[0][0]
