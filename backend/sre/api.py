import hashlib
import hmac
import json
import logging
import re
import secrets
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError

from . import temporal_client
from .llm import platform
from .llm.resolve import provider_supports_step
from .models import (
    ORG_ROLE_RANK,
    ROLE_RANK,
    ExecutionMode,
    GitHubInstallation,
    IncidentRun,
    LLMProvider,
    LLMProviderConfig,
    LLMStepOverride,
    Organization,
    OrganizationMembership,
    OrgRole,
    Playbook,
    PlaybookRun,
    Project,
    ProjectMembership,
    ProjectRole,
    Runbook,
    ServiceGraph,
    UptraceCredential,
    UptraceStatus,
)
from .orgs import personal_org
from .permissions import get_membership, get_org_membership, get_project_for, member_project_ids
from .schemas import (
    ApprovePlaybookRunIn,
    GitHubConnectOut,
    GitHubInstallationOut,
    GitHubRepoOut,
    GitHubStatusOut,
    IncidentRunListOut,
    IncidentRunOut,
    LLMConfigIn,
    LLMConfigOut,
    LLMConfigUpdateIn,
    MemberAddIn,
    MemberOut,
    MemberUpdateIn,
    OrganizationIn,
    OrganizationOut,
    OrgMemberAddIn,
    OrgMemberOut,
    OrgMemberUpdateIn,
    PlatformOut,
    PlaybookCreateIn,
    PlaybookListOut,
    PlaybookOut,
    PlaybookRunOut,
    PlaybookUpdateIn,
    CliProjectOut,
    ManagedUptraceOut,
    ProjectCreatedOut,
    ProjectCreateIn,
    ProjectOut,
    ProjectUpdateIn,
    UptraceSetupIn,
    RunbookCreateIn,
    RunbookListOut,
    RunbookOut,
    RunbookUpdateIn,
    ServiceGraphOut,
    StepOverrideOut,
    StepOverridesIn,
    UptraceCredentialIn,
    UptraceCredentialOut,
    UptraceCredentialUpdateIn,
    UptraceWebhookIn,
    UptraceWebhookOut,
    WebhookSecretOut,
)
from .services import auto_projects, github_connect, mesh, project_deletion
from .services.playbooks import clean_playbook_steps, clean_steps, visible_playbooks
from .services.triage import CATEGORIES
from .services import uptrace_admin
from .services.uptrace import resolve_credential
from .temporal_types import ApprovalDecision, IncidentInput, UptraceSyncInput
from .validators import UnsafeURLError, validate_llm_base_url, validate_uptrace_api_url

logger = logging.getLogger(__name__)
router = Router(tags=["sre"])

GITHUB_REPO_FIELDS = {"github_installation_id", "github_repo_owner", "github_repo_name"}
OWNER_ONLY_PROJECT_FIELDS = GITHUB_REPO_FIELDS | {"uptrace_source_id", "organization_id",
                                                  "uptrace_credential_id"}


# ---- helpers ---------------------------------------------------------------

def _github_verified(project: Project) -> bool:
    return GitHubInstallation.objects.filter(
        installation_id=project.github_installation_id,
        user__sre_memberships__project=project,
        user__sre_memberships__role=ProjectRole.OWNER,
    ).exists()


def _project_out(project: Project, role: str) -> dict:
    return {
        **{f: getattr(project, f) for f in ProjectOut.model_fields
           if f not in ("role", "github_verified", "platform_tokens_this_month",
                        "organization_name", "uptrace_fetch_ready", "uptrace_dsn",
                        "uptrace_shared_with")},
        "role": role,
        "uptrace_dsn": (uptrace_admin.dsn_of(project)
                        if ROLE_RANK[role] >= ROLE_RANK[ProjectRole.ADMIN] else ""),
        "uptrace_shared_with": [{"id": p.id, "name": p.name} for p in _uptrace_group(project)
                                if p.id != project.id],
        "organization_name": project.organization.name if project.organization_id else "",
        "uptrace_fetch_ready": resolve_credential(project) is not None,
        "github_verified": _github_verified(project),
        "platform_tokens_this_month": platform.tokens_this_month(project),
    }


# ---- managed Uptrace -------------------------------------------------------------

def _uptrace_group(project: Project) -> list[Project]:
    """The managed projects of the project's org that use its Uptrace project."""
    if not project.uptrace_managed or not project.uptrace_project_id:
        return []
    return list(Project.objects.filter(uptrace_managed=True, organization_id=project.organization_id,
                                       uptrace_project_id=project.uptrace_project_id).order_by("id"))


def _share_target(user, organization_id: int | None, target_id: int, service_names: list[str]) -> Project:
    """An existing managed project to share an Uptrace project with: same org, and the
    caller administers it (its alerts get filtered by service once it's shared)."""
    target = get_project_for(user, target_id, ProjectRole.ADMIN)
    if target.organization_id != organization_id:
        raise HttpError(400, "Only projects in the same organization can share an Uptrace project")
    if not target.uptrace_managed or not target.uptrace_project_id:
        raise HttpError(400, "That project's Uptrace isn't managed by the platform (or isn't set up yet)")
    if not service_names or not target.service_names:
        raise HttpError(400, "Projects sharing an Uptrace project are told apart by service name: "
                             f"set service names on this project and on {target.name!r} first")
    return target


def _start_uptrace_sync(request: HttpRequest, project: Project | None = None,
                        uptrace_project_ids: list[int] = ()) -> None:
    inp = UptraceSyncInput(base_url=request.build_absolute_uri("/").rstrip("/"),
                           project_id=project.id if project else 0,
                           uptrace_project_ids=[i for i in uptrace_project_ids if i])
    if not inp.project_id and not inp.uptrace_project_ids:
        return
    try:
        temporal_client.start_uptrace_sync(inp)
    except Exception:
        logger.exception("could not start the Uptrace sync for %s", inp)
        if project is not None:
            uptrace_admin.mark_error([project.id], "Could not start the Uptrace setup; try again.")


@router.get("/uptrace/managed", response=ManagedUptraceOut)
def managed_uptrace(request: HttpRequest):
    return {"enabled": uptrace_admin.configured(),
            "url": settings.UPTRACE_MANAGED_URL if uptrace_admin.configured() else ""}


@router.get("/cli/project", response={200: CliProjectOut, 400: dict, 404: dict, 409: dict})
def cli_project(request: HttpRequest, repo: str, project_id: int | None = None):
    """What `buggly run` needs to send a repo's telemetry: its project's DSN and service
    name. The DSN is admin-only, like on the project page."""
    owner, _, name = repo.strip().partition("/")
    if not owner or not name:
        return 400, {"detail": "repo must be owner/name"}
    memberships = ProjectMembership.objects.filter(
        user=request.auth, role__in=[ProjectRole.OWNER, ProjectRole.ADMIN],
        project__github_repo_owner__iexact=owner, project__github_repo_name__iexact=name,
    ).select_related("project").order_by("project_id")
    if project_id is not None:
        memberships = memberships.filter(project_id=project_id)
    projects = [m.project for m in memberships]
    if not projects:
        return 404, {"detail": f"You administer no project for {owner}/{name}: install the GitHub "
                               "App on the repo, or create a project for it in Settings"}
    if len(projects) > 1:
        return 409, {"detail": f"Several projects use {owner}/{name}: pass --project",
                     "projects": [{"id": p.id, "name": p.name} for p in projects]}
    project = projects[0]
    dsn = uptrace_admin.dsn_of(project) if project.uptrace_managed else ""
    parts = urlsplit(dsn)
    return 200, {
        "project_id": project.id, "name": project.name,
        "repo": f"{project.github_repo_owner}/{project.github_repo_name}",
        "service_name": (project.service_names or [project.github_repo_name])[0],
        "uptrace_status": project.uptrace_status,
        "dsn": dsn,
        "otlp_endpoint": f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}" if dsn else "",
    }


@router.post("/projects/{project_id}/uptrace/resolve-alerts", response={200: dict, 400: dict, 502: dict})
def resolve_managed_alerts(request: HttpRequest, project_id: int):
    """Resolves the project's open Uptrace alerts: the next occurrence of the error then
    reopens its alert and starts a new incident. Lets testing tools re-run a bug without
    Uptrace access."""
    project = get_project_for(request.auth, project_id, ProjectRole.ADMIN)
    if not uptrace_admin.is_managed(project) or not project.uptrace_project_id:
        return 400, {"detail": "This project's Uptrace isn't managed by the platform"}
    try:
        return 200, {"resolved": uptrace_admin.resolve_open_alerts(project)}
    except uptrace_admin.UptraceAdminError as exc:
        return 502, {"detail": str(exc)}


