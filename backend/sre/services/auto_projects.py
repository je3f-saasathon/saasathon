"""Projects on install (SRE_GITHUB_AUTO_PROJECTS): connecting a GitHub App installation
creates a draft-only project for each repo it can reach, so installing the App is the
whole setup and `buggly run` finds a project for the repo. The caller starts the managed
Uptrace sync for the projects this returns (it needs the request's base URL)."""

import logging

from django.conf import settings
from django.db import transaction

from ..models import GitHubInstallation, Project, ProjectMembership, ProjectRole, UptraceStatus
from ..orgs import personal_org
from . import github_connect, uptrace_admin

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return settings.SRE_GITHUB_AUTO_PROJECTS


def create_for_repos(user, installation_id: str, repos: list[dict] | None = None) -> list[Project]:
    """A project per repo ({owner, name, default_branch?}) that has none on this
    installation yet. repos=None lists what the installation can reach."""
    if repos is None:
        repos = github_connect.installation_repos(installation_id)
    org = personal_org(user)
    taken_services = {n for names in Project.objects.filter(organization=org)
                      .values_list("service_names", flat=True) for n in names or []}
    managed = uptrace_admin.configured()
    created = []
    for repo in repos:
        owner, name = repo["owner"], repo["name"]
        if Project.objects.filter(github_installation_id=installation_id,
                                  github_repo_owner__iexact=owner, github_repo_name__iexact=name).exists():
            continue
        services = [] if name in taken_services else [name]
        taken_services.update(services)
        with transaction.atomic():
            project = Project.objects.create(
                organization=org, name=name, github_installation_id=installation_id,
                github_repo_owner=owner, github_repo_name=name,
                github_default_branch=repo.get("default_branch") or "main",
                service_names=services, uptrace_managed=managed,
                uptrace_status=UptraceStatus.PROVISIONING if managed else "",
            )
            ProjectMembership.objects.create(project=project, user=user, role=ProjectRole.OWNER)
        created.append(project)
    return created


def owner_for_event(installation_id: str, sender_github_id) -> "object | None":
    """Who a GitHub installation event's projects belong to: a user who connected this
    installation (proved access) and whose GitHub account sent the event, else the only
    user who connected it. None when that's ambiguous or nobody has connected it yet (the
    connect callback then creates them)."""
    linked = GitHubInstallation.objects.filter(installation_id=installation_id).select_related("user")
    users = [link.user for link in linked]
    if sender_github_id is not None:
        for user in users:
            if user.github_id and user.github_id == str(sender_github_id):
                return user
    return users[0] if len(users) == 1 else None


def repos_from_event(event: str, payload: dict) -> list[dict] | None:
    """The repos an installation event adds, or None if it adds none. Events only carry
    owner/name ("full_name"); the default branch is filled in from GitHub when listed."""
    action = payload.get("action") or ""
    if event == "installation" and action == "created":
        listed = payload.get("repositories") or []
    elif event == "installation_repositories" and action == "added":
        listed = payload.get("repositories_added") or []
    else:
        return None
    repos = []
    for r in listed:
        owner, _, name = str(r.get("full_name", "")).partition("/")
        if owner and name:
            repos.append({"owner": owner, "name": name})
    return repos


def with_default_branches(installation_id: str, repos: list[dict]) -> list[dict]:
    """Fills in each repo's default branch from GitHub; keeps "main" if that fails."""
    try:
        listed = {(r["owner"].lower(), r["name"].lower()): r
                  for r in github_connect.installation_repos(installation_id)}
    except Exception as exc:
        logger.warning("could not list repos for installation %s: %s", installation_id, exc)
        return repos
    return [listed.get((r["owner"].lower(), r["name"].lower()), r) for r in repos]
