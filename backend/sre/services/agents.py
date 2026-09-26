"""Starting remediation agents' scan runs: by hand, and from the GitHub App's merge and push
events (see docs/MESH_AND_REMEDIATION.md)."""

import fnmatch
import logging

from django.db.models import Q

from .. import temporal_client
from ..models import AgentTrigger, Project, RemediationAgent, ScanRun, ScanTrigger
from .scanning import create_scan_run

logger = logging.getLogger(__name__)

NULL_SHA = "0" * 40


class ScanStartFailed(Exception):
    pass


def launch(agent: RemediationAgent, trigger: str, trigger_ref: str, workflow_id: str,
           repos=None) -> ScanRun | None:
    """Creates the scan run (committed, so the worker can see it) and starts its workflow.
    None if a run with this workflow id exists already (a redelivered webhook). If Temporal
    can't be reached the run is removed again, so a retry or redelivery starts afresh, and
    ScanStartFailed is raised."""
    if ScanRun.objects.filter(temporal_workflow_id=workflow_id).exists():
        return None
    scan_run = create_scan_run(agent, trigger, trigger_ref, workflow_id, repos)
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