@router.post("/projects/{project_id}/uptrace/setup", response=ProjectOut)
def setup_managed_uptrace(request: HttpRequest, project_id: int, payload: UptraceSetupIn):
    """Hands the project's Uptrace side to the platform (or retries its setup), with an
    Uptrace project of its own or shared with another project."""
    if not uptrace_admin.configured():
        raise HttpError(400, "Managed Uptrace isn't configured on this server")
    project = get_project_for(request.auth, project_id, ProjectRole.OWNER)
    old = project.uptrace_project_id if project.uptrace_managed else None
    if payload.share_with_project_id is not None:
        if payload.share_with_project_id == project.id:
            raise HttpError(400, "A project can't share with itself")
        target = _share_target(request.auth, project.organization_id,
                               payload.share_with_project_id, project.service_names)
        new = target.uptrace_project_id
    else:
        # Leaving a shared Uptrace project means getting one of its own.
        new = old if old and len(_uptrace_group(project)) == 1 else None
    project.uptrace_managed = True
    project.uptrace_project_id = new
    project.uptrace_source_id = uptrace_admin.source_id(new) if new else ""
    project.uptrace_status, project.uptrace_error = UptraceStatus.PROVISIONING, ""
    project.save()
    _start_uptrace_sync(request, project, [old] if old and old != new else [])
    project.refresh_from_db()
    return _project_out(project, ProjectRole.OWNER)


MAX_SERVICE_NAMES = 50


def _check_service_names(names: list[str], org_id: int | None, project_id: int | None) -> list[str]:
    """Trimmed and deduplicated. A service can only map to one project per org."""
    cleaned = list(dict.fromkeys(n.strip() for n in names))
    if len(cleaned) > MAX_SERVICE_NAMES or any(not n or len(n) > 255 for n in cleaned):
        raise HttpError(400, f"service_names: up to {MAX_SERVICE_NAMES} names of 1-255 characters")
    others = Project.objects.filter(organization_id=org_id).exclude(id=project_id)
    for other in others.only("name", "service_names"):
        taken = set(cleaned) & set(other.service_names or [])
        if taken:
            raise HttpError(409, f"{sorted(taken)[0]!r} already belongs to project {other.name!r}")
    return cleaned


def _target_org(user, org_id: int | None) -> Organization:
    """The org a project is created in or moved to: the caller's personal org by default,
    otherwise one where the caller is at least an admin."""
    if org_id is None:
        return personal_org(user)
    return get_org_membership(user, org_id, OrgRole.ADMIN).organization


def _check_github_repo(user, installation_id: str, owner: str, name: str) -> None:
    """A project may only use an installation its editor proved access to (Connect GitHub),
    and a repo that installation can reach. Otherwise anyone could aim the agent at
    another customer's installation."""
    if not GitHubInstallation.objects.filter(user=user, installation_id=installation_id).exists():
        raise HttpError(400, "Connect GitHub and pick an installation you have access to")
    try:
        repos = github_connect.installation_repos(installation_id)
    except Exception as exc:
        logger.warning("could not list repos for installation %s: %s", installation_id, exc)
        raise HttpError(400, "Could not list that installation's repositories; try again") from exc
    if not any(r["owner"].lower() == owner.lower() and r["name"].lower() == name.lower()
               for r in repos):
        raise HttpError(400, f"The GitHub App installation can't access {owner}/{name}")


def _webhook_url(request: HttpRequest, project: Project) -> str:
    return request.build_absolute_uri(f"/api/sre/webhooks/uptrace/{project.id}")


def _webhook_urls(request: HttpRequest, project: Project) -> dict:
    url = _webhook_url(request, project)
    return {"webhook_secret": project.uptrace_webhook_secret, "webhook_url": url,
            "uptrace_webhook_url": f"{url}?token={project.uptrace_webhook_secret}"}


def _own_config(user, config_id: int) -> LLMProviderConfig:
    return get_object_or_404(LLMProviderConfig, id=config_id, owner=user)


INCIDENT_EXTRA_FIELDS = {
    "created_playbook_id", "playbook_run_id", "project_name", "playbook", "pr_url",
    "playbook_run_status", "execution_mode", "generate_tests", "usage", "runbook",
}


def _usage_out(rows) -> dict:
    by_step: dict[tuple, dict] = {}
    for row in rows:
        entry = by_step.setdefault((row.step, row.provider, row.model, row.billed_to), {
            "step": row.step, "provider": row.provider, "model": row.model,
            "billed_to": row.billed_to, "calls": 0, "input_tokens": 0, "cached_input_tokens": 0,
            "output_tokens": 0,
        })
        entry["calls"] += 1
        entry["input_tokens"] += row.input_tokens
        entry["cached_input_tokens"] += row.cached_input_tokens
        entry["output_tokens"] += row.output_tokens
    steps = list(by_step.values())
    input_tokens = sum(s["input_tokens"] for s in steps)
    output_tokens = sum(s["output_tokens"] for s in steps)
    return {
        "calls": sum(s["calls"] for s in steps),
        "input_tokens": input_tokens,
        "cached_input_tokens": sum(s["cached_input_tokens"] for s in steps),
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "platform_tokens": sum(s["input_tokens"] + s["output_tokens"] for s in steps
                               if s["billed_to"] == platform.PLATFORM),
        "models": list(dict.fromkeys(s["model"] for s in steps if s["model"])),
        "by_step": steps,
    }


def _incident_out(run: IncidentRun) -> dict:
    created = getattr(run, "created_playbook", None)
    playbook_run = getattr(run, "playbook_run", None)
    playbook, source = (run.matched_playbook, "matched") if run.matched_playbook else (created, "created")
    return {
        **{f: getattr(run, f) for f in IncidentRunOut.model_fields if f not in INCIDENT_EXTRA_FIELDS},
        "created_playbook_id": created.id if created else None,
        "playbook_run_id": playbook_run.id if playbook_run else None,
        "project_name": run.project.name,
        "playbook": {"id": playbook.id, "title": playbook.title, "status": playbook.status,
                     "source": source} if playbook else None,
        "pr_url": playbook_run.pr_url if playbook_run else "",
        "playbook_run_status": playbook_run.status if playbook_run else None,
        "execution_mode": playbook_run.execution_mode if playbook_run else None,
        "generate_tests": playbook_run.generate_tests if playbook_run else None,
        "usage": _usage_out(run.llm_usage.all()),
        "runbook": _runbook_brief(run, playbook_run),
    }


def _runbook_brief(run: IncidentRun, playbook_run) -> dict | None:
    if run.matched_runbook_id:
        runbook, source = run.matched_runbook, "matched"
    else:
        runbook = getattr(playbook_run, "created_runbook", None) if playbook_run else None
        source = "created"
    if runbook is None:
        return None
    return {"id": runbook.id, "title": runbook.title, "status": runbook.status, "source": source}


def _incident_runs():
    return IncidentRun.objects.select_related(
        "project", "matched_playbook", "created_playbook", "playbook_run",
        "matched_runbook", "playbook_run__created_runbook",
    ).prefetch_related("llm_usage")


def _detach_user_configs(project: Project, user) -> None:
    """A departing member's keys must stop paying for (and seeing) this project's runs."""
    if project.default_llm_config_id and project.default_llm_config.owner_id == user.id:
        project.default_llm_config = None
        project.save(update_fields=["default_llm_config", "updated_at"])
    LLMStepOverride.objects.filter(project=project, llm_config__owner=user).delete()


def _owner_count(project: Project) -> int:
    return project.memberships.filter(role=ProjectRole.OWNER).count()


# ---- webhook -----------------------------------------------------------------

def _webhook_authorized(request: HttpRequest, project: Project) -> bool:
    secret = project.uptrace_webhook_secret.encode()
    signature = request.headers.get("X-SRE-Signature", "")
    if signature.startswith("sha256="):
        expected = hmac.new(secret, request.body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature[len("sha256="):], expected)
    # Uptrace's webhook channel can't send headers or sign requests, only a URL, so it
    # passes the secret as ?token= (the URL settings shows). Other callers use the header.
    token = request.headers.get("X-SRE-Webhook-Secret", "") or request.GET.get("token", "")
    return bool(token) and hmac.compare_digest(token.encode(), secret)


