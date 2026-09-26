"""A merged fix sends runbook_variant agents after the same bug in related repos
(services/agents.on_fix_merged, called from record_playbook_outcome)."""

import pytest
from django.utils import timezone

from sre import activities, temporal_client
from sre.models import (
    AgentKind, IncidentRun, Playbook, PlaybookExecutionAttempt, PlaybookRun, Project, RemediationAgent,
    Runbook, ScanRun,
)
from sre.orgs import personal_org
from sre.services import agents as agent_service
from sre.services import scanning

pytestmark = pytest.mark.django_db


@pytest.fixture
def scans(monkeypatch):
    started = []
    monkeypatch.setattr(temporal_client, "start_scan", lambda wid, scan_run_id: started.append(wid))
    return started


@pytest.fixture
def owner(make_user):
    return make_user()


def _project(owner, repo, installation="1"):
    return Project.objects.create(name=repo, organization=personal_org(owner), github_installation_id=installation,
                                  github_repo_owner="acme", github_repo_name=repo)


def _agent(owner, kind=AgentKind.RUNBOOK_VARIANT, trigger="on_merge", name="", **kwargs):
    return RemediationAgent.objects.create(organization=personal_org(owner), name=name or f"{kind}-{trigger}",
                                           kind=kind, trigger=trigger, execution_mode="draft_only", **kwargs)


def _merged_fix(project, source="alert", scan_kind="", approved=True, status=PlaybookRun.Status.SUCCEEDED):
    """A fix whose PR a person merged, with the runbook saved from it."""
    playbook = Playbook.objects.create(title="Null checks", is_generic=True, origin="builtin", status="confirmed")
    run = IncidentRun.objects.create(project=project, trace_id=f"t{IncidentRun.objects.count()}",
                                     temporal_workflow_id=f"w{IncidentRun.objects.count()}",
                                     source=source, scan_kind=scan_kind)
    playbook_run = PlaybookRun.objects.create(
        incident_run=run, playbook=playbook, execution_mode="draft_only", status=status,
        pr_url=f"https://github.com/acme/{project.github_repo_name}/pull/1",
        approved_at=timezone.now() if approved else None)
    Runbook.objects.create(project=project, playbook=playbook, title="Handle optional assignees",
                           source_playbook_run=playbook_run)
    return playbook_run


def test_a_merged_fix_hunts_for_its_bug_in_the_other_repos(owner, scans):
    jira, confluence, trello = _project(owner, "jira-lite"), _project(owner, "confluence-lite"), _project(owner, "trello-lite")
    _project(owner, "jira-lite")  # a second project on the fixed repo: nothing to hunt there
    _project(owner, "elsewhere", installation="2")  # another GitHub account: can't see the fix
    agent = _agent(owner)
    fix = _merged_fix(jira)

    [scan] = agent_service.on_fix_merged(fix)
    assert (scan.agent, scan.trigger, scan.trigger_ref) == (agent, "fix_merged", fix.pr_url)
    assert scan.runbook == Runbook.objects.get(source_playbook_run=fix)
    assert [(r.project, r.base_sha, r.head_sha) for r in scan.repos.order_by("project_id")] == [
        (confluence, "", ""), (trello, "", "")]  # whole repos
    assert scans == [f"sre-scan-{agent.id}-fix-{fix.id}"]
    # A retried activity starts nothing new.
    assert agent_service.on_fix_merged(fix) == [] and ScanRun.objects.count() == 1


def test_only_accepted_fixes_propagate(owner, scans):
    jira, _ = _project(owner, "jira-lite"), _project(owner, "confluence-lite")
    _agent(owner)
    assert agent_service.on_fix_merged(_merged_fix(jira, approved=False)) == []  # autonomous: unreviewed
    assert agent_service.on_fix_merged(_merged_fix(jira, status=PlaybookRun.Status.REJECTED)) == []
    no_runbook = _merged_fix(jira)
    Runbook.objects.filter(source_playbook_run=no_runbook).delete()
    assert agent_service.on_fix_merged(no_runbook) == []
    # A fix that was itself a variant finding: the hunt that found it covered the others.
    assert agent_service.on_fix_merged(_merged_fix(jira, source="scan", scan_kind="runbook_variant")) == []
    assert scans == []


def test_only_runbook_variant_agents_on_merge_that_cover_the_fixed_repo(owner, scans):
    jira, confluence = _project(owner, "jira-lite"), _project(owner, "confluence-lite")
    _agent(owner, kind=AgentKind.PLAYBOOK_SWEEP)
    _agent(owner, trigger="schedule", schedule_cron="0 3 * * *")
    _agent(owner, name="paused", enabled=False)
    _agent(owner, name="confluence only").projects.set([confluence])  # doesn't cover jira-lite
    assert agent_service.on_fix_merged(_merged_fix(jira)) == []


def test_temporal_down_doesnt_break_the_fixes_bookkeeping(owner, monkeypatch):
    jira, _ = _project(owner, "jira-lite"), _project(owner, "confluence-lite")
    _agent(owner)

    def down(*args):
        raise RuntimeError("temporal down")

    monkeypatch.setattr(temporal_client, "start_scan", down)
    assert agent_service.on_fix_merged(_merged_fix(jira)) == []
    assert not ScanRun.objects.exists()  # removed again, so a redelivery can start afresh


def test_the_scan_hunts_for_that_runbook_only(owner):
    jira, confluence = _project(owner, "jira-lite"), _project(owner, "confluence-lite")
    agent = _agent(owner)
    wanted = Runbook.objects.get(source_playbook_run=_merged_fix(jira))
    Runbook.objects.get(source_playbook_run=_merged_fix(confluence))  # another fix, not this scan's
    assert len(scanning.variant_material(agent, confluence).data) == 2
    material = scanning.variant_material(agent, confluence, wanted.id)
    assert [c["id"] for c in material.data] == [wanted.id]


def test_recording_a_merged_fix_starts_the_hunt(owner, scans, settings):
    settings.SRE_RUNBOOKS_ENABLED = settings.SRE_REMEDIATION_AGENTS_ENABLED = True
    jira, _ = _project(owner, "jira-lite"), _project(owner, "confluence-lite")
    agent = _agent(owner)
    fix = _merged_fix(jira)
    Runbook.objects.filter(source_playbook_run=fix).delete()  # saved by the activity itself
    PlaybookExecutionAttempt.objects.create(
        playbook_run=fix, attempt_number=1, outcome="succeeded", summary="Guarded the assignee",
        generated_steps=[{"type": "run_command", "command": "pytest"}])
    activities.record_playbook_outcome(fix.id)
    assert Runbook.objects.filter(source_playbook_run=fix).exists()
    assert scans == [f"sre-scan-{agent.id}-fix-{fix.id}"]

    settings.SRE_REMEDIATION_AGENTS_ENABLED = False
    other = _merged_fix(jira)
    activities.record_playbook_outcome(other.id)
    assert len(scans) == 1
