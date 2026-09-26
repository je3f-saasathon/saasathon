"""The pipeline with SRE_RUNBOOKS_ENABLED: search, the combined judge, the generic author,
the autonomy cap, the agent's runbook proposal, and outcomes."""

import pytest

from sre import activities
from sre.models import Playbook, PlaybookExecutionAttempt, PlaybookRun, Runbook
from sre.services.executor import PlaybookExecutor
from sre.temporal_types import IncidentInput, JudgeInput, RunInput, SearchInput
from sre.tests.test_activities import FakeLLM, fake_infra, incident, project  # shared fixtures

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def runbooks_on(settings):
    settings.SRE_RUNBOOKS_ENABLED = True


@pytest.fixture
def builtin():
    return Playbook.objects.get(slug="timeout")


def _runbook(project, playbook, **kw):
    defaults = dict(title="Close DB connections in the report job", area="reports",
                    keywords=["timeout", "pool"], service_name="shop-api",
                    steps=[{"type": "edit_file", "path": "reports/export.py", "instructions": "x"}])
    return Runbook.objects.create(project=project, playbook=playbook, **{**defaults, **kw})


# ---- search -----------------------------------------------------------------------------

def test_search_finds_runbooks_and_playbooks_by_category(project, incident, builtin):
    runbook = _runbook(project, builtin)
    _runbook(project, builtin, title="failing one", status="failing")
    incident.telemetry = {"service_name": "shop-api"}
    incident.save()
    found = activities.find_candidates(SearchInput(project.id, ["pool"], incident.id, "timeout"))
    assert found.runbook_ids == [runbook.id]
    assert found.playbook_ids[0] == builtin.id  # the category match ranks first


def test_search_is_the_old_one_with_runbooks_off(project, incident, builtin, settings):
    settings.SRE_RUNBOOKS_ENABLED = False
    _runbook(project, builtin)
    own = Playbook.objects.create(project=project, title="pool", keywords=["pool"])
    found = activities.find_candidates(SearchInput(project.id, ["pool"], incident.id, "timeout"))
    assert found.playbook_ids == [own.id] and found.runbook_ids == []


# ---- judge ------------------------------------------------------------------------------

def test_judge_can_pick_a_runbook(monkeypatch, project, incident, builtin):
    runbook = _runbook(project, builtin)
    llm = FakeLLM(monkeypatch, {"kind": "runbook", "id": runbook.id, "confidence": 0.9,
                                "reasoning": "same job"})
    result = activities.judge_match(JudgeInput(incident.id, [builtin.id], [runbook.id]))
    assert (result.matched_runbook_id, result.matched_playbook_id) == (runbook.id, builtin.id)
    incident.refresh_from_db()
    assert incident.matched_runbook_id == runbook.id and incident.matched_playbook_id == builtin.id
    prompt = llm.prompts[0][1][0]["content"]
    assert "reports/export.py" in prompt  # runbook cards list the files they touch


def test_judge_can_pick_a_playbook_or_nothing(monkeypatch, project, incident, builtin):
    FakeLLM(monkeypatch, {"kind": "playbook", "id": builtin.id, "confidence": 0.8, "reasoning": "r"},
            {"kind": "playbook", "id": builtin.id, "confidence": 0.3, "reasoning": "unsure"},
            {"kind": "runbook", "id": 999999, "confidence": 0.9, "reasoning": "made up"})
    assert activities.judge_match(JudgeInput(incident.id, [builtin.id])).matched_playbook_id == builtin.id
    assert activities.judge_match(JudgeInput(incident.id, [builtin.id])).matched_playbook_id is None
    assert activities.judge_match(JudgeInput(incident.id, [builtin.id])).matched_playbook_id is None


def test_judge_ignores_another_projects_runbook(monkeypatch, project, incident, builtin,
                                                 make_user, make_project):
    other = _runbook(make_project(make_user("z@x.com"), name="other"), builtin)
    FakeLLM(monkeypatch, {"kind": "runbook", "id": other.id, "confidence": 0.9, "reasoning": "r"})
    result = activities.judge_match(JudgeInput(incident.id, [builtin.id], [other.id]))
    assert result.matched_runbook_id is None and result.matched_playbook_id is None