# Uptrace notifies on every alert state change; only a new or recurring open alert is work.
# Uptrace 2.1 names events with underscores and calls an open alert "unresolved" (in
# `status`, mirrored in `state`); older releases used hyphens and "open".
UPTRACE_ACTIONABLE_EVENTS = {"created", "recurring", "state_changed", "status_changed",
                             "state-changed", "status-changed"}
UPTRACE_OPEN_STATES = {"open", "unresolved"}
# The events an alert comes back with after someone resolved it (or it auto-resolved).
UPTRACE_REOPEN_EVENTS = {"state_changed", "status_changed", "state-changed", "status-changed"}
# An incident in one of these is still working on its alert.
_ACTIVE_INCIDENT_STATUSES = {IncidentRun.Status.RUNNING, IncidentRun.Status.AWAITING_APPROVAL}

# alert.url is ".../alerting/<uptrace project id>/alerts/<alert id>" on the Uptrace instance.
_UPTRACE_ALERT_URL = re.compile(r"^https?://([^/?#]+)/alerting/(\d+)/alerts/")


def uptrace_source(alert: dict) -> str:
    """Which Uptrace instance + project an alert came from, e.g. "app.uptrace.dev/1"."""
    match = _UPTRACE_ALERT_URL.match(str(alert.get("url") or ""))
    return f"{match.group(1).lower()}/{match.group(2)}" if match else ""


def _uptrace_incident_key(project: Project, alert_id: str, event_name: str) -> str:
    """One incident per Uptrace alert, plus one more each time the alert reopens after its
    last incident finished: the bug came back (or the fix didn't hold), so it's new work.
    While an incident is still running, and for `recurring` reminders, it's the same one.
    Keys are uptrace-alert-{id}, then uptrace-alert-{id}-r2, -r3, ..."""
    base = f"uptrace-alert-{alert_id}"
    runs = IncidentRun.objects.filter(project=project).filter(
        Q(trace_id=base) | Q(trace_id__startswith=f"{base}-r"))
    latest = runs.order_by("-id").first()
    if latest is None:
        return base
    if latest.status in _ACTIVE_INCIDENT_STATUSES or event_name not in UPTRACE_REOPEN_EVENTS:
        return latest.trace_id
    return f"{base}-r{runs.count() + 1}"


def _pin_uptrace_source(project: Project, source: str) -> str | None:
    """Pins the project to the Uptrace project of its first alert, so a channel attached to
    another service's monitors can't send the agent after the wrong repo. Returns an error
    message if this alert comes from somewhere else."""
    if not project.uptrace_source_id and source:
        # Conditional update: two first alerts at once can't pin different sources.
        Project.objects.filter(id=project.id, uptrace_source_id="").update(uptrace_source_id=source)
        project.refresh_from_db(fields=["uptrace_source_id"])
    if project.uptrace_source_id and source != project.uptrace_source_id:
        return (f"This project only takes alerts from Uptrace project "
                f"{project.uptrace_source_id}, not {source or 'an unrecognised alert URL'}. "
                "Send that Uptrace project's alerts to its own project's webhook URL, or "
                "clear the pin in Settings → Projects.")
    return None


@router.post("/webhooks/uptrace/{project_id}", auth=None,
             response={200: UptraceWebhookOut, 202: dict, 401: dict, 409: dict, 422: dict,
                       503: dict})
def uptrace_webhook(request: HttpRequest, project_id: int, payload: UptraceWebhookIn):
    project = Project.objects.filter(id=project_id).first()
    # Same answer for unknown project and bad secret: don't reveal which projects exist.
    if project is None or not _webhook_authorized(request, project):
        return 401, {"detail": "Invalid webhook signature"}

    if payload.alert is not None:
        alert = payload.alert
        state = alert.get("status") or alert.get("state")
        if state not in UPTRACE_OPEN_STATES or payload.eventName not in UPTRACE_ACTIONABLE_EVENTS:
            return 202, {"detail": f"Ignored: alert {state or 'unknown'}, "
                                   f"event {payload.eventName or 'unknown'}"}
        if not alert.get("id"):
            return 422, {"detail": "Uptrace alert has no id"}
        mismatch = _pin_uptrace_source(project, uptrace_source(alert))
        if mismatch:
            return 409, {"detail": mismatch}
        # Uptrace already groups repeats of the same error into one alert, so a crash loop
        # is one incident, not one per occurrence.
        incident_key = _uptrace_incident_key(project, str(alert["id"]), payload.eventName or "")
    elif payload.trace_id:
        incident_key = payload.trace_id
    else:
        return 422, {"detail": "Expected an Uptrace alert notification or a trace_id"}

    workflow_id = f"sre-incident-{project.id}-{incident_key}"
    run, _ = IncidentRun.objects.get_or_create(
        temporal_workflow_id=workflow_id,
        defaults={
            "project": project,
            "trace_id": incident_key,
            "uptrace_exception_id": payload.exception_id or "",
            "raw_webhook_payload": payload.dict(),
        },
    )
    # Always (re)try the start: a duplicate is a no-op, and a previous failed start
    # (Temporal down) gets another chance when Uptrace retries.
    try:
        temporal_client.start_incident_workflow(workflow_id, IncidentInput(run.id, project.id))
    except Exception:
        logger.exception("could not start workflow %s", workflow_id)
        return 503, {"detail": "Could not start incident workflow; retry later"}
    return 200, {"incident_run_id": run.id, "temporal_workflow_id": workflow_id, "status": run.status}


# ---- projects ----------------------------------------------------------------

@router.get("/projects", response=list[ProjectOut])
def list_projects(request: HttpRequest):
    memberships = ProjectMembership.objects.filter(user=request.auth).select_related(
        "project", "project__organization"
    )
    return [_project_out(m.project, m.role) for m in memberships.order_by("-project__created_at")]


def _check_preset(preset: str | None) -> None:
    if preset is not None and preset not in platform.PRESETS:
        raise HttpError(400, f"Unknown company model option '{preset}'")


@router.post("/projects", response={201: ProjectCreatedOut})
def create_project(request: HttpRequest, payload: ProjectCreateIn):
    _check_preset(payload.platform_preset)
    _check_github_repo(request.auth, payload.github_installation_id,
                       payload.github_repo_owner, payload.github_repo_name)
    data = payload.dict()
    data["organization_id"] = _target_org(request.auth, data.pop("organization_id")).id
    data["service_names"] = _check_service_names(data["service_names"], data["organization_id"], None)
    share_with = data.pop("uptrace_share_with_project_id")
    # "Use my own Uptrace", or a project pinned to another Uptrace by hand: the old way.
    wants_managed = data.pop("uptrace_managed")
    managed = uptrace_admin.configured() and wants_managed and not data["uptrace_source_id"]
    if managed:
        data.update(uptrace_managed=True, uptrace_status=UptraceStatus.PROVISIONING)
        if share_with is not None:
            target = _share_target(request.auth, data["organization_id"], share_with,
                                   data["service_names"])
            data.update(uptrace_project_id=target.uptrace_project_id,
                        uptrace_source_id=target.uptrace_source_id)
    with transaction.atomic():
        project = Project.objects.create(**data)
        ProjectMembership.objects.create(project=project, user=request.auth, role=ProjectRole.OWNER)
    if managed:
        _start_uptrace_sync(request, project)
        project.refresh_from_db()
    return 201, {
        **_project_out(project, ProjectRole.OWNER),
        **_webhook_urls(request, project),
    }


@router.get("/projects/{project_id}", response=ProjectOut)
def get_project(request: HttpRequest, project_id: int):
    membership = get_membership(request.auth, project_id, ProjectRole.VIEWER)
    return _project_out(membership.project, membership.role)


