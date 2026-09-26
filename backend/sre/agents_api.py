"""Remediation agents: /sre/organizations/{id}/agents, /sre/agents/*, /sre/scan-runs/*.
Registered on the SRE router (imported at the bottom of api.py). See docs/CONTRACTS.md."""

import re
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404
from ninja.errors import HttpError

from . import temporal_client
from .api import _usage_out, router
from .models import (
    AgentKind, AgentTrigger, ExecutionMode, IncidentRun, LLMUsage, OrgRole, Playbook, Project,
    RemediationAgent, ScanRun, ScanTrigger,
)
from .permissions import get_org_membership
from .schemas import AgentIn, AgentOut, AgentUpdateIn, ScanRunListOut, ScanRunOut
from .services import agents as agent_service
from .services.scanning import agent_tokens_this_month

# Five fields of digits, *, /, -, ',' (Temporal validates the rest).
_CRON_FIELD = re.compile(r"^[\d*/,\-]+$")
MAX_FINDINGS = 10


def _enabled() -> None:
    if not settings.SRE_REMEDIATION_AGENTS_ENABLED:
        raise Http404


def _agent_for(user, agent_id: int, role: str) -> RemediationAgent:
    _enabled()
    agent = get_object_or_404(RemediationAgent, id=agent_id)
    get_org_membership(user, agent.organization_id, role)
    return agent


def _agent_out(agent: RemediationAgent) -> dict:
    last = agent.scan_runs.order_by("-started_at").values_list("id", flat=True).first()
    return {
        **{f: getattr(agent, f) for f in AgentOut.model_fields
           if f not in ("project_ids", "playbook_ids", "tokens_this_month", "last_scan_run_id")},
        "project_ids": list(agent.projects.values_list("id", flat=True)),
        "playbook_ids": list(agent.playbooks.values_list("id", flat=True)),
        "tokens_this_month": agent_tokens_this_month(agent),
        "last_scan_run_id": last,
    }


def _scan_run_out(scan_run: ScanRun) -> dict:
    incidents: dict[int, list[int]] = {}
    for run_id, project_id in IncidentRun.objects.filter(scan_run=scan_run).values_list("id", "project_id"):
        incidents.setdefault(project_id, []).append(run_id)
    repos = [{"project_id": r.project_id, "project_name": r.project.name, "status": r.status,
              "finding_count": r.finding_count, "incident_run_ids": incidents.get(r.project_id, []),
              "error": r.error} for r in scan_run.repos.select_related("project")]
    return {
        **{f: getattr(scan_run, f) for f in ("id", "agent_id", "trigger", "trigger_ref", "status",
                                             "error_message", "started_at", "finished_at")},
        "repos": repos,
        "finding_count": sum(r["finding_count"] for r in repos),
        "usage": _usage_out(LLMUsage.objects.filter(scan_repo__scan_run=scan_run)),
    }


def _validate(agent: RemediationAgent, project_ids, playbook_ids) -> None:
    """Checks the agent as it would be saved. Raises 400."""
    if not agent.name.strip():
        raise HttpError(400, "name is required")
    if agent.execution_mode == ExecutionMode.AUTONOMOUS:
        raise HttpError(400, "A remediation agent can't run autonomous: use advisory_only or draft_only")
    if agent.trigger == AgentTrigger.SCHEDULE:
        fields = agent.schedule_cron.split()
        if len(fields) != 5 or not all(_CRON_FIELD.match(f) for f in fields):
            raise HttpError(400, "schedule_cron must be a 5-field cron expression, e.g. \"0 3 * * *\"")
    if agent.trigger == AgentTrigger.BRANCH_WATCH and not agent.branch_pattern.strip():
        raise HttpError(400, "branch_watch needs a branch_pattern, e.g. \"release/*\"")
    if not 1 <= agent.max_findings_per_repo <= MAX_FINDINGS:
        raise HttpError(400, f"max_findings_per_repo must be 1-{MAX_FINDINGS}")
    if agent.monthly_token_budget < 0:
        raise HttpError(400, "monthly_token_budget can't be negative")
    if project_ids is not None:
        found = Project.objects.filter(id__in=project_ids, organization_id=agent.organization_id).count()
        if found != len(set(project_ids)):
            raise HttpError(400, "Every project must belong to this organization")
    if playbook_ids is not None:
        visible = Playbook.objects.filter(id__in=playbook_ids).filter(
            Q(organization__isnull=True, project__isnull=True)  # built-ins
            | Q(organization_id=agent.organization_id, is_generic=True))
        if visible.count() != len(set(playbook_ids)):
            raise HttpError(400, "Every playbook must be a built-in or one of this organization's")


