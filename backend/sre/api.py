import hashlib
import hmac
import logging
import re
import secrets

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.conf import settings
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError

from . import temporal_client
from .llm import platform
from .llm.resolve import provider_supports_step
from .models import (
    GitHubInstallation,
    IncidentRun,
    LLMProvider,
    LLMProviderConfig,
    LLMStepOverride,
    Playbook,
    PlaybookRun,
    Project,
    ProjectMembership,
    ProjectRole,
)
from .permissions import get_membership, get_project_for, member_project_ids
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
    PlatformOut,
    PlaybookCreateIn,
    PlaybookListOut,
    PlaybookOut,
    PlaybookRunOut,
    PlaybookUpdateIn,
    ProjectCreatedOut,
    ProjectCreateIn,
    ProjectOut,
    ProjectUpdateIn,
    StepOverrideOut,
    StepOverridesIn,
    UptraceWebhookIn,
    UptraceWebhookOut,
    WebhookSecretOut,
)
from .services import github_connect
from .services.playbooks import clean_steps
from .temporal_types import ApprovalDecision, IncidentInput
from .validators import UnsafeURLError, validate_llm_base_url

logger = logging.getLogger(__name__)
router = Router(tags=["sre"])

GITHUB_REPO_FIELDS = {"github_installation_id", "github_repo_owner", "github_repo_name"}
OWNER_ONLY_PROJECT_FIELDS = GITHUB_REPO_FIELDS | {"uptrace_source_id"}


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
           if f not in ("role", "github_verified", "platform_tokens_this_month")},
        "role": role,
        "github_verified": _github_verified(project),
        "platform_tokens_this_month": platform.tokens_this_month(project),
    }


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
    "playbook_run_status", "execution_mode", "generate_tests", "usage",
}


def _usage_out(rows) -> dict:
    by_step: dict[tuple, dict] = {}
    for row in rows:
        entry = by_step.setdefault((row.step, row.provider, row.model, row.billed_to), {
            "step": row.step, "provider": row.provider, "model": row.model,
            "billed_to": row.billed_to, "calls": 0, "input_tokens": 0, "output_tokens": 0,
        })
        entry["calls"] += 1
        entry["input_tokens"] += row.input_tokens
        entry["output_tokens"] += row.output_tokens
    steps = list(by_step.values())
    input_tokens = sum(s["input_tokens"] for s in steps)
    output_tokens = sum(s["output_tokens"] for s in steps)
    return {
        "calls": sum(s["calls"] for s in steps),
        "input_tokens": input_tokens,
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
    }