@router.patch("/projects/{project_id}", response=ProjectOut)
def update_project(request: HttpRequest, project_id: int, payload: ProjectUpdateIn):
    changes = payload.dict(exclude_unset=True)
    min_role = ProjectRole.OWNER if OWNER_ONLY_PROJECT_FIELDS & changes.keys() else ProjectRole.ADMIN
    membership = get_membership(request.auth, project_id, min_role)
    project = membership.project
    if "default_llm_config_id" in changes and changes["default_llm_config_id"] is not None:
        _own_config(request.auth, changes["default_llm_config_id"])
    if changes.get("organization_id") is not None:
        _target_org(request.auth, changes["organization_id"])
    if changes.get("uptrace_credential_id") is not None:
        org_id = changes.get("organization_id") or project.organization_id
        if not UptraceCredential.objects.filter(id=changes["uptrace_credential_id"],
                                                organization_id=org_id).exists():
            raise HttpError(400, "That Uptrace credential isn't in this project's organization")
    if changes.get("service_names") is not None or changes.get("organization_id") is not None:
        changes["service_names"] = _check_service_names(
            changes.get("service_names") if changes.get("service_names") is not None
            else project.service_names,
            changes.get("organization_id") or project.organization_id, project.id,
        )
    _check_preset(changes.get("platform_preset"))
    # Existing wiring is grandfathered: only a change to it has to be proven.
    repo = {f: changes.get(f) or getattr(project, f) for f in GITHUB_REPO_FIELDS}
    if any(repo[f] != getattr(project, f) for f in GITHUB_REPO_FIELDS):
        _check_github_repo(request.auth, repo["github_installation_id"],
                           repo["github_repo_owner"], repo["github_repo_name"])
    group = _uptrace_group(project)
    if project.uptrace_managed:
        if changes.get("uptrace_source_id") not in (None, project.uptrace_source_id):
            raise HttpError(400, "The platform manages this project's Uptrace pin")
        moving = changes.get("organization_id") not in (None, project.organization_id)
        if len(group) > 1 and moving:
            raise HttpError(400, "This project shares its Uptrace project: stop sharing before moving it")
        if len(group) > 1 and changes.get("service_names") == []:
            raise HttpError(400, "This project shares its Uptrace project, so it needs at least one "
                                 "service name to tell its alerts apart")
    old_services = list(project.service_names)
    for field, value in changes.items():
        if value is None and field not in ("default_llm_config_id", "uptrace_credential_id"):
            continue
        setattr(project, field, value)
    project.save()
    if len(group) > 1 and project.service_names != old_services:
        _start_uptrace_sync(request, uptrace_project_ids=[project.uptrace_project_id])
    return _project_out(project, membership.role)


@router.delete("/projects/{project_id}", response={204: None, 503: dict})
def delete_project(request: HttpRequest, project_id: int):
    """Cancels the project's running workflows first (services/project_deletion.py); if
    Temporal can't be reached nothing is deleted."""
    project = get_project_for(request.auth, project_id, ProjectRole.OWNER)
    uptrace_project_id = project.uptrace_project_id if project.uptrace_managed else None
    try:
        project_deletion.delete_project(project)
    except project_deletion.TemporalUnavailable:
        return 503, {"detail": "Could not cancel this project's running work in Temporal, "
                               "so nothing was deleted; try again"}
    # Removes its monitor and channel (and unshares the rest of its Uptrace project).
    if uptrace_project_id and uptrace_admin.configured():
        _start_uptrace_sync(request, uptrace_project_ids=[uptrace_project_id])
    return 204, None


@router.post("/projects/{project_id}/webhook-secret/rotate", response=WebhookSecretOut)
def rotate_webhook_secret(request: HttpRequest, project_id: int):
    project = get_project_for(request.auth, project_id, ProjectRole.OWNER)
    project.uptrace_webhook_secret = secrets.token_urlsafe(32)
    project.save(update_fields=["uptrace_webhook_secret", "updated_at"])
    if uptrace_admin.is_managed(project) and project.uptrace_project_id:
        _start_uptrace_sync(request, uptrace_project_ids=[project.uptrace_project_id])
    return _webhook_urls(request, project)


# ---- GitHub connect --------------------------------------------------------------

def _settings_redirect(**params) -> HttpResponseRedirect:
    query = "&".join(f"{k}={v}" for k, v in {"tab": "github", **params}.items())
    return HttpResponseRedirect(f"{settings.FRONTEND_URL}/settings?{query}")


@router.get("/github/status", response=GitHubStatusOut)
def github_status(request: HttpRequest):
    return {"configured": github_connect.configured(), "app_slug": settings.GITHUB_APP_SLUG}


@router.post("/github/connect", response={200: GitHubConnectOut, 400: dict})
def github_connect_start(request: HttpRequest):
    if not github_connect.configured():
        return 400, {"detail": "The GitHub App isn't configured on this server (see docs/AUTH.md)"}
    state = github_connect.make_state(request.auth.id)
    return 200, {"install_url": github_connect.install_url(state),
                 "authorize_url": github_connect.authorize_url(state)}


@router.get("/github/callback", auth=None)
def github_connect_callback(request: HttpRequest, code: str = "", state: str = ""):
    """GitHub redirects the browser here after install/authorize. Always answers with a
    redirect to the settings page, never an error page."""
    try:
        if not github_connect.configured():
            raise github_connect.ConnectError("not_configured")
        user_id = github_connect.verify_state(state)
        user = get_user_model().objects.filter(id=user_id).first()
        if user is None:
            raise github_connect.ConnectError("invalid_state")
        if not code:
            raise github_connect.ConnectError("authorization_missing")
        installations = github_connect.user_installations(github_connect.exchange_code(code))
    except github_connect.ConnectError as exc:
        return _settings_redirect(github_error=exc.code)
    except Exception:
        logger.exception("GitHub connect callback failed")
        return _settings_redirect(github_error="github_api_failed")

    with transaction.atomic():
        seen, new = [], []
        for inst in installations:
            _, created = GitHubInstallation.objects.update_or_create(
                user=user, installation_id=inst["installation_id"],
                defaults={"account_login": inst["account_login"],
                          "account_type": inst["account_type"]},
            )
            seen.append(inst["installation_id"])
            if created:
                new.append(inst["installation_id"])
        # Access this GitHub user no longer has (uninstalled, removed from the org) is dropped.
        GitHubInstallation.objects.filter(user=user).exclude(installation_id__in=seen).delete()
    if not auto_projects.enabled():
        return _settings_redirect(github="connected", count=len(seen))
    # Only newly connected installations: reconnecting mustn't bring back deleted projects.
    projects = []
    for installation_id in new:
        try:
            projects += auto_projects.create_for_repos(user, installation_id)
        except Exception:
            logger.exception("could not create projects for installation %s", installation_id)
    _sync_new_projects(request, projects)
    return _settings_redirect(github="connected", count=len(seen), projects=len(projects))


def _sync_new_projects(request: HttpRequest, projects: list[Project]) -> None:
    for project in projects:
        if project.uptrace_managed:
            _start_uptrace_sync(request, project)


def _installation_event(request: HttpRequest, event: str, payload: dict):
    """Projects on install: an installation created, or repos added to one."""
    repos = auto_projects.repos_from_event(event, payload)
    action = payload.get("action") or ""
    if not auto_projects.enabled() or not repos:
        return 202, {"detail": f"Ignored: {event} {action}".strip()}
    installation_id = str((payload.get("installation") or {}).get("id", ""))
    user = auto_projects.owner_for_event(installation_id, (payload.get("sender") or {}).get("id"))
    if user is None:
        return 202, {"detail": "Ignored: no single connected user owns this installation yet"}
    repos = auto_projects.with_default_branches(installation_id, repos)
    projects = auto_projects.create_for_repos(user, installation_id, repos)
    _sync_new_projects(request, projects)
    if not projects:
        return 202, {"detail": "Ignored: every repo already has a project"}
    return 200, {"detail": "Created projects " + ", ".join(str(p.id) for p in projects)}


@router.get("/github/installations", response=list[GitHubInstallationOut])
def list_github_installations(request: HttpRequest):
    return list(GitHubInstallation.objects.filter(user=request.auth))


@router.delete("/github/installations/{installation_pk}", response={204: None})
def unlink_github_installation(request: HttpRequest, installation_pk: int):
    get_object_or_404(GitHubInstallation, id=installation_pk, user=request.auth).delete()
    return 204, None


@router.get("/github/installations/{installation_pk}/repos",
            response={200: list[GitHubRepoOut], 502: dict})
