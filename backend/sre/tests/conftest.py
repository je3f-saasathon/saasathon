import json

import pytest

from accounts.models import AuthToken
from sre.models import Project, ProjectMembership, ProjectRole
from sre.orgs import personal_org


class Api:
    def __init__(self, client, user):
        _, raw = AuthToken.issue(user)
        self.client = client
        self.headers = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}

    def _send(self, method, path, body=None):
        kwargs = dict(self.headers)
        if body is not None:
            kwargs.update(data=json.dumps(body), content_type="application/json")
        return getattr(self.client, method)(f"/api/sre{path}", **kwargs)

    def get(self, path):
        return self._send("get", path)

    def post(self, path, body=None):
        return self._send("post", path, body if body is not None else {})

    def patch(self, path, body):
        return self._send("patch", path, body)

    def put(self, path, body):
        return self._send("put", path, body)

    def delete(self, path):
        return self._send("delete", path)


@pytest.fixture
def api_for(client):
    return lambda user: Api(client, user)


@pytest.fixture
def make_project(db):
    def _make(owner, name="shop", **kwargs):
        kwargs.setdefault("organization", personal_org(owner))
        project = Project.objects.create(
            name=name, github_installation_id="1", github_repo_owner="acme",
            github_repo_name="shop", **kwargs,
        )
        ProjectMembership.objects.create(project=project, user=owner, role=ProjectRole.OWNER)
        return project

    return _make


@pytest.fixture
def add_member():
    def _add(project, user, role):
        return ProjectMembership.objects.create(project=project, user=user, role=role)

    return _add
