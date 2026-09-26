"""Temporal activities: thin wrappers that load rows, call a service class, and persist.
Each LLM-calling activity gets its own Langfuse trace."""

import functools
from contextlib import contextmanager
from dataclasses import asdict

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import close_old_connections, transaction
from django.utils import timezone
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .llm.clients import LLMError
from .llm.resolve import NoLLMConfigError
from .llm.usage import usage_scope
from .models import ExecutionMode, IncidentRun, Playbook, PlaybookRun, Project, Runbook
from .services.executor import PlaybookExecutor, pr_title_body
from .services.github import GitHubRepo
from .services import mesh
from .services import runbooks as runbook_outcomes
from .services.knowledge import GenericPlaybookAuthor, KnowledgeJudge, KnowledgeSearch
from .services.playbooks import DiagnosisReporter, PlaybookAuthor, PlaybookJudge, PlaybookSearch, visible_playbooks
from .services.triage import AnomalyChecker, BugClassifier
from .services.uptrace import fetch_telemetry
from .temporal_types import (
    FAILING_THRESHOLD,
    AnomalyResult,
    AttemptInput,
    AttemptResult,
    Candidates,
    Classification,
    GraphRefreshInput,
    GraphTarget,
    IncidentInput,
    JudgeInput,
    JudgeResult,
    PlaybookRunInfo,
    PlaybookRunStatus,
    RootCauseResult,
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


@contextmanager
def llm_step(step: str, run: IncidentRun):
    """Langfuse trace + DB usage recording for one LLM-calling activity."""
    with trace_step(step, project_id=run.project_id, incident_run_id=run.id) as span:
        with usage_scope(run.id, step):
            yield span


def _incident(incident_run_id: int) -> IncidentRun:
    return IncidentRun.objects.select_related("project", "project__default_llm_config").get(
        id=incident_run_id
    )


@django_activity
def fetch_incident_telemetry(inp: IncidentInput) -> None:
    """Stores the alert's exception from Uptrace on the run (or {}); never raises for a
    failed fetch, so it can't fail the incident."""
    run = _incident(inp.incident_run_id)
    if run.source != IncidentRun.Source.ALERT:
        return  # linked/scan incidents are created with their telemetry; there's no alert
    run.telemetry = fetch_telemetry(run)
    run.save(update_fields=["telemetry", "updated_at"])


def mesh_enabled() -> bool:
    return settings.SRE_SERVICE_MESH_ENABLED and settings.SRE_UPTRACE_FETCH_ENABLED


@django_activity
def localize_root_cause(inp: IncidentInput) -> RootCauseResult:
    """Service mesh: walks the incident's trace (no LLM). Never fails the incident: with
    the mesh off, no trace or an Uptrace error, the incident just carries on here."""
    if not mesh_enabled():
        return RootCauseResult()
    child = mesh.localize(_incident(inp.incident_run_id))
    if child is None:
        return RootCauseResult()
    return RootCauseResult(child.id, child.project_id, child.temporal_workflow_id)


@django_activity
def list_graph_targets(inp: GraphRefreshInput) -> list[GraphTarget]:
    if not mesh_enabled():
        return []
    targets = mesh.refresh_targets(inp.organization_id or None)
    return [GraphTarget(org, source) for org, source in targets if not inp.source or source == inp.source]


@django_activity
def refresh_service_graph(target: GraphTarget) -> bool:
    return mesh.refresh_graph(target.organization_id, target.source)


@django_activity
def confirm_anomaly(inp: IncidentInput) -> AnomalyResult:
    run = _incident(inp.incident_run_id)
    with llm_step("anomaly_double_check", run):
        result = AnomalyChecker(run).check()
    run.classification = {**(run.classification or {}), "anomaly": asdict(result)}
    run.save(update_fields=["classification", "updated_at"])
    return result


@django_activity
def classify_bug(inp: IncidentInput) -> Classification:
    run = _incident(inp.incident_run_id)
    with llm_step("bug_classification", run):
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
    with llm_step("playbook_similarity_judge", run):
        result = PlaybookJudge(run).judge(inp.candidate_ids)
    if result.matched_playbook_id is not None:
        run.matched_playbook_id = result.matched_playbook_id
        run.save(update_fields=["matched_playbook", "updated_at"])
    return result


@django_activity
def find_candidates(inp: SearchInput) -> Candidates:
    """Runbooks off: the old playbook search, no runbooks. On: runbooks and playbooks."""
    project = Project.objects.get(id=inp.project_id)
    if not settings.SRE_RUNBOOKS_ENABLED:
        return Candidates(playbook_ids=PlaybookSearch(project).top_k(inp.keywords))
    run = IncidentRun.objects.filter(id=inp.incident_run_id).first()
    service = str(((run.telemetry if run else None) or {}).get("service_name") or "")
    return KnowledgeSearch(project).find(inp.keywords, inp.category, service)


@django_activity
def judge_match(inp: JudgeInput) -> JudgeResult:
    run = _incident(inp.incident_run_id)
    with llm_step("playbook_similarity_judge", run):
        if settings.SRE_RUNBOOKS_ENABLED:
            result = KnowledgeJudge(run).judge(inp.candidate_ids, inp.runbook_ids)
        else:
            result = PlaybookJudge(run).judge(inp.candidate_ids)
    if result.matched_playbook_id is not None:
        run.matched_playbook_id = result.matched_playbook_id
        run.matched_runbook_id = result.matched_runbook_id
        run.save(update_fields=["matched_playbook", "matched_runbook", "updated_at"])
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
    author = GenericPlaybookAuthor if settings.SRE_RUNBOOKS_ENABLED else PlaybookAuthor
    with llm_step("playbook_creation", run):
        playbook = author(run).create(classification)
    return playbook.id


@django_activity
def create_playbook_run(inp: RunInput) -> PlaybookRunInfo:
    run = _incident(inp.incident_run_id)
    runbook = None
    if settings.SRE_RUNBOOKS_ENABLED:
        playbook = visible_playbooks(run.project).get(id=inp.playbook_id)
        if inp.runbook_id is not None:
            runbook = Runbook.objects.get(id=inp.runbook_id, project=run.project,
                                          playbook_id=playbook.id)
    else:
        playbook = Playbook.objects.get(id=inp.playbook_id, project=run.project)
    mode = playbook.execution_mode_override or run.project.default_execution_mode
    if settings.SRE_RUNBOOKS_ENABLED:
        # Unattended only with a runbook a person has confirmed: a playbook alone (even a
        # confirmed built-in) means a fix this repo has never seen.
        if mode == ExecutionMode.AUTONOMOUS and (
                runbook is None or runbook.status != Playbook.Status.CONFIRMED):
            mode = ExecutionMode.DRAFT_ONLY
    # A never-reviewed playbook must not run unattended, whatever the project allows.
    elif playbook.status == Playbook.Status.UNCONFIRMED and mode == ExecutionMode.AUTONOMOUS:
        mode = ExecutionMode.DRAFT_ONLY
    playbook_run, _ = PlaybookRun.objects.get_or_create(
        incident_run=run,
        defaults={"playbook": playbook, "runbook": runbook, "execution_mode": mode,
                  "generate_tests": run.project.generate_tests},
    )
    return PlaybookRunInfo(playbook_run.id, playbook_run.execution_mode)


@django_activity
def run_playbook_attempt(inp: AttemptInput) -> AttemptResult:
    playbook_run = PlaybookRun.objects.select_related(
        "incident_run__project", "playbook"
    ).get(id=inp.playbook_run_id)
    run = playbook_run.incident_run
    with llm_step("playbook_execution", run) as span:
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
    with llm_step("diagnosis_report", run):
        report = DiagnosisReporter(run, run.matched_playbook, run.matched_runbook).write()
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
    if settings.SRE_RUNBOOKS_ENABLED:
        runbook_outcomes.record_outcome(playbook_run)
        return
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
    fetch_incident_telemetry,
    localize_root_cause,
    list_graph_targets,
    refresh_service_graph,
    confirm_anomaly,
    classify_bug,
    find_candidate_playbooks,
    judge_playbook_match,
    find_candidates,
    judge_match,
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