def list_github_repos(request: HttpRequest, installation_pk: int):
    installation = get_object_or_404(GitHubInstallation, id=installation_pk, user=request.auth)
    try:
        return 200, github_connect.installation_repos(installation.installation_id)
    except Exception:
        logger.exception("could not list repos for installation %s", installation.installation_id)
        return 502, {"detail": "Could not list repositories from GitHub"}


# ---- members -----------------------------------------------------------------

def _member_out(m: ProjectMembership) -> dict:
    return {"user_id": m.user_id, "email": m.user.email, "name": m.user.name, "role": m.role}


@router.get("/projects/{project_id}/members", response=list[MemberOut])
def list_members(request: HttpRequest, project_id: int):
    project = get_project_for(request.auth, project_id, ProjectRole.VIEWER)
    return [_member_out(m) for m in project.memberships.select_related("user").order_by("created_at")]


@router.post("/projects/{project_id}/members", response={201: MemberOut, 404: dict, 409: dict})
def add_member(request: HttpRequest, project_id: int, payload: MemberAddIn):
    project = get_project_for(request.auth, project_id, ProjectRole.OWNER)
    user = get_user_model().objects.filter(email=payload.email.lower()).first()
    if user is None:
        return 404, {"detail": "No user with that email"}
    membership, created = ProjectMembership.objects.get_or_create(
        project=project, user=user, defaults={"role": payload.role}
    )
    if not created:
        return 409, {"detail": "Already a member"}
    return 201, _member_out(membership)


@router.patch("/projects/{project_id}/members/{user_id}", response={200: MemberOut, 409: dict})
def update_member(request: HttpRequest, project_id: int, user_id: int, payload: MemberUpdateIn):
    project = get_project_for(request.auth, project_id, ProjectRole.OWNER)
    with transaction.atomic():
        membership = get_object_or_404(
            ProjectMembership.objects.select_for_update().select_related("user"),
            project=project, user_id=user_id,
        )
        if (membership.role == ProjectRole.OWNER and payload.role != ProjectRole.OWNER
                and _owner_count(project) == 1):
            return 409, {"detail": "A project must keep at least one owner"}
        membership.role = payload.role
        membership.save(update_fields=["role"])
    return 200, _member_out(membership)


@router.delete("/projects/{project_id}/members/{user_id}", response={204: None, 409: dict})
def remove_member(request: HttpRequest, project_id: int, user_id: int):
    # Any member may leave; only owners may remove someone else.
    min_role = ProjectRole.VIEWER if user_id == request.auth.id else ProjectRole.OWNER
    project = get_project_for(request.auth, project_id, min_role)
    with transaction.atomic():
        membership = get_object_or_404(
            ProjectMembership.objects.select_for_update().select_related("user"),
            project=project, user_id=user_id,
        )
        if membership.role == ProjectRole.OWNER and _owner_count(project) == 1:
            return 409, {"detail": "A project must keep at least one owner"}
        _detach_user_configs(project, membership.user)
        membership.delete()
    return 204, None


# ---- organizations -------------------------------------------------------------

def _org_out(m: OrganizationMembership) -> dict:
    org = m.organization
    return {"id": org.id, "name": org.name, "is_personal": org.is_personal, "role": m.role,
            "created_at": org.created_at}


def _org_member_out(m: OrganizationMembership) -> dict:
    return {"user_id": m.user_id, "email": m.user.email, "name": m.user.name, "role": m.role}


def _org_owner_count(org: Organization) -> int:
    return org.memberships.filter(role=OrgRole.OWNER).count()


@router.get("/organizations", response=list[OrganizationOut])
def list_organizations(request: HttpRequest):
    personal_org(request.auth)  # make sure it exists
    memberships = OrganizationMembership.objects.filter(user=request.auth).select_related("organization")
    return [_org_out(m) for m in memberships.order_by("-organization__is_personal", "organization__created_at")]


@router.post("/organizations", response={201: OrganizationOut})
def create_organization(request: HttpRequest, payload: OrganizationIn):
    with transaction.atomic():
        org = Organization.objects.create(name=payload.name.strip()[:255] or "Untitled")
        membership = OrganizationMembership.objects.create(
            organization=org, user=request.auth, role=OrgRole.OWNER
        )
    return 201, _org_out(membership)


@router.get("/organizations/{org_id}", response=OrganizationOut)
def get_organization(request: HttpRequest, org_id: int):
    return _org_out(get_org_membership(request.auth, org_id, OrgRole.MEMBER))


@router.patch("/organizations/{org_id}", response=OrganizationOut)
def update_organization(request: HttpRequest, org_id: int, payload: OrganizationIn):
    membership = get_org_membership(request.auth, org_id, OrgRole.OWNER)
    org = membership.organization
    org.name = payload.name.strip()[:255] or org.name
    org.save(update_fields=["name", "updated_at"])
    return _org_out(membership)


@router.get("/organizations/{org_id}/members", response=list[OrgMemberOut])
def list_org_members(request: HttpRequest, org_id: int):
    org = get_org_membership(request.auth, org_id, OrgRole.MEMBER).organization
    return [_org_member_out(m) for m in org.memberships.select_related("user").order_by("created_at")]


@router.post("/organizations/{org_id}/members",
             response={201: OrgMemberOut, 400: dict, 404: dict, 409: dict})
def add_org_member(request: HttpRequest, org_id: int, payload: OrgMemberAddIn):
    org = get_org_membership(request.auth, org_id, OrgRole.OWNER).organization
    if org.is_personal:
        return 400, {"detail": "A personal organization can't have other members"}
    user = get_user_model().objects.filter(email=payload.email.lower()).first()
    if user is None:
        return 404, {"detail": "No user with that email"}
    membership, created = OrganizationMembership.objects.get_or_create(
        organization=org, user=user, defaults={"role": payload.role}
    )
    if not created:
        return 409, {"detail": "Already a member"}
    return 201, _org_member_out(membership)


@router.patch("/organizations/{org_id}/members/{user_id}", response={200: OrgMemberOut, 409: dict})
def update_org_member(request: HttpRequest, org_id: int, user_id: int, payload: OrgMemberUpdateIn):
    org = get_org_membership(request.auth, org_id, OrgRole.OWNER).organization
    with transaction.atomic():
        membership = get_object_or_404(
            OrganizationMembership.objects.select_for_update().select_related("user"),
            organization=org, user_id=user_id,
        )
        if (membership.role == OrgRole.OWNER and payload.role != OrgRole.OWNER
                and _org_owner_count(org) == 1):
            return 409, {"detail": "An organization must keep at least one owner"}
        membership.role = payload.role
        membership.save(update_fields=["role"])
    return 200, _org_member_out(membership)


@router.delete("/organizations/{org_id}/members/{user_id}", response={204: None, 409: dict})
def remove_org_member(request: HttpRequest, org_id: int, user_id: int):
    min_role = OrgRole.MEMBER if user_id == request.auth.id else OrgRole.OWNER
    org = get_org_membership(request.auth, org_id, min_role).organization
    with transaction.atomic():
        membership = get_object_or_404(
            OrganizationMembership.objects.select_for_update(), organization=org, user_id=user_id,
        )
        if membership.role == OrgRole.OWNER and _org_owner_count(org) == 1:
            return 409, {"detail": "An organization must keep at least one owner"}
        membership.delete()
    return 204, None


# ---- Service mesh --------------------------------------------------------------

def _mesh_org(user, org_id: int, role: str) -> Organization:
    if not settings.SRE_SERVICE_MESH_ENABLED:
        raise Http404
    return get_org_membership(user, org_id, role).organization


@router.get("/organizations/{org_id}/service-graph", response={200: ServiceGraphOut, 400: dict})
def get_service_graph(request: HttpRequest, org_id: int, source: str = ""):
    org = _mesh_org(request.auth, org_id, OrgRole.MEMBER)
    sources = mesh.org_sources(org)
    if not source:
        if len(sources) > 1:
            return 400, {"detail": f"This organization's projects use several Uptrace projects; "
                                   f"pass ?source= one of {', '.join(sources)}"}
        source = sources[0] if sources else ""
    graph = ServiceGraph.objects.filter(organization=org, source=source).first()
    nodes, edges = [], []
    if graph is not None:
        projects = mesh.mesh_projects(org.id, source)
        for node in graph.nodes.all():
            project, mapped_by = (mesh.map_service(node.name, node.repo_url, projects)
                                  if node.kind == node.Kind.SERVICE else (None, ""))
            nodes.append({
                **{f: getattr(node, f) for f in ("id", "name", "kind", "repo_url",
                                                 "first_seen_at", "last_seen_at")},
                "project_id": project.id if project else None,
                "project_name": project.name if project else "",
                "mapped_by": mapped_by,
            })
        edges = list(graph.edges.all())
    return 200, {"organization_id": org.id, "source": source,
                 "refreshed_at": graph.refreshed_at if graph else None,
                 "nodes": nodes, "edges": edges}


