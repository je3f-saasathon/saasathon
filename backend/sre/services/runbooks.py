"""After a run (SRE_RUNBOOKS_ENABLED): save the runbook a successful fix produced, and keep
failure streaks and confirmation on the thing the run actually followed."""

from django.db import transaction

from ..models import Playbook, PlaybookExecutionAttempt, PlaybookRun, Runbook
from ..temporal_types import FAILING_THRESHOLD
from .playbooks import clean_steps

FINISHED = [PlaybookRun.Status.SUCCEEDED, PlaybookRun.Status.FAILED]


def draft_from_attempt(attempt: PlaybookExecutionAttempt, playbook_title: str) -> dict:
    """The agent's proposed runbook, or one rebuilt from what it actually did."""
    draft = attempt.runbook_draft or {}
    steps = clean_steps(draft.get("steps"))
    if not steps:
        steps = clean_steps([
            {**s, "instructions": s.get("instructions") or attempt.summary[:1000]}
            for s in attempt.generated_steps
        ])
    return {
        "title": str(draft.get("title") or playbook_title)[:255],
        "area": str(draft.get("area") or "")[:255],
        "description": str(draft.get("description") or attempt.summary)[:5000],
        "steps": steps,
    }


def save_runbook_from_run(playbook_run: PlaybookRun) -> Runbook | None:
    """A successful fix that followed a playbook alone becomes this project's runbook.
    Idempotent (one per run), and never from a failed or rejected run."""
    if playbook_run.status != PlaybookRun.Status.SUCCEEDED or playbook_run.runbook_id:
        return None
    attempt = playbook_run.attempts.filter(outcome="succeeded").order_by("-attempt_number").first()
    if attempt is None:
        return None
    draft = draft_from_attempt(attempt, playbook_run.playbook.title)
    if not draft["steps"]:
        return None
    run = playbook_run.incident_run
    project = run.project
    classification = run.classification or {}
    runbook, _ = Runbook.objects.get_or_create(
        source_playbook_run=playbook_run,
        defaults={
            **draft,
            "project": project,
            "playbook": playbook_run.playbook,
            "keywords": [str(k).lower() for k in classification.get("keywords") or []][:20],
            "origin": Runbook.Origin.AGENT,
            "status": Playbook.Status.UNCONFIRMED,
            "repo_owner": project.github_repo_owner,
            "repo_name": project.github_repo_name,
            "service_name": str((run.telemetry or {}).get("service_name") or "")[:255],
        },
    )
    return runbook


def _update_streak(item, runs, playbook_run: PlaybookRun) -> None:
    """Recomputed from run history, so a retried activity can't double-count."""
    streak = 0
    for past in runs.filter(status__in=FINISHED).order_by("-created_at"):
        if past.status != PlaybookRun.Status.FAILED:
            break
        streak += 1
    item.consecutive_failure_count = streak
    human_approved_success = (playbook_run.status == PlaybookRun.Status.SUCCEEDED
                              and playbook_run.approved_at is not None)
    if streak >= FAILING_THRESHOLD:
        item.status = Playbook.Status.FAILING
    elif human_approved_success and item.status == Playbook.Status.UNCONFIRMED:
        item.status = Playbook.Status.CONFIRMED
    item.save(update_fields=["consecutive_failure_count", "status", "updated_at"])


def record_outcome(playbook_run: PlaybookRun) -> None:
    """A run counts toward its runbook when it used one, else toward its playbook, except a
    built-in: that row is shared by every org, so no one tenant's runs may change it."""
    with transaction.atomic():
        if playbook_run.runbook_id:
            runbook = Runbook.objects.select_for_update().get(id=playbook_run.runbook_id)
            _update_streak(runbook, runbook.runs, playbook_run)
        else:
            playbook = Playbook.objects.select_for_update().get(id=playbook_run.playbook_id)
            if playbook.origin != Playbook.Origin.BUILTIN:
                _update_streak(playbook, playbook.runs.filter(runbook__isnull=True), playbook_run)
        save_runbook_from_run(playbook_run)