def _incident_runs():
    return IncidentRun.objects.select_related(
        "project", "matched_playbook", "created_playbook", "playbook_run"
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
UPTRACE_ACTIONABLE_EVENTS = {"created", "recurring", "state-changed"}

# alert.url is ".../alerting/<uptrace project id>/alerts/<alert id>" on the Uptrace instance.
_UPTRACE_ALERT_URL = re.compile(r"^https?://([^/?#]+)/alerting/(\d+)/alerts/")


def uptrace_source(alert: dict) -> str:
    """Which Uptrace instance + project an alert came from, e.g. "app.uptrace.dev/1"."""
    match = _UPTRACE_ALERT_URL.match(str(alert.get("url") or ""))
    return f"{match.group(1).lower()}/{match.group(2)}" if match else ""


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
        if alert.get("state") != "open" or payload.eventName not in UPTRACE_ACTIONABLE_EVENTS:
            return 202, {"detail": f"Ignored: alert {alert.get('state') or 'unknown'}, "
                                   f"event {payload.eventName or 'unknown'}"}
        if not alert.get("id"):
            return 422, {"detail": "Uptrace alert has no id"}
        mismatch = _pin_uptrace_source(project, uptrace_source(alert))
        if mismatch:
            return 409, {"detail": mismatch}
        # One incident per Uptrace alert: Uptrace already groups repeats of the same error
        # into one alert, so a crash loop is one incident, not one per occurrence.
        incident_key = f"uptrace-alert-{alert['id']}"
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
    memberships = ProjectMembership.objects.filter(user=request.auth).select_related("project")
    return [_project_out(m.project, m.role) for m in memberships.order_by("-project__created_at")]


@router.post("/projects", response={201: ProjectCreatedOut})
def create_project(request: HttpRequest, payload: ProjectCreateIn):
    _check_github_repo(request.auth, payload.github_installation_id,
                       payload.github_repo_owner, payload.github_repo_name)
    with transaction.atomic():
        project = Project.objects.create(**payload.dict())
        ProjectMembership.objects.create(project=project, user=request.auth, role=ProjectRole.OWNER)
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
    # Existing wiring is grandfathered: only a change to it has to be proven.
    repo = {f: changes.get(f) or getattr(project, f) for f in GITHUB_REPO_FIELDS}
    if any(repo[f] != getattr(project, f) for f in GITHUB_REPO_FIELDS):
        _check_github_repo(request.auth, repo["github_installation_id"],
                           repo["github_repo_owner"], repo["github_repo_name"])
    for field, value in changes.items():
        if value is None and field != "default_llm_config_id":
            continue
        setattr(project, field, value)
    project.save()
    return _project_out(project, membership.role)


@router.delete("/projects/{project_id}", response={204: None})
def delete_project(request: HttpRequest, project_id: int):
    get_project_for(request.auth, project_id, ProjectRole.OWNER).delete()
    return 204, None


@router.post("/projects/{project_id}/webhook-secret/rotate", response=WebhookSecretOut)
def rotate_webhook_secret(request: HttpRequest, project_id: int):
    project = get_project_for(request.auth, project_id, ProjectRole.OWNER)
    project.uptrace_webhook_secret = secrets.token_urlsafe(32)
    project.save(update_fields=["uptrace_webhook_secret", "updated_at"])
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
        seen = []
        for inst in installations:
            GitHubInstallation.objects.update_or_create(
                user=user, installation_id=inst["installation_id"],
                defaults={"account_login": inst["account_login"],
                          "account_type": inst["account_type"]},
            )
            seen.append(inst["installation_id"])
        # Access this GitHub user no longer has (uninstalled, removed from the org) is dropped.
        GitHubInstallation.objects.filter(user=user).exclude(installation_id__in=seen).delete()
    return _settings_redirect(github="connected", count=len(seen))


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

@router.get("/projects/{project_id}/playbooks", response=PlaybookListOut)
def list_playbooks(request: HttpRequest, project_id: int, status: str | None = None):
    project = get_project_for(request.auth, project_id, ProjectRole.VIEWER)
    qs = project.playbooks.all()
    if status:
        qs = qs.filter(status=status)
    return {"playbooks": list(qs), "total": qs.count()}


@router.post("/projects/{project_id}/playbooks", response={201: PlaybookOut})
def create_playbook(request: HttpRequest, project_id: int, payload: PlaybookCreateIn):
    project = get_project_for(request.auth, project_id, ProjectRole.ADMIN)
    playbook = Playbook.objects.create(
        project=project,
        title=payload.title,
        description=payload.description,
        keywords=[k.lower().strip() for k in payload.keywords if k.strip()],
        steps=clean_steps(payload.steps),
        execution_mode_override=payload.execution_mode_override,
        status=Playbook.Status.CONFIRMED,  # written by a human admin
    )
    return 201, playbook


def _playbook_for(user, playbook_id: int, min_role: ProjectRole) -> Playbook:
    playbook = get_object_or_404(Playbook, id=playbook_id)
    get_membership(user, playbook.project_id, min_role)
    return playbook


@router.get("/playbooks/{playbook_id}", response=PlaybookOut)
def get_playbook(request: HttpRequest, playbook_id: int):
    return _playbook_for(request.auth, playbook_id, ProjectRole.VIEWER)


@router.patch("/playbooks/{playbook_id}", response=PlaybookOut)
def update_playbook(request: HttpRequest, playbook_id: int, payload: PlaybookUpdateIn):
    playbook = _playbook_for(request.auth, playbook_id, ProjectRole.ADMIN)
    changes = payload.dict(exclude_unset=True)
    for field in ("title", "description"):
        if changes.get(field) is not None:
            setattr(playbook, field, changes[field])
    if changes.get("keywords") is not None:
        playbook.keywords = [k.lower().strip() for k in changes["keywords"] if k.strip()]
    if changes.get("steps") is not None:
        playbook.steps = clean_steps(changes["steps"])
    if changes.get("status") is not None:
        playbook.status = changes["status"]
        if playbook.status != Playbook.Status.FAILING:
            playbook.consecutive_failure_count = 0
    if "execution_mode_override" in changes:
        playbook.execution_mode_override = changes["execution_mode_override"]
    playbook.save()
    return playbook


@router.delete("/playbooks/{playbook_id}", response={204: None})
def delete_playbook(request: HttpRequest, playbook_id: int):
    _playbook_for(request.auth, playbook_id, ProjectRole.ADMIN).delete()
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