@router.post("/organizations/{org_id}/service-graph/refresh", response={202: dict, 503: dict})
def refresh_service_graph(request: HttpRequest, org_id: int, source: str = ""):
    org = _mesh_org(request.auth, org_id, OrgRole.ADMIN)
    try:
        temporal_client.start_graph_refresh(org.id, source)
    except Exception:
        logger.exception("could not start the service graph refresh for org %s", org.id)
        return 503, {"detail": "Could not start the refresh; retry later"}
    return 202, {"detail": "Refresh started"}


# ---- Uptrace credentials (per organization) --------------------------------------

def _normalize_host(host: str) -> str:
    host = host.strip().lower()
    for prefix in ("https://", "http://"):
        host = host.removeprefix(prefix)
    return host.split("/", 1)[0]


def _check_api_url(url: str) -> None:
    try:
        validate_uptrace_api_url(url)
    except UnsafeURLError as exc:
        raise HttpError(400, str(exc)) from exc


def _credential_for_admin(user, credential_id: int) -> UptraceCredential:
    credential = get_object_or_404(UptraceCredential, id=credential_id)
    get_org_membership(user, credential.organization_id, OrgRole.ADMIN)
    return credential


@router.get("/organizations/{org_id}/uptrace-credentials", response=list[UptraceCredentialOut])
def list_uptrace_credentials(request: HttpRequest, org_id: int):
    org = get_org_membership(request.auth, org_id, OrgRole.ADMIN).organization
    return list(org.uptrace_credentials.all())


@router.post("/organizations/{org_id}/uptrace-credentials",
             response={201: UptraceCredentialOut, 409: dict})
def create_uptrace_credential(request: HttpRequest, org_id: int, payload: UptraceCredentialIn):
    from .crypto import encrypt

    org = get_org_membership(request.auth, org_id, OrgRole.ADMIN).organization
    _check_api_url(payload.api_base_url)
    if org.uptrace_credentials.filter(name=payload.name).exists():
        return 409, {"detail": "A credential with that name already exists"}
    credential = UptraceCredential.objects.create(
        organization=org, name=payload.name, host=_normalize_host(payload.host),
        api_base_url=payload.api_base_url.rstrip("/"), created_by=request.auth,
        token_encrypted=encrypt(payload.token) if payload.token else b"",
    )
    return 201, credential


@router.patch("/uptrace-credentials/{credential_id}", response=UptraceCredentialOut)
def update_uptrace_credential(request: HttpRequest, credential_id: int,
                              payload: UptraceCredentialUpdateIn):
    from .crypto import encrypt

    credential = _credential_for_admin(request.auth, credential_id)
    changes = payload.dict(exclude_unset=True)
    if changes.get("api_base_url") is not None:
        _check_api_url(changes["api_base_url"])
        credential.api_base_url = changes["api_base_url"].rstrip("/")
    if changes.get("host") is not None:
        credential.host = _normalize_host(changes["host"])
    if changes.get("name") is not None:
        credential.name = changes["name"]
    if changes.get("token") is not None:
        credential.token_encrypted = encrypt(changes["token"]) if changes["token"] else b""
    credential.save()
    return credential


@router.delete("/uptrace-credentials/{credential_id}", response={204: None})
def delete_uptrace_credential(request: HttpRequest, credential_id: int):
    _credential_for_admin(request.auth, credential_id).delete()
    return 204, None


# ---- company default model ---------------------------------------------------------

@router.get("/platform", response=PlatformOut)
def platform_default(request: HttpRequest):
    return platform.describe()


# ---- LLM configs (owned by the user, shared into projects by reference) ------

def _validate_config(provider: str, base_url: str) -> None:
    try:
        validate_llm_base_url(base_url)
    except UnsafeURLError as exc:
        raise HttpError(400, str(exc)) from exc
    if provider == LLMProvider.SELF_HOSTED and not base_url:
        raise HttpError(400, "self_hosted configs need a base_url")


def _set_api_key(config: LLMProviderConfig, api_key: str) -> None:
    from .crypto import encrypt

    config.api_key_encrypted = encrypt(api_key) if api_key else b""


@router.get("/llm-configs", response=list[LLMConfigOut])
def list_llm_configs(request: HttpRequest):
    return list(LLMProviderConfig.objects.filter(owner=request.auth))


@router.post("/llm-configs", response={201: LLMConfigOut})
def create_llm_config(request: HttpRequest, payload: LLMConfigIn):
    _validate_config(payload.provider, payload.base_url)
    config = LLMProviderConfig(
        owner=request.auth, name=payload.name, provider=payload.provider, model=payload.model,
        base_url=payload.base_url, extra_config=payload.extra_config,
    )
    _set_api_key(config, payload.api_key)
    config.save()
    return 201, config


@router.patch("/llm-configs/{config_id}", response=LLMConfigOut)
def update_llm_config(request: HttpRequest, config_id: int, payload: LLMConfigUpdateIn):
    config = _own_config(request.auth, config_id)
    changes = payload.dict(exclude_unset=True)
    if "base_url" in changes:
        _validate_config(config.provider, changes["base_url"] or "")
    for field in ("name", "model", "base_url", "extra_config"):
        if changes.get(field) is not None:
            setattr(config, field, changes[field])
    if changes.get("api_key") is not None:
        _set_api_key(config, changes["api_key"])
    config.save()
    return config


@router.delete("/llm-configs/{config_id}", response={204: None})
def delete_llm_config(request: HttpRequest, config_id: int):
    _own_config(request.auth, config_id).delete()
    return 204, None


# ---- step overrides -------------------------------------------------------------

def _overrides_out(project: Project) -> list[dict]:
    return [
        {"step": o.step, "llm_config_id": o.llm_config_id, "llm_config_name": o.llm_config.name}
        for o in project.step_overrides.select_related("llm_config").order_by("step")
    ]


@router.get("/projects/{project_id}/step-overrides", response=list[StepOverrideOut])
def list_step_overrides(request: HttpRequest, project_id: int):
    return _overrides_out(get_project_for(request.auth, project_id, ProjectRole.VIEWER))


@router.put("/projects/{project_id}/step-overrides", response=list[StepOverrideOut])
def set_step_overrides(request: HttpRequest, project_id: int, payload: StepOverridesIn):
    project = get_project_for(request.auth, project_id, ProjectRole.ADMIN)
    with transaction.atomic():
        for step, config_id in payload.overrides.items():
            if config_id is None:
                LLMStepOverride.objects.filter(project=project, step=step).delete()
                continue
            config = _own_config(request.auth, config_id)
            if not provider_supports_step(config.provider, step):
                raise HttpError(400, f"Jev configs can't run step '{step}'")
            LLMStepOverride.objects.update_or_create(
                project=project, step=step, defaults={"llm_config": config}
            )
    return _overrides_out(project)


# ---- playbooks -------------------------------------------------------------------

def _check_category(category: str | None) -> None:
    if category and category not in CATEGORIES:
        raise HttpError(400, f"Unknown category '{category}'")


def _new_playbook(user, payload: PlaybookCreateIn, **scope) -> Playbook:
    _check_category(payload.category)
    return Playbook.objects.create(
        **scope,
        title=payload.title,
        description=payload.description,
        keywords=[k.lower().strip() for k in payload.keywords if k.strip()],
        steps=clean_playbook_steps(payload.steps),
        execution_mode_override=payload.execution_mode_override,
        category=payload.category,
        symptoms=payload.symptoms,
        origin=Playbook.Origin.HUMAN,
        created_by=user,
        status=Playbook.Status.CONFIRMED,  # written by a human admin
    )


