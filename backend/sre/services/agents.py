"""Starting remediation agents' scan runs: by hand, from the GitHub App's merge and push
events, and when a fix merges (see docs/MESH_AND_REMEDIATION.md)."""

import fnmatch
import logging

from django.db.models import Q

from .. import temporal_client
from ..models import (
    AgentKind, AgentTrigger, IncidentRun, PlaybookRun, Project, RemediationAgent, Runbook, ScanRun,
    ScanTrigger,
)
from .scanning import create_scan_run

logger = logging.getLogger(__name__)

NULL_SHA = "0" * 40


class ScanStartFailed(Exception):
    pass


def launch(agent: RemediationAgent, trigger: str, trigger_ref: str, workflow_id: str,
           repos=None, runbook: Runbook | None = None) -> ScanRun | None:
    """Creates the scan run (committed, so the worker can see it) and starts its workflow.
    None if a run with this workflow id exists already (a redelivered webhook). If Temporal
    can't be reached the run is removed again, so a retry or redelivery starts afresh, and
    ScanStartFailed is raised."""
    if ScanRun.objects.filter(temporal_workflow_id=workflow_id).exists():
        return None
    scan_run = create_scan_run(agent, trigger, trigger_ref, workflow_id, repos, runbook)
    try:
        temporal_client.start_scan(workflow_id, scan_run.id)
    except Exception as exc:
        logger.exception("could not start scan %s", workflow_id)
        scan_run.delete()
        raise ScanStartFailed(workflow_id) from exc
    return scan_run


def _agents_for(project: Project, trigger: str):
    return RemediationAgent.objects.filter(
        Q(projects=project) | Q(projects__isnull=True),
        organization_id=project.organization_id, enabled=True, trigger=trigger,
    ).distinct()


def _projects_for(repository: dict) -> list[Project]:
    owner, _, name = str(repository.get("full_name") or "").partition("/")
    if not owner or not name:
        return []
    return list(Project.objects.filter(github_repo_owner__iexact=owner, github_repo_name__iexact=name)
                .exclude(organization__isnull=True))


def on_merge(payload: dict) -> list[ScanRun]:
    """A PR merged into a project's default branch: every on_merge agent covering that
    project scans the PR's changes (the merge commit against its first parent)."""
    pr = payload.get("pull_request") or {}
    merge_sha = str(pr.get("merge_commit_sha") or "")
    base_ref = str((pr.get("base") or {}).get("ref") or "")
    if not pr.get("merged") or not merge_sha:
        return []
    started = []
    for project in _projects_for(payload.get("repository") or {}):
        if project.github_default_branch != base_ref:
            continue
        for agent in _agents_for(project, AgentTrigger.ON_MERGE):
            run = launch(agent, ScanTrigger.ON_MERGE, merge_sha,
                         f"sre-scan-{agent.id}-{project.id}-merge-{merge_sha[:12]}",
                         [(project, base_ref, f"{merge_sha}^1", merge_sha)])
            if run is not None:
                started.append(run)
    return started


def on_push(payload: dict) -> list[ScanRun]:
    """A push to a branch matching a branch_watch agent's pattern: that agent scans what
    the push changed (a new branch: against the default branch)."""
    ref, before, after = (str(payload.get(k) or "") for k in ("ref", "before", "after"))
    if not ref.startswith("refs/heads/") or payload.get("deleted") or not after or after == NULL_SHA:
        return []
    if not payload.get("commits") and not payload.get("created"):
        return []
    branch = ref.removeprefix("refs/heads/")
    started = []
    for project in _projects_for(payload.get("repository") or {}):
        base = before if before and before != NULL_SHA else project.github_default_branch
        for agent in _agents_for(project, AgentTrigger.BRANCH_WATCH):
            if not fnmatch.fnmatchcase(branch, agent.branch_pattern):
                continue
            run = launch(agent, ScanTrigger.BRANCH_WATCH, after,
                         f"sre-scan-{agent.id}-{project.id}-push-{after[:12]}",
                         [(project, branch, base, after)])
            if run is not None:
                started.append(run)
    return started


def _same_repo(a: Project, b: Project) -> bool:
    return (a.github_repo_owner.lower(), a.github_repo_name.lower()) == (
        b.github_repo_owner.lower(), b.github_repo_name.lower())


def on_fix_merged(playbook_run: PlaybookRun) -> list[ScanRun]:
    """A fix a person accepted (its PR merged, or approved in the app): every runbook_variant
    agent that scans on merge and covers the fixed repo hunts for the same bug in its other
    repos on the same GitHub account, whole repo, with the fix's runbook only. Called after
    the runbook is saved. Once per fix (the workflow id), and not for a fix that was itself
    a variant finding: that hunt already covered the other repos. Best effort: a scan that
    can't start is logged, not raised."""
    run = playbook_run.incident_run
    if playbook_run.status != PlaybookRun.Status.SUCCEEDED or playbook_run.approved_at is None:
        return []
    if run.source == IncidentRun.Source.SCAN and run.scan_kind == AgentKind.RUNBOOK_VARIANT:
        return []
    runbook = playbook_run.runbook or Runbook.objects.filter(source_playbook_run=playbook_run).first()
    if runbook is None:
        return []
    fixed = run.project
    started = []
    for agent in _agents_for(fixed, AgentTrigger.ON_MERGE).filter(kind=AgentKind.RUNBOOK_VARIANT):
        targets = [p for p in agent.covered_projects().order_by("id")
                   if not _same_repo(p, fixed) and p.github_installation_id == fixed.github_installation_id]
        if not targets:
            continue
        try:
            scan_run = launch(agent, ScanTrigger.FIX_MERGED, playbook_run.pr_url or f"runbook {runbook.id}",
                              f"sre-scan-{agent.id}-fix-{playbook_run.id}",
                              [(p, p.github_default_branch, "", "") for p in targets], runbook=runbook)
        except ScanStartFailed:
            continue  # launch logged it
        if scan_run is not None:
            started.append(scan_run)
    return started
