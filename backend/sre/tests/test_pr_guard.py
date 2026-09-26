"""One agent PR per bug per repo, and a few per repo from remediation agents
(services/pr_guard.py, applied in create_playbook_run)."""

import pytest

from sre import activities
from sre.models import IncidentRun, Playbook, PlaybookRun
from sre.temporal_types import IncidentInput, RunInput

pytestmark = pytest.mark.django_db

SOURCE = "uptrace.buggly.dev/3"


@pytest.fixture(autouse=True)
def generic_playbooks(settings):
    settings.SRE_RUNBOOKS_ENABLED = True  # as in prod: built-in playbooks shared by every repo


@pytest.fixture
def owner(make_user):
    return make_user()


def _project(make_project, owner, name, **kwargs):
    """Every project here uses the same repo, acme/shop, like two projects on one repo."""
    kwargs.setdefault("default_execution_mode", "draft_only")
    return make_project(owner, name=name, uptrace_source_id=SOURCE, **kwargs)


def _incident(project, trace_id, source="alert", **fields):
    return IncidentRun.objects.create(project=project, trace_id=trace_id, source=source,
                                      temporal_workflow_id=f"w-{project.id}-{trace_id}", **fields)


def _playbook(title="Missing key"):
    return Playbook.objects.create(title=title, status="confirmed", is_generic=True,
                                   origin=Playbook.Origin.BUILTIN)


def _start(run, playbook):
    info = activities.create_playbook_run(RunInput(run.id, playbook.id))
    return PlaybookRun.objects.get(id=info.playbook_run_id)


def test_one_alert_reaching_two_projects_on_a_repo_opens_one_pr(make_project, owner):
    playbook = _playbook()
    first = _start(_incident(_project(make_project, owner, "test"), "uptrace-alert-35"), playbook)
    first.pr_url = "https://github.com/acme/shop/pull/23"
    first.status = PlaybookRun.Status.PENDING_APPROVAL
    first.save()

    second_incident = _incident(_project(make_project, owner, "sales order"), "uptrace-alert-35")
    second = _start(second_incident, playbook)
    assert second.execution_mode == "advisory_only" and second.covered_by == first
    assert second.mode_note == (f"Diagnosis only: incident #{first.incident_run_id} is already "
                                "fixing this in acme/shop")

    # A reopened alert (-r2) is the same bug while the first fix is still open.
    reopened = _start(_incident(second_incident.project, "uptrace-alert-35-r2"), playbook)
    assert reopened.covered_by == first


def test_same_playbook_in_the_same_file_is_the_same_fix(make_project, owner):
    project = _project(make_project, owner, "p")
    playbook = _playbook()
    first = _start(_incident(project, "a1", classification={"suspected_files": ["app/views.py"]}), playbook)
    same_file = _start(_incident(project, "a2", telemetry={"location": "app/views.py:40"}), playbook)
    other_file = _start(_incident(project, "a3", telemetry={"location": "app/models.py:3"}), playbook)
    other_playbook = _start(_incident(project, "a4", telemetry={"location": "app/views.py:9"}),
                            _playbook("Timeout"))
    assert same_file.covered_by == first and same_file.execution_mode == "advisory_only"
    assert other_file.execution_mode == other_playbook.execution_mode == "draft_only"


def test_a_finished_or_diagnosis_only_run_covers_nothing(make_project, owner):
    project = _project(make_project, owner, "p")
    playbook = _playbook()
    done = _start(_incident(project, "a1", telemetry={"location": "app/views.py:1"}), playbook)
    done.status = PlaybookRun.Status.REJECTED
    done.save()
    report_only = _start(_incident(_project(make_project, owner, "q", default_execution_mode="advisory_only"),
                                   "a2", telemetry={"location": "app/views.py:1"}), playbook)
    assert report_only.execution_mode == "advisory_only" and report_only.mode_note == ""
    again = _start(_incident(project, "a3", telemetry={"location": "app/views.py:1"}), playbook)
    assert again.execution_mode == "draft_only" and again.covered_by is None


def test_agent_findings_stop_at_the_repos_open_pr_limit(make_project, owner, settings):
    settings.SRE_AGENT_MAX_OPEN_PRS_PER_REPO = 2
    project = _project(make_project, owner, "p")
    playbook = _playbook()

    def finding(n):
        return _start(_incident(project, f"scan-sweep-{n}", source="scan", execution_mode_cap="draft_only",
                                telemetry={"location": f"app/f{n}.py:1"}), playbook)

    first, second, third = finding(1), finding(2), finding(3)
    assert [r.execution_mode for r in (first, second, third)] == ["draft_only", "draft_only", "advisory_only"]
    assert third.mode_note == "Diagnosis only: acme/shop already has 2 open PRs from remediation agents"
    assert third.covered_by is None
    # Production alerts aren't limited: they're real errors.
    alert = _start(_incident(project, "a1", telemetry={"location": "app/other.py:1"}), playbook)
    assert alert.execution_mode == "draft_only"
    # Merging (or closing) one frees a slot.
    PlaybookRun.objects.filter(id=first.id).update(status=PlaybookRun.Status.SUCCEEDED)
    assert finding(4).execution_mode == "draft_only"


def test_a_held_back_report_says_why_first(make_project, owner, monkeypatch):
    playbook = _playbook()
    first = _start(_incident(_project(make_project, owner, "a"), "uptrace-alert-9"), playbook)
    PlaybookRun.objects.filter(id=first.id).update(pr_url="https://github.com/acme/shop/pull/7")
    held = _incident(_project(make_project, owner, "b"), "uptrace-alert-9", matched_playbook=playbook)
    _start(held, playbook)

    class Reporter:
        def __init__(self, *args):
            pass

        def write(self):
            return "## Summary"

    monkeypatch.setattr(activities, "DiagnosisReporter", Reporter)
    activities.write_diagnosis_report(IncidentInput(held.id, held.project_id))
    held.refresh_from_db()
    assert held.diagnosis_report == (
        f"> Diagnosis only: incident #{first.incident_run_id} is already fixing this in "
        "acme/shop (https://github.com/acme/shop/pull/7).\n\n## Summary")


def test_the_incident_api_shows_the_covering_pr(make_project, owner, api_for):
    playbook = _playbook()
    first = _start(_incident(_project(make_project, owner, "a"), "uptrace-alert-9"), playbook)
    PlaybookRun.objects.filter(id=first.id).update(pr_url="https://github.com/acme/shop/pull/7")
    held = _incident(_project(make_project, owner, "b"), "uptrace-alert-9")
    _start(held, playbook)
    body = api_for(owner).get(f"/incident-runs/{held.id}").json()
    assert body["mode_note"].startswith("Diagnosis only: incident #")
    assert (body["covered_by_incident_run_id"], body["covered_by_pr_url"]) == (
        first.incident_run_id, "https://github.com/acme/shop/pull/7")
    first_body = api_for(owner).get(f"/incident-runs/{first.incident_run_id}").json()
    assert (first_body["mode_note"], first_body["covered_by_pr_url"]) == ("", "")