@router.get("/projects/{project_id}/playbooks", response=PlaybookListOut)
def list_playbooks(request: HttpRequest, project_id: int, status: str | None = None):
    project = get_project_for(request.auth, project_id, ProjectRole.VIEWER)
    qs = visible_playbooks(project).order_by("-created_at")
    if status:
        qs = qs.filter(status=status)
    return {"playbooks": list(qs), "total": qs.count()}


@router.post("/projects/{project_id}/playbooks", response={201: PlaybookOut})
def create_playbook(request: HttpRequest, project_id: int, payload: PlaybookCreateIn):
    project = get_project_for(request.auth, project_id, ProjectRole.ADMIN)
    # With runbooks on it's a generic playbook for the whole org; the project is only
    # where it came from.
    scope = dict(project=project, organization_id=project.organization_id,
                 is_generic=settings.SRE_RUNBOOKS_ENABLED)
    return 201, _new_playbook(request.auth, payload, **scope)


@router.post("/organizations/{org_id}/playbooks", response={201: PlaybookOut})
def create_org_playbook(request: HttpRequest, org_id: int, payload: PlaybookCreateIn):
    org = get_org_membership(request.auth, org_id, OrgRole.ADMIN).organization
    return 201, _new_playbook(request.auth, payload, organization=org, is_generic=True)


def _can_use_playbook(user, playbook: Playbook, min_role: ProjectRole) -> bool:
    """Legacy playbooks: a role on their project. Generic org ones: the org role (member to
    read, admin to write), or that project role on any project in the org."""
    if not playbook.is_generic or not settings.SRE_RUNBOOKS_ENABLED:
        if playbook.project_id is None:
            return False
        try:
            get_membership(user, playbook.project_id, min_role)
            return True
        except (Http404, HttpError):
            return False
    org_role = OrganizationMembership.objects.filter(
        organization_id=playbook.organization_id, user=user
    ).values_list("role", flat=True).first()
    needed = OrgRole.MEMBER if min_role == ProjectRole.VIEWER else OrgRole.ADMIN
    if org_role is not None and ORG_ROLE_RANK[org_role] >= ORG_ROLE_RANK[needed]:
        return True
    roles = ProjectMembership.objects.filter(
        user=user, project__organization_id=playbook.organization_id
    ).values_list("role", flat=True)
    return any(ROLE_RANK[r] >= ROLE_RANK[min_role] for r in roles)


def _playbook_for(user, playbook_id: int, min_role: ProjectRole) -> Playbook:
    """Can't see it → 404; can see it but not edit → 403. Built-ins: anyone may read them
    (with runbooks on), nobody may change them."""
    playbook = get_object_or_404(Playbook, id=playbook_id)
    if playbook.origin == Playbook.Origin.BUILTIN and playbook.organization_id is None:
        if not settings.SRE_RUNBOOKS_ENABLED:
            raise Http404("Playbook not found")
        if min_role != ProjectRole.VIEWER:
            raise HttpError(403, "Built-in playbooks are read-only")
        return playbook
    if _can_use_playbook(user, playbook, min_role):
        return playbook
    if min_role != ProjectRole.VIEWER and _can_use_playbook(user, playbook, ProjectRole.VIEWER):
        raise HttpError(403, f"Requires {min_role.label.lower()} role")
    raise Http404("Playbook not found")


@router.get("/playbooks/{playbook_id}", response=PlaybookOut)
def get_playbook(request: HttpRequest, playbook_id: int):
    return _playbook_for(request.auth, playbook_id, ProjectRole.VIEWER)


@router.patch("/playbooks/{playbook_id}", response=PlaybookOut)
def update_playbook(request: HttpRequest, playbook_id: int, payload: PlaybookUpdateIn):
    playbook = _playbook_for(request.auth, playbook_id, ProjectRole.ADMIN)
    changes = payload.dict(exclude_unset=True)
    _check_category(changes.get("category"))
    for field in ("title", "description", "category", "symptoms"):
        if changes.get(field) is not None:
            setattr(playbook, field, changes[field])
    if changes.get("keywords") is not None:
        playbook.keywords = [k.lower().strip() for k in changes["keywords"] if k.strip()]
    if changes.get("steps") is not None:
        playbook.steps = clean_playbook_steps(changes["steps"])
    if changes.get("status") is not None:
        playbook.status = changes["status"]
        if playbook.status != Playbook.Status.FAILING:
            playbook.consecutive_failure_count = 0
    if "execution_mode_override" in changes:
        playbook.execution_mode_override = changes["execution_mode_override"]
    playbook.save()
    return playbook


@router.delete("/playbooks/{playbook_id}", response={204: None, 409: dict})
def delete_playbook(request: HttpRequest, playbook_id: int):
    playbook = _playbook_for(request.auth, playbook_id, ProjectRole.ADMIN)
    if playbook.runbooks.exists():
        return 409, {"detail": "Runbooks still use this playbook; move or delete them first"}
    playbook.delete()
    return 204, None


# ---- runbooks ------------------------------------------------------------------------

def _require_runbooks() -> None:
    if not settings.SRE_RUNBOOKS_ENABLED:
        raise Http404("Runbooks aren't enabled on this server")


def _runbook_playbook(project: Project, playbook_id: int) -> Playbook:
    playbook = visible_playbooks(project).filter(id=playbook_id).first()
    if playbook is None:
        raise HttpError(400, "This project can't use that playbook")
    return playbook


def _runbook_for(user, runbook_id: int, min_role: ProjectRole) -> Runbook:
    _require_runbooks()
    runbook = get_object_or_404(Runbook, id=runbook_id)
    get_membership(user, runbook.project_id, min_role)
    return runbook


@router.get("/projects/{project_id}/runbooks", response=RunbookListOut)
def list_runbooks(request: HttpRequest, project_id: int, status: str | None = None,
                  playbook_id: int | None = None):
    _require_runbooks()
    project = get_project_for(request.auth, project_id, ProjectRole.VIEWER)
    qs = project.runbooks.all()
    if status:
        qs = qs.filter(status=status)
    if playbook_id is not None:
        qs = qs.filter(playbook_id=playbook_id)
    return {"runbooks": list(qs), "total": qs.count()}


@router.post("/projects/{project_id}/runbooks", response={201: RunbookOut})
def create_runbook(request: HttpRequest, project_id: int, payload: RunbookCreateIn):
    _require_runbooks()
    project = get_project_for(request.auth, project_id, ProjectRole.ADMIN)
    runbook = Runbook.objects.create(
        project=project,
        playbook=_runbook_playbook(project, payload.playbook_id),
        title=payload.title,
        description=payload.description,
        area=payload.area,
        keywords=[k.lower().strip() for k in payload.keywords if k.strip()],
        steps=clean_steps(payload.steps),
        service_name=payload.service_name,
        repo_owner=project.github_repo_owner,
        repo_name=project.github_repo_name,
        origin=Runbook.Origin.HUMAN,
        created_by=request.auth,
        status=Playbook.Status.CONFIRMED,  # written by a human admin
    )
    return 201, runbook


@router.get("/runbooks/{runbook_id}", response=RunbookOut)
def get_runbook(request: HttpRequest, runbook_id: int):
    return _runbook_for(request.auth, runbook_id, ProjectRole.VIEWER)


@router.patch("/runbooks/{runbook_id}", response=RunbookOut)
def update_runbook(request: HttpRequest, runbook_id: int, payload: RunbookUpdateIn):
    runbook = _runbook_for(request.auth, runbook_id, ProjectRole.ADMIN)
    changes = payload.dict(exclude_unset=True)
    if changes.get("playbook_id") is not None:
        runbook.playbook = _runbook_playbook(runbook.project, changes["playbook_id"])
    for field in ("title", "description", "area", "service_name"):
        if changes.get(field) is not None:
            setattr(runbook, field, changes[field])
    if changes.get("keywords") is not None:
        runbook.keywords = [k.lower().strip() for k in changes["keywords"] if k.strip()]
    if changes.get("steps") is not None:
        runbook.steps = clean_steps(changes["steps"])
    if changes.get("status") is not None:
        runbook.status = changes["status"]
        if runbook.status != Playbook.Status.FAILING:
            runbook.consecutive_failure_count = 0
    runbook.save()
    return runbook


@router.delete("/runbooks/{runbook_id}", response={204: None})
def delete_runbook(request: HttpRequest, runbook_id: int):
    _runbook_for(request.auth, runbook_id, ProjectRole.ADMIN).delete()
    return 204, None


