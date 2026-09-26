"""Keeps a repo from getting a second agent PR for a bug an open one already fixes (the
same alert reaching two projects on one repo, or a scan finding it again), and from
piling up more than a few PRs from remediation agents. Both hold the new run to a
diagnosis, with a note saying why."""

import re

from django.conf import settings

from ..models import ExecutionMode, IncidentRun, PlaybookRun, Project

ACTIVE = [PlaybookRun.Status.RUNNING, PlaybookRun.Status.PENDING_APPROVAL]


def lock_repo(project: Project) -> None:
    """Row locks on every project using the project's repo (call inside a transaction), so
    runs for the same repo decide one at a time: alerts reaching several projects land in
    the same second."""
    list(Project.objects.select_for_update().filter(
        github_repo_owner__iexact=project.github_repo_owner,
        github_repo_name__iexact=project.github_repo_name,
    ).order_by("id").values_list("id", flat=True))


def open_pr_runs(project: Project):
    """Runs in the project's repo (any project using it) that have, or are writing, a PR."""
    return PlaybookRun.objects.filter(
        incident_run__project__github_repo_owner__iexact=project.github_repo_owner,
        incident_run__project__github_repo_name__iexact=project.github_repo_name,
        status__in=ACTIVE,
    ).exclude(execution_mode=ExecutionMode.ADVISORY_ONLY)


def bug_key(run: IncidentRun) -> str:
    """What makes two incidents the same report: the alert (without its -rN reopen suffix,
    and only within one Uptrace project), or the scan finding's fingerprint."""
    if run.source == IncidentRun.Source.SCAN:
        return run.trace_id
    if run.trace_id.startswith("uptrace-alert-") and run.project.uptrace_source_id:
        return f"{run.project.uptrace_source_id}|{re.sub(r'-r[0-9]+$', '', run.trace_id)}"
    return ""


def bug_file(run: IncidentRun) -> str:
    """The file the bug is in, when the incident says: the scan finding's (or fetched
    exception's) location, else the classifier's first suspected file."""
    location = str((run.telemetry or {}).get("location") or "")
    if location:
        return location.split(":", 1)[0].strip().lstrip("/")
    suspected = (run.classification or {}).get("suspected_files") or []
    return str(suspected[0]).split(":", 1)[0].strip().lstrip("/") if suspected else ""


def covering_run(run: IncidentRun, playbook_id: int) -> PlaybookRun | None:
    """An open run in the same repo that fixes the same bug: the same report, or the same
    playbook in the same file."""
    key, path = bug_key(run), bug_file(run)
    others = open_pr_runs(run.project).exclude(incident_run=run).select_related("incident_run__project")
    for other in others.order_by("id"):
        if key and bug_key(other.incident_run) == key:
            return other
        if path and other.playbook_id == playbook_id and bug_file(other.incident_run) == path:
            return other
    return None


def hold_back(run: IncidentRun, playbook_id: int) -> tuple[str, PlaybookRun | None]:
    """(note, covering run) when a run that could open a PR should stop at a diagnosis;
    ("", None) when it can go ahead. Call with lock_repo held."""
    repo = f"{run.project.github_repo_owner}/{run.project.github_repo_name}"
    other = covering_run(run, playbook_id)
    if other is not None:
        return (f"Diagnosis only: incident #{other.incident_run_id} is already fixing this "
                f"in {repo}"), other
    if run.source == IncidentRun.Source.SCAN:
        limit = settings.SRE_AGENT_MAX_OPEN_PRS_PER_REPO
        count = open_pr_runs(run.project).filter(incident_run__source=IncidentRun.Source.SCAN).count()
        if count >= limit:
            return (f"Diagnosis only: {repo} already has {count} open PRs from remediation "
                    "agents"), None
    return "", None
