"""Temporal activities: thin wrappers that load rows, call a service class, and persist.
Each LLM-calling activity gets its own Langfuse trace."""

import functools
from dataclasses import asdict

from django.core.exceptions import ImproperlyConfigured
from django.db import close_old_connections, transaction
from django.utils import timezone
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .llm.clients import LLMError
from .llm.resolve import NoLLMConfigError
from .models import ExecutionMode, IncidentRun, Playbook, PlaybookRun, Project
from .services.executor import PlaybookExecutor, pr_title_body
from .services.github import GitHubRepo
from .services.playbooks import DiagnosisReporter, PlaybookAuthor, PlaybookJudge, PlaybookSearch
from .services.triage import AnomalyChecker, BugClassifier
from .temporal_types import (
    FAILING_THRESHOLD,
    AnomalyResult,
    AttemptInput,
    AttemptResult,
    Classification,
    IncidentInput,
    JudgeInput,
    JudgeResult,
    PlaybookRunInfo,
    PlaybookRunStatus,
    RunInput,
    SearchInput,
    StatusUpdate,
)
from .tracing import trace_step


def django_activity(fn):
    """Activities run in a thread pool: give each one fresh DB connections, and turn
    errors a retry can't fix into non-retryable Temporal failures."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        close_old_connections()
        try:
            return fn(*args, **kwargs)
        except (NoLLMConfigError, ImproperlyConfigured) as exc:
            raise ApplicationError(str(exc), type=type(exc).__name__, non_retryable=True) from exc
        except LLMError as exc:
            raise ApplicationError(str(exc), type="LLMError", non_retryable=not exc.retryable) from exc
        finally:
            close_old_connections()

    return activity.defn(name=fn.__name__)(wrapper)


def _incident(incident_run_id: int) -> IncidentRun:
    return IncidentRun.objects.select_related("project", "project__default_llm_config").get(
        id=incident_run_id
    )


@django_activity
def confirm_anomaly(inp: IncidentInput) -> AnomalyResult:
    run = _incident(inp.incident_run_id)
    with trace_step("anomaly_double_check", project_id=run.project_id, incident_run_id=run.id):
        result = AnomalyChecker(run).check()
    run.classification = {**(run.classification or {}), "anomaly": asdict(result)}
    run.save(update_fields=["classification", "updated_at"])
    return result


@django_activity
def classify_bug(inp: IncidentInput) -> Classification:
    run = _incident(inp.incident_run_id)
    with trace_step("bug_classification", project_id=run.project_id, incident_run_id=run.id):
        result = BugClassifier(run).classify()
    run.classification = {**(run.classification or {}), **asdict(result)}
    run.save(update_fields=["classification", "updated_at"])
    return result


@django_activity
def find_candidate_playbooks(inp: SearchInput) -> list[int]:
    return PlaybookSearch(Project.objects.get(id=inp.project_id)).top_k(inp.keywords)


@django_activity
def judge_playbook_match(inp: JudgeInput) -> JudgeResult:
    run = _incident(inp.incident_run_id)
    with trace_step("playbook_similarity_judge", project_id=run.project_id, incident_run_id=run.id):
        result = PlaybookJudge(run).judge(inp.candidate_ids)
    if result.matched_playbook_id is not None:
        run.matched_playbook_id = result.matched_playbook_id
        run.save(update_fields=["matched_playbook", "updated_at"])
    return result


@django_activity
def create_playbook(inp: IncidentInput) -> int:
    run = _incident(inp.incident_run_id)
    data = run.classification or {}
    classification = Classification(
        category=data.get("category", "other"),
        severity=data.get("severity", "medium"),
        summary=data.get("summary", ""),
        keywords=data.get("keywords", []),
        suspected_files=data.get("suspected_files", []),
    )
    with trace_step("playbook_creation", project_id=run.project_id, incident_run_id=run.id):
        playbook = PlaybookAuthor(run).create(classification)
    return playbook.id


@django_activity
def create_playbook_run(inp: RunInput) -> PlaybookRunInfo:
    run = _incident(inp.incident_run_id)
    playbook = Playbook.objects.get(id=inp.playbook_id, project=run.project)
    mode = playbook.execution_mode_override or run.project.default_execution_mode
    # A never-reviewed playbook must not run unattended, whatever the project allows.
    if playbook.status == Playbook.Status.UNCONFIRMED and mode == ExecutionMode.AUTONOMOUS:
        mode = ExecutionMode.DRAFT_ONLY
    playbook_run, _ = PlaybookRun.objects.get_or_create(
        incident_run=run, defaults={"playbook": playbook, "execution_mode": mode}
    )
    return PlaybookRunInfo(playbook_run.id, playbook_run.execution_mode)


@django_activity
def run_playbook_attempt(inp: AttemptInput) -> AttemptResult:
    playbook_run = PlaybookRun.objects.select_related(
        "incident_run__project", "playbook"
    ).get(id=inp.playbook_run_id)
    run = playbook_run.incident_run
    with trace_step("playbook_execution", project_id=run.project_id, incident_run_id=run.id) as span:
        executor = PlaybookExecutor(
            playbook_run, inp.attempt_number, inp.previous_feedback, heartbeat=activity.heartbeat
        )
        result = executor.execute()
        if span.trace_id:
            playbook_run.attempts.filter(attempt_number=inp.attempt_number).update(
                langfuse_trace_id=span.trace_id
            )
    return result


@django_activity
def write_diagnosis_report(inp: IncidentInput) -> None:
    run = _incident(inp.incident_run_id)
    with trace_step("diagnosis_report", project_id=run.project_id, incident_run_id=run.id):
        report = DiagnosisReporter(run, run.matched_playbook).write()
    run.diagnosis_report = report
    run.save(update_fields=["diagnosis_report", "updated_at"])


@django_activity
def open_pull_request(playbook_run_id: int) -> str:
    """Runs on approval: makes the draft PR ready for review, or opens a PR if the
    attempt didn't leave one (e.g. runs from before draft PRs existed)."""
    playbook_run = PlaybookRun.objects.select_related(
        "incident_run__project", "playbook"
    ).get(id=playbook_run_id)
    repo = GitHubRepo(playbook_run.incident_run.project)
    pr_url = repo.mark_pull_request_approved(playbook_run.branch_name)
    if pr_url is None:
        attempt = playbook_run.attempts.filter(outcome="succeeded").order_by("-attempt_number").first()
        summary = attempt.summary if attempt else ""
        pr_url = repo.open_pull_request(playbook_run.branch_name, *pr_title_body(playbook_run, summary))
    playbook_run.pr_url = pr_url
    playbook_run.save(update_fields=["pr_url", "updated_at"])
    return pr_url


