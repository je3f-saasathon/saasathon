"""Deleting a project: stop what is still running for it in Temporal, then delete it.

Temporal first, database second: a workflow left running after its rows are gone would
keep calling the LLM (and a fix attempt could still push a branch) with nothing to record
it on. If Temporal can't be reached, nothing is deleted (TemporalUnavailable), and a retry
is safe: cancelling an already cancelled or finished workflow is a no-op.

What happens to each piece (also in docs/CONTRACTS.md, "Delete a project"):
- Incidents that haven't finished (running, awaiting approval, and rejected ones, whose
  workflow listens for a reopened PR for 30 days) have their workflows cancelled. All the
  project's incidents, playbook runs, attempts, LLM usage rows and runbooks are deleted
  with it (cascade). Draft or open PRs on GitHub are left alone: they are in the user's
  repo, and merging or closing one later is simply ignored by the GitHub webhook.
- Linked incidents: this project's incidents that delegated to another project leave that
  child running (it fixes the other project's code) with its parent link cleared; this
  project's own children of another project's incident are cancelled and deleted.
- A running scan run whose only repo is this project is cancelled and marked failed. A run
  covering other projects keeps going: this project's ScanRepo is deleted and the run
  finishes over the rest (the scan activity skips a repo that has gone).
- Agents: the project leaves every agent's explicit project list. An agent that listed only
  this project is disabled (and its schedule removed), because an empty list means "every
  project in the org" and it would otherwise start scanning all of them.
- Playbooks: generic (org) playbooks stay, only their `project` provenance is cleared.
  Legacy project-only playbooks are deleted: nothing else could see them any more.
"""

import logging

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from .. import temporal_client
from ..models import (
    AgentTrigger, IncidentRun, Playbook, Project, RemediationAgent, ScanRepo, ScanRun,
)

logger = logging.getLogger(__name__)

# Statuses whose workflow may still be running or waiting. REJECTED is included: the
# workflow keeps listening for a reopened PR for REOPEN_WINDOW (30 days).
UNFINISHED_INCIDENT_STATUSES = [
    IncidentRun.Status.RUNNING, IncidentRun.Status.AWAITING_APPROVAL, IncidentRun.Status.REJECTED,
]


class TemporalUnavailable(Exception):
    pass


def _scan_runs_to_cancel(project: Project) -> list[ScanRun]:
    """Running scan runs whose only repo is this project."""
    ids = ScanRepo.objects.filter(project=project, scan_run__status=ScanRun.Status.RUNNING) \
        .values_list("scan_run_id", flat=True)
    return list(ScanRun.objects.filter(id__in=ids).annotate(n=Count("repos")).filter(n=1))


def _agents_to_disable(project: Project) -> list[RemediationAgent]:
    """Agents whose explicit project list is just this project."""
    # Annotate before filtering on the relation, or the count would see only this project.
    return list(RemediationAgent.objects.annotate(n=Count("projects"))
                .filter(projects=project, enabled=True, n=1))


def delete_project(project: Project) -> dict:
    """Stops the project's workflows, then deletes it. Returns what was stopped."""
    incident_workflows = list(project.incident_runs.filter(status__in=UNFINISHED_INCIDENT_STATUSES)
                              .values_list("temporal_workflow_id", flat=True))
    scan_runs = _scan_runs_to_cancel(project)
    agents = _agents_to_disable(project)
    schedules = [a.id for a in agents if a.trigger == AgentTrigger.SCHEDULE]
    workflow_ids = incident_workflows + [s.temporal_workflow_id for s in scan_runs]
    if workflow_ids or schedules:
        try:
            temporal_client.stop_project_work(workflow_ids, schedules,
                                              reason=f"project {project.id} deleted")
        except Exception as exc:
            logger.exception("could not stop project %s's workflows", project.id)
            raise TemporalUnavailable(str(exc)) from exc

    legacy_playbooks = list(Playbook.objects.filter(project=project, is_generic=False)
                            .exclude(origin=Playbook.Origin.BUILTIN).values_list("id", flat=True))
    with transaction.atomic():
        ScanRun.objects.filter(id__in=[s.id for s in scan_runs], status=ScanRun.Status.RUNNING).update(
            status=ScanRun.Status.FAILED, finished_at=timezone.now(),
            error_message="Cancelled: its only project was deleted")
        RemediationAgent.objects.filter(id__in=[a.id for a in agents]).update(
            enabled=False, updated_at=timezone.now())
        project.delete()
        # Anything still using one (a run or runbook elsewhere) keeps it.
        Playbook.objects.filter(id__in=legacy_playbooks, runs__isnull=True, runbooks__isnull=True) \
            .delete()
    return {"cancelled_incidents": len(incident_workflows), "cancelled_scan_runs": len(scan_runs),
            "disabled_agents": len(agents)}