# ---- no match: a generic playbook ---------------------------------------------------------

def test_no_match_writes_a_generic_org_playbook(monkeypatch, project, incident):
    incident.classification = {"category": "timeout", "summary": "pool exhausted"}
    incident.save()
    FakeLLM(monkeypatch, {"title": "DB pool exhaustion", "description": "d", "symptoms": "s",
                          "keywords": ["TimeoutError"],
                          "steps": [{"type": "investigate", "instructions": "find the holder"},
                                    {"type": "edit_file", "path": "db.py", "instructions": "x"},
                                    {"type": "verify", "instructions": "load test"}]})
    playbook = Playbook.objects.get(id=activities.create_playbook(IncidentInput(incident.id, project.id)))
    assert playbook.is_generic and playbook.organization_id == project.organization_id
    assert playbook.category == "timeout" and playbook.status == "unconfirmed"
    assert [s["type"] for s in playbook.steps] == ["investigate", "verify"]  # no file steps


# ---- how far a run may go ---------------------------------------------------------------

@pytest.mark.parametrize("runbook_status,expected", [
    (None, "draft_only"),           # a playbook alone, even a confirmed built-in
    ("unconfirmed", "draft_only"),
    ("confirmed", "autonomous"),
])
def test_autonomous_needs_a_confirmed_runbook(project, incident, builtin, runbook_status, expected):
    project.default_execution_mode = "autonomous"
    project.save()
    runbook = _runbook(project, builtin, status=runbook_status) if runbook_status else None
    info = activities.create_playbook_run(RunInput(incident.id, builtin.id,
                                                   runbook.id if runbook else None))
    playbook_run = PlaybookRun.objects.get(id=info.playbook_run_id)
    assert info.execution_mode == expected and playbook_run.runbook == runbook


def test_run_refuses_another_projects_runbook(project, incident, builtin, make_user, make_project):
    other = _runbook(make_project(make_user("z@x.com"), name="other"), builtin)
    with pytest.raises(Runbook.DoesNotExist):
        activities.create_playbook_run(RunInput(incident.id, builtin.id, other.id))


# ---- the agent proposes a runbook ----------------------------------------------------------

AGENT_TURNS = [
    {"action": "write_file", "path": "reports/export.py", "content": "..."},
    {"action": "run_command", "command": "pytest tests/test_export.py"},
    {"action": "finish", "summary": "stream the export", "tests_passed": True,
     "runbook": {"title": "Stream the report export", "area": "reports",
                 "steps": [{"type": "edit_file", "path": "reports/export.py", "instructions": "stream"},
                           {"type": "run_command", "command": "pytest tests/test_export.py"},
                           {"type": "create_pr"}]}},
]


def test_agent_is_asked_for_a_runbook_and_it_is_saved_on_the_attempt(
        monkeypatch, fake_infra, project, incident, builtin):
    playbook_run = PlaybookRun.objects.create(incident_run=incident, playbook=builtin,
                                              execution_mode="draft_only")
    llm = FakeLLM(monkeypatch, *AGENT_TURNS)
    assert PlaybookExecutor(playbook_run, 1, "").execute().outcome == "succeeded"
    assert '"runbook"' in llm.prompts[0][0]
    draft = playbook_run.attempts.get().runbook_draft
    assert draft["title"] == "Stream the report export"
    assert [s["type"] for s in draft["steps"]] == ["edit_file", "run_command"]


def test_agent_following_a_runbook_gets_it_and_isnt_asked_for_another(
        monkeypatch, fake_infra, project, incident, builtin):
    runbook = _runbook(project, builtin)
    playbook_run = PlaybookRun.objects.create(incident_run=incident, playbook=builtin,
                                              runbook=runbook, execution_mode="draft_only")
    llm = FakeLLM(monkeypatch, *AGENT_TURNS)
    PlaybookExecutor(playbook_run, 1, "").execute()
    system, messages = llm.prompts[0]
    assert '"runbook"' not in system
    assert "reports/export.py" in messages[0]["content"]


# ---- outcomes -------------------------------------------------------------------------------