# ---- incident runs & playbook runs ----------------------------------------------

@router.get("/incident-runs", response=IncidentRunListOut)
def list_incident_runs(request: HttpRequest, project_id: int | None = None,
                       status: str | None = None, page: int = 1, page_size: int = 20):
    page = max(page, 1)
    page_size = max(1, min(page_size, 100))
    qs = _incident_runs().filter(project_id__in=member_project_ids(request.auth))
    if project_id is not None:
        qs = qs.filter(project_id=project_id)
    if status:
        qs = qs.filter(status=status)
    total = qs.count()
    start = (page - 1) * page_size
    runs = qs[start:start + page_size]
    return {"runs": [_incident_out(r) for r in runs], "total": total}


@router.get("/incident-runs/{run_id}", response=IncidentRunOut)
def get_incident_run(request: HttpRequest, run_id: int):
    run = get_object_or_404(
        _incident_runs(),
        Q(id=run_id) & Q(project_id__in=member_project_ids(request.auth)),
    )
    return _incident_out(run)


def _playbook_run_for(user, playbook_run_id: int, min_role: ProjectRole) -> PlaybookRun:
    playbook_run = get_object_or_404(
        PlaybookRun.objects.select_related("incident_run"), id=playbook_run_id
    )
    get_membership(user, playbook_run.incident_run.project_id, min_role)
    return playbook_run


def _playbook_run_out(playbook_run: PlaybookRun) -> dict:
    return {
        **{f: getattr(playbook_run, f) for f in PlaybookRunOut.model_fields if f != "attempts"},
        "attempts": list(playbook_run.attempts.all()),
    }


@router.get("/playbook-runs/{playbook_run_id}", response=PlaybookRunOut)
def get_playbook_run(request: HttpRequest, playbook_run_id: int):
    return _playbook_run_out(_playbook_run_for(request.auth, playbook_run_id, ProjectRole.VIEWER))


@router.post("/playbook-runs/{playbook_run_id}/approve",
             response={200: PlaybookRunOut, 409: dict, 503: dict})
def approve_playbook_run(request: HttpRequest, playbook_run_id: int, payload: ApprovePlaybookRunIn):
    playbook_run = _playbook_run_for(request.auth, playbook_run_id, ProjectRole.ADMIN)
    # Conditional update: two admins clicking at once can't both decide.
    claimed = PlaybookRun.objects.filter(
        id=playbook_run.id, status=PlaybookRun.Status.PENDING_APPROVAL, approved_at__isnull=True,
    ).update(approved_by=request.auth, approved_at=timezone.now())
    if not claimed:
        return 409, {"detail": "This run is not waiting for approval"}
    try:
        temporal_client.signal_approval(
            playbook_run.incident_run.temporal_workflow_id,
            ApprovalDecision(approve=payload.approve, user_id=request.auth.id),
        )
    except Exception:
        logger.exception("could not signal workflow for playbook run %s", playbook_run.id)
        PlaybookRun.objects.filter(id=playbook_run.id).update(approved_by=None, approved_at=None)
        return 503, {"detail": "Could not reach the workflow; try again"}
    playbook_run.refresh_from_db()
    return 200, _playbook_run_out(playbook_run)


# "opened" too: someone can open a fresh PR from the agent's branch instead of reopening.
GITHUB_PR_ACTIONS = {"closed", "reopened", "opened"}


def _github_signature_valid(request: HttpRequest) -> bool:
    secret = settings.GITHUB_APP_WEBHOOK_SECRET.encode()
    expected = "sha256=" + hmac.new(secret, request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(request.headers.get("X-Hub-Signature-256", ""), expected)


@router.post("/github/webhook", auth=None, response={200: dict, 202: dict, 401: dict, 503: dict})
def github_webhook(request: HttpRequest):
    """The GitHub App's webhook. A draft-only run is decided on GitHub: merging its PR
    approves the fix, closing it unmerged rejects it, and reopening a rejected PR (or opening
    a new one from the same branch) puts the run back up for review. Everything else is
    acknowledged and ignored."""
    if not settings.GITHUB_APP_WEBHOOK_SECRET:
        return 503, {"detail": "GitHub webhooks are not configured"}
    if not _github_signature_valid(request):
        return 401, {"detail": "Invalid webhook signature"}

    event = request.headers.get("X-GitHub-Event", "")
    payload = json.loads(request.body or b"{}")
    action = payload.get("action") or ""
    if event in ("installation", "installation_repositories"):
        return _installation_event(request, event, payload)
    # Remediation agents: merges and pushes start on_merge / branch_watch scans. A merged
    # agent fix both approves its run (below) and triggers these.
    scans = []
    if settings.SRE_REMEDIATION_AGENTS_ENABLED and (
            event == "push" or (event == "pull_request" and action == "closed")):
        from .services import agents as agent_service
        try:
            scans = agent_service.on_push(payload) if event == "push" else agent_service.on_merge(payload)
        except agent_service.ScanStartFailed:
            return 503, {"detail": "Could not start a remediation scan; redeliver the webhook"}
    scans_note = f"Started scan runs {', '.join(str(s.id) for s in scans)}" if scans else ""
    if event == "push":
        return (200, {"detail": scans_note}) if scans else (202, {"detail": "Ignored: no agent watches this push"})
    if event != "pull_request" or action not in GITHUB_PR_ACTIONS:
        return 202, {"detail": f"Ignored: {event or 'unknown'} {action}".strip()}

    pr = payload.get("pull_request") or {}
    owner, _, name = str((payload.get("repository") or {}).get("full_name", "")).partition("/")
    playbook_run = PlaybookRun.objects.select_related("incident_run").filter(
        branch_name=(pr.get("head") or {}).get("ref", ""),
        execution_mode=ExecutionMode.DRAFT_ONLY,
        incident_run__project__github_repo_owner__iexact=owner,
        incident_run__project__github_repo_name__iexact=name,
    ).first()
    if playbook_run is None:
        if scans:
            return 200, {"detail": scans_note}
        return 202, {"detail": "Ignored: not a pull request the agent is waiting on"}

    Status = PlaybookRun.Status
    undecided = Q(status__in=[Status.RUNNING, Status.PENDING_APPROVAL], approved_at__isnull=True)
    pr_url = pr.get("html_url") or playbook_run.pr_url
    merged = bool(pr.get("merged"))
    # Each transition is a conditional update, so a redelivery (or an in-app decision) can't
    # apply it twice. RUNNING counts as undecided: the PR can close before the workflow has
    # marked the run pending. A merge is also taken from REJECTED in case the reopen was lost.
    if action in ("opened", "reopened"):
        claim, changes = Q(status=Status.REJECTED), {"approved_by": None, "approved_at": None}
        send = lambda wid: temporal_client.signal_pull_request_reopened(wid)
        done = "Reopened: awaiting review again"
    else:
        claim = undecided | Q(status=Status.REJECTED) if merged else undecided
        changes = {"approved_by": None, "approved_at": timezone.now()}
        decision = ApprovalDecision(approve=merged, user_id=0, via_github=True)
        send = lambda wid: temporal_client.signal_approval(wid, decision)
        done = "Approved: PR merged" if merged else "Rejected: PR closed without merging"

    if not PlaybookRun.objects.filter(claim, id=playbook_run.id).update(**changes, pr_url=pr_url):
        return 202, {"detail": f"Ignored: nothing to do for a {action} PR on a {playbook_run.status} run"}
    try:
        send(playbook_run.incident_run.temporal_workflow_id)
    except Exception as exc:
        PlaybookRun.objects.filter(id=playbook_run.id).update(
            approved_by_id=playbook_run.approved_by_id, approved_at=playbook_run.approved_at,
        )
        if isinstance(exc, temporal_client.WorkflowFinished):
            return 202, {"detail": "Ignored: this run's workflow has finished (a rejected PR is "
                                   "only watched for reopening for 30 days)"}
        logger.exception("could not signal workflow for playbook run %s", playbook_run.id)
        # GitHub doesn't retry; redeliver from the App's "Advanced" settings.
        return 503, {"detail": "Could not reach the workflow; redeliver this webhook"}
    return 200, {"detail": done}


from . import agents_api  # noqa: E402,F401  (registers the remediation agent routes)
