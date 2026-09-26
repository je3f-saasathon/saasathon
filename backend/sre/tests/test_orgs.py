import json

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from accounts.oauth import find_or_create_user
from sre.models import Organization, OrganizationMembership, OrgRole, Project
from sre.orgs import personal_org

pytestmark = pytest.mark.django_db


def _personal(user):
    return Organization.objects.get(is_personal=True, memberships__user=user)


# ---- every account gets a personal org ----------------------------------------------

def test_email_signup_creates_personal_org(client):
    resp = client.post("/api/auth/register", content_type="application/json",
                       data=json.dumps({"email": "new@example.com", "password": "s3curePassw0rd!"}))
    assert resp.status_code == 200
    org = Organization.objects.get(is_personal=True, memberships__user__email="new@example.com")
    assert org.name == "new@example.com's workspace"
    assert org.memberships.get().role == OrgRole.OWNER


def test_oauth_signup_creates_personal_org_once():
    identity = {"email": "gh@example.com", "provider_id": "42", "name": "GH"}
    user = find_or_create_user(provider="github", identity=identity)
    find_or_create_user(provider="github", identity=identity)  # a second login saves nothing new
    assert Organization.objects.filter(is_personal=True, memberships__user=user).count() == 1


def test_personal_org_is_created_on_first_use_if_missing(make_user):
    user = make_user()
    _personal(user).delete()
    org = personal_org(user)
    assert org.is_personal and personal_org(user) == org


# ---- backfill migration ----------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_backfill_gives_projects_their_earliest_owners_org_and_reverses():
    executor = MigrationExecutor(connection)
    executor.migrate([("sre", "0006_organizations")])
    apps = executor.loader.project_state([("sre", "0006_organizations")]).apps
    User = apps.get_model("accounts", "User")
    Project_ = apps.get_model("sre", "Project")
    Membership = apps.get_model("sre", "ProjectMembership")
    owner = User.objects.create(email="owner@example.com")
    later_owner = User.objects.create(email="later@example.com")
    viewer = User.objects.create(email="viewer@example.com")
    project = Project_.objects.create(name="p", github_installation_id="1",
                                      github_repo_owner="a", github_repo_name="b")
    Membership.objects.create(project=project, user=owner, role="owner")
    Membership.objects.create(project=project, user=later_owner, role="owner")
    Membership.objects.create(project=project, user=viewer, role="viewer")

    executor = MigrationExecutor(connection)
    executor.migrate([("sre", "0007_backfill_personal_orgs")])
    project = Project.objects.get(id=project.id)
    assert project.organization.memberships.get().user_id == owner.id
    assert Organization.objects.filter(is_personal=True).count() == 3  # one per user
    # The viewer can still see the project, but isn't in the owner's org.
    assert not OrganizationMembership.objects.filter(organization=project.organization,
                                                     user_id=viewer.id).exists()

    executor = MigrationExecutor(connection)
    executor.migrate([("sre", "0006_organizations")])
    assert Organization.objects.count() == 0
    assert Project.objects.get(id=project.id).organization_id is None

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


# ---- API ------------------------------------------------------------------------------

def test_list_orgs_shows_personal_first(make_user, api_for):
    user = make_user()
    api = api_for(user)
    team = api.post("/organizations", {"name": "Acme"}).json()
    body = api.get("/organizations").json()
    assert [o["is_personal"] for o in body] == [True, False]
    assert body[1] == {**team, "role": "owner"}


def test_org_access_rules(make_user, api_for):
    owner, member, outsider = make_user("o@x.com"), make_user("m@x.com"), make_user("x@x.com")
    org_id = api_for(owner).post("/organizations", {"name": "Acme"}).json()["id"]
    assert api_for(owner).post(f"/organizations/{org_id}/members",
                               {"email": "m@x.com", "role": "member"}).status_code == 201
    assert api_for(outsider).get(f"/organizations/{org_id}").status_code == 404
    assert api_for(member).get(f"/organizations/{org_id}/members").status_code == 200
    assert api_for(member).patch(f"/organizations/{org_id}", {"name": "Nope"}).status_code == 403
    assert api_for(member).post(f"/organizations/{org_id}/members",
                                {"email": "x@x.com", "role": "member"}).status_code == 403


def test_last_org_owner_is_kept(make_user, api_for):
    owner = make_user()
    api = api_for(owner)
    org_id = api.post("/organizations", {"name": "Acme"}).json()["id"]
    assert api.patch(f"/organizations/{org_id}/members/{owner.id}", {"role": "member"}).status_code == 409
    assert api.delete(f"/organizations/{org_id}/members/{owner.id}").status_code == 409


def test_personal_org_takes_no_members(make_user, api_for):
    user = make_user("a@x.com")
    make_user("b@x.com")
    resp = api_for(user).post(f"/organizations/{_personal(user).id}/members",
                              {"email": "b@x.com", "role": "member"})
    assert resp.status_code == 400


def _create_project(api, **extra):
    return api.post("/projects", {"name": "p", "github_installation_id": "1",
                                  "github_repo_owner": "acme", "github_repo_name": "shop", **extra})


@pytest.fixture
def github_ok(monkeypatch):
    from sre import api as sre_api
    monkeypatch.setattr(sre_api, "_check_github_repo", lambda *a: None)


def test_project_defaults_to_personal_org(make_user, api_for, github_ok):
    user = make_user()
    body = _create_project(api_for(user)).json()
    assert body["organization_id"] == _personal(user).id
    assert body["organization_name"] == f"{user.email}'s workspace"


def test_project_in_another_org_needs_org_admin(make_user, api_for, github_ok):
    owner, member = make_user("o@x.com"), make_user("m@x.com")
    org_id = api_for(owner).post("/organizations", {"name": "Acme"}).json()["id"]
    api_for(owner).post(f"/organizations/{org_id}/members", {"email": "m@x.com", "role": "member"})
    assert _create_project(api_for(member), organization_id=org_id).status_code == 403
    assert _create_project(api_for(make_user("z@x.com")), organization_id=org_id).status_code == 404
    resp = _create_project(api_for(owner), organization_id=org_id)
    assert resp.status_code == 201 and resp.json()["organization_id"] == org_id


def test_moving_a_project_needs_owner_and_target_admin(make_user, api_for, make_project, add_member):
    owner, admin = make_user("o@x.com"), make_user("a@x.com")
    project = make_project(owner)
    add_member(project, admin, "admin")
    org_id = api_for(owner).post("/organizations", {"name": "Acme"}).json()["id"]
    assert api_for(admin).patch(f"/projects/{project.id}", {"organization_id": org_id}).status_code == 403
    resp = api_for(owner).patch(f"/projects/{project.id}", {"organization_id": org_id})
    assert resp.status_code == 200 and resp.json()["organization_id"] == org_id