def _finished_run(incident, playbook, status, runbook=None, approved=False, draft=None):
    from django.utils import timezone

    playbook_run = PlaybookRun.objects.create(
        incident_run=incident, playbook=playbook, runbook=runbook, execution_mode="draft_only",
        status=status, approved_at=timezone.now() if approved else None,
    )
    PlaybookExecutionAttempt.objects.create(
        playbook_run=playbook_run, attempt_number=1,
        outcome="succeeded" if status == "succeeded" else "failed", summary="streamed it",
        runbook_draft=draft or {}, generated_steps=[{"type": "run_command", "command": "pytest"}],
    )
    return playbook_run


def test_a_successful_fix_from_a_playbook_saves_an_unconfirmed_runbook(project, incident, builtin):
    incident.telemetry = {"service_name": "shop-api"}
    incident.classification = {"keywords": ["Timeout"]}
    incident.save()
    draft = {"title": "Stream the export", "area": "reports",
             "steps": [{"type": "edit_file", "path": "reports/export.py", "instructions": "s"}]}
    playbook_run = _finished_run(incident, builtin, "succeeded", approved=True, draft=draft)
    activities.record_playbook_outcome(playbook_run.id)
    activities.record_playbook_outcome(playbook_run.id)  # a retry saves nothing new
    runbook = Runbook.objects.get(source_playbook_run=playbook_run)
    assert runbook.status == "unconfirmed" and runbook.playbook == builtin
    assert (runbook.title, runbook.area, runbook.service_name) == ("Stream the export", "reports",
                                                                   "shop-api")
    assert (runbook.repo_owner, runbook.repo_name) == ("acme", "shop")
    assert runbook.keywords == ["timeout"]


def test_without_a_draft_the_runbook_comes_from_what_the_agent_did(project, incident, builtin):
    playbook_run = _finished_run(incident, builtin, "succeeded")
    activities.record_playbook_outcome(playbook_run.id)
    runbook = Runbook.objects.get(source_playbook_run=playbook_run)
    assert runbook.steps == [{"type": "run_command", "command": "pytest"}]


def test_a_rejected_or_failed_run_saves_no_runbook(project, incident, builtin):
    for status in ("rejected", "failed"):
        PlaybookRun.objects.filter(incident_run=incident).delete()
        activities.record_playbook_outcome(_finished_run(incident, builtin, status).id)
    assert not Runbook.objects.exists()


def test_an_approved_reuse_confirms_the_runbook(project, incident, builtin):
    runbook = _runbook(project, builtin)
    playbook_run = _finished_run(incident, builtin, "succeeded", runbook=runbook, approved=True)
    activities.record_playbook_outcome(playbook_run.id)
    runbook.refresh_from_db()
    assert runbook.status == "confirmed"
    assert Runbook.objects.count() == 1  # following a runbook doesn't save another


def test_failures_count_against_the_runbook_and_never_the_builtin(project, builtin):
    from sre.models import IncidentRun

    runbook = _runbook(project, builtin)
    for i in range(3):
        run = IncidentRun.objects.create(project=project, trace_id=f"t{i}", temporal_workflow_id=f"w{i}")
        activities.record_playbook_outcome(_finished_run(run, builtin, "failed", runbook=runbook).id)
    for i in range(3, 6):
        run = IncidentRun.objects.create(project=project, trace_id=f"t{i}", temporal_workflow_id=f"w{i}")
        activities.record_playbook_outcome(_finished_run(run, builtin, "failed").id)
    runbook.refresh_from_db()
    builtin.refresh_from_db()
    assert runbook.status == "failing" and runbook.consecutive_failure_count == 3
    assert builtin.status == "confirmed" and builtin.consecutive_failure_count == 0


def test_an_org_playbook_still_goes_failing(project, builtin):
    from sre.models import IncidentRun

    org_playbook = Playbook.objects.create(organization=project.organization, project=project,
                                           is_generic=True, title="org", status="confirmed")
    for i in range(3):
        run = IncidentRun.objects.create(project=project, trace_id=f"t{i}", temporal_workflow_id=f"w{i}")
        activities.record_playbook_outcome(_finished_run(run, org_playbook, "failed").id)
    org_playbook.refresh_from_db()
    assert org_playbook.status == "failing"