@django_activity
def close_pull_request(playbook_run_id: int) -> None:
    playbook_run = PlaybookRun.objects.select_related("incident_run__project").get(id=playbook_run_id)
    GitHubRepo(playbook_run.incident_run.project).close_pull_request(
        playbook_run.branch_name, "Rejected in the SRE agent; closing without merging."
    )


@django_activity
def set_playbook_run_status(inp: PlaybookRunStatus) -> None:
    PlaybookRun.objects.filter(id=inp.playbook_run_id).update(
        status=inp.status, updated_at=timezone.now()
    )


@django_activity
def record_playbook_outcome(playbook_run_id: int) -> None:
    """Recomputes the playbook's streak from run history, so a retry can't double-count."""
    playbook_run = PlaybookRun.objects.select_related("playbook").get(id=playbook_run_id)
    with transaction.atomic():
        playbook = Playbook.objects.select_for_update().get(id=playbook_run.playbook_id)
        finished = playbook.runs.filter(
            status__in=[PlaybookRun.Status.SUCCEEDED, PlaybookRun.Status.FAILED]
        ).order_by("-created_at")
        streak = 0
        for past in finished:
            if past.status != PlaybookRun.Status.FAILED:
                break
            streak += 1
        playbook.consecutive_failure_count = streak
        human_approved_success = (
            playbook_run.status == PlaybookRun.Status.SUCCEEDED
            and playbook_run.approved_at is not None
        )
        if streak >= FAILING_THRESHOLD:
            playbook.status = Playbook.Status.FAILING
        elif human_approved_success and playbook.status == Playbook.Status.UNCONFIRMED:
            playbook.status = Playbook.Status.CONFIRMED
        playbook.save(update_fields=["consecutive_failure_count", "status", "updated_at"])


@django_activity
def mark_incident_status(inp: StatusUpdate) -> None:
    IncidentRun.objects.filter(id=inp.incident_run_id).update(
        status=inp.status, error_message=inp.error_message[:5000], updated_at=timezone.now()
    )


ALL_ACTIVITIES = [
    confirm_anomaly,
    classify_bug,
    find_candidate_playbooks,
    judge_playbook_match,
    create_playbook,
    create_playbook_run,
    run_playbook_attempt,
    write_diagnosis_report,
    open_pull_request,
    close_pull_request,
    set_playbook_run_status,
    record_playbook_outcome,
    mark_incident_status,
]