def _save(agent: RemediationAgent, project_ids, playbook_ids) -> None:
    """Saves the agent and syncs its Temporal Schedule; a sync failure undoes the save."""
    try:
        with transaction.atomic():
            agent.save()
            if project_ids is not None:
                agent.projects.set(project_ids)
            if playbook_ids is not None:
                agent.playbooks.set(playbook_ids)
            temporal_client.sync_agent_schedule(agent)
    except HttpError:
        raise
    except Exception as exc:
        raise HttpError(503, "Could not update the agent's schedule in Temporal; try again") from exc


@router.get("/organizations/{org_id}/agents", response=list[AgentOut])
def list_agents(request: HttpRequest, org_id: int):
    _enabled()
    org = get_org_membership(request.auth, org_id, OrgRole.MEMBER).organization
    return [_agent_out(a) for a in org.remediation_agents.all()]


@router.post("/organizations/{org_id}/agents", response={201: AgentOut, 409: dict})
def create_agent(request: HttpRequest, org_id: int, payload: AgentIn):
    _enabled()
    org = get_org_membership(request.auth, org_id, OrgRole.ADMIN).organization
    data = payload.dict()
    project_ids, playbook_ids = data.pop("project_ids"), data.pop("playbook_ids")
    if data["execution_mode"] is None:
        data["execution_mode"] = (ExecutionMode.ADVISORY_ONLY if data["kind"] == AgentKind.PLAYBOOK_SWEEP
                                  else ExecutionMode.DRAFT_ONLY)
    data["name"] = data["name"].strip()[:100]
    agent = RemediationAgent(organization=org, created_by=request.auth, **data)
    _validate(agent, project_ids, playbook_ids)
    if org.remediation_agents.filter(name=agent.name).exists():
        return 409, {"detail": "An agent with that name already exists"}
    _save(agent, project_ids, playbook_ids)
    return 201, _agent_out(agent)


@router.get("/agents/{agent_id}", response=AgentOut)
def get_agent(request: HttpRequest, agent_id: int):
    return _agent_out(_agent_for(request.auth, agent_id, OrgRole.MEMBER))


@router.patch("/agents/{agent_id}", response={200: AgentOut, 409: dict})
def update_agent(request: HttpRequest, agent_id: int, payload: AgentUpdateIn):
    agent = _agent_for(request.auth, agent_id, OrgRole.ADMIN)
    changes = payload.dict(exclude_unset=True)
    project_ids, playbook_ids = changes.pop("project_ids", None), changes.pop("playbook_ids", None)
    for field, value in changes.items():
        if value is not None:
            setattr(agent, field, value.strip()[:100] if field == "name" else value)
    _validate(agent, project_ids, playbook_ids)
    if agent.organization.remediation_agents.filter(name=agent.name).exclude(id=agent.id).exists():
        return 409, {"detail": "An agent with that name already exists"}
    _save(agent, project_ids, playbook_ids)
    return 200, _agent_out(agent)


@router.delete("/agents/{agent_id}", response={204: None, 503: dict})
def delete_agent(request: HttpRequest, agent_id: int):
    agent = _agent_for(request.auth, agent_id, OrgRole.ADMIN)
    try:
        temporal_client.delete_agent_schedule(agent.id)
    except Exception:
        return 503, {"detail": "Could not remove the agent's schedule in Temporal; try again"}
    agent.delete()
    return 204, None


@router.post("/agents/{agent_id}/run", response={202: ScanRunOut, 409: dict, 503: dict})
def run_agent(request: HttpRequest, agent_id: int):
    agent = _agent_for(request.auth, agent_id, OrgRole.ADMIN)
    if not agent.enabled:
        return 409, {"detail": "This agent is disabled"}
    if agent.scan_runs.filter(status=ScanRun.Status.RUNNING).exists():
        return 409, {"detail": "This agent already has a scan running"}
    try:
        scan_run = agent_service.launch(agent, ScanTrigger.MANUAL, "",
                                        f"sre-scan-{agent.id}-manual-{uuid.uuid4().hex[:12]}")
    except agent_service.ScanStartFailed:
        return 503, {"detail": "Could not start the scan; try again"}
    return 202, _scan_run_out(scan_run)


@router.get("/agents/{agent_id}/scan-runs", response=ScanRunListOut)
def list_scan_runs(request: HttpRequest, agent_id: int, page: int = 1, page_size: int = 20):
    agent = _agent_for(request.auth, agent_id, OrgRole.MEMBER)
    page, page_size = max(page, 1), min(max(page_size, 1), 100)
    runs = agent.scan_runs.all()
    chunk = runs[(page - 1) * page_size: page * page_size]
    return {"scan_runs": [_scan_run_out(r) for r in chunk], "total": runs.count()}


@router.get("/scan-runs/{scan_run_id}", response=ScanRunOut)
def get_scan_run(request: HttpRequest, scan_run_id: int):
    _enabled()
    scan_run = get_object_or_404(ScanRun.objects.select_related("agent"), id=scan_run_id)
    get_org_membership(request.auth, scan_run.agent.organization_id, OrgRole.MEMBER)
    return _scan_run_out(scan_run)
