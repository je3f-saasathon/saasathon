from django.http import Http404
from ninja.errors import HttpError

from .models import ROLE_RANK, Project, ProjectMembership, ProjectRole


def get_membership(user, project_id: int, min_role: ProjectRole) -> ProjectMembership:
    """Non-member → 404 (don't reveal the project exists); role too low → 403."""
    membership = (
        ProjectMembership.objects.select_related("project")
        .filter(project_id=project_id, user=user)
        .first()
    )
    if membership is None:
        raise Http404("Project not found")
    if ROLE_RANK[membership.role] < ROLE_RANK[min_role]:
        raise HttpError(403, f"Requires {min_role.label.lower()} role")
    return membership


def get_project_for(user, project_id: int, min_role: ProjectRole) -> Project:
    return get_membership(user, project_id, min_role).project


def member_project_ids(user):
    return ProjectMembership.objects.filter(user=user).values_list("project_id", flat=True)
