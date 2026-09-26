"""Projects on install (SRE_GITHUB_AUTO_PROJECTS) and the CLI's project lookup."""

from urllib.parse import parse_qs, urlparse

import pytest
import responses

from sre import temporal_client
from sre.crypto import encrypt
from sre.models import GitHubInstallation, Project, ProjectRole, UptraceStatus
from sre.services import github_connect
from sre.tests.test_api import post_github

pytestmark = pytest.mark.django_db

REPOS = {"42": [{"owner": "acme", "name": "shop", "default_branch": "trunk", "private": True},
                {"owner": "acme", "name": "cart", "default_branch": "main", "private": False}]}


@pytest.fixture(autouse=True)
def app_settings(settings, monkeypatch):
    settings.GITHUB_APP_SLUG = "sre-app"
    settings.GITHUB_APP_CLIENT_ID = "Iv1.client"
    settings.GITHUB_APP_CLIENT_SECRET = "secret"
    settings.GITHUB_APP_WEBHOOK_SECRET = "gh-webhook-secret"
    settings.FRONTEND_URL = "http://frontend"
    settings.SRE_GITHUB_AUTO_PROJECTS = True
    settings.UPTRACE_MANAGED_URL = "https://uptrace.example.test"
    settings.UPTRACE_MANAGED_API_URL = "https://uptrace.example.test"
    settings.UPTRACE_MANAGED_TOKEN = "admin-token"
    monkeypatch.setattr(github_connect, "installation_repos", lambda i: REPOS.get(i, []))
    return settings


@pytest.fixture
def syncs(monkeypatch):
    started = []
    monkeypatch.setattr(temporal_client, "start_uptrace_sync", started.append)
    return started


def connect(client, user):
    responses.post("https://github.com/login/oauth/access_token", json={"access_token": "user-tok"})
    responses.get("https://api.github.com/user/installations", json={"installations": [
        {"id": 42, "app_slug": "sre-app", "account": {"login": "acme", "type": "Organization"}}]})
    resp = client.get("/api/sre/github/callback",
                      {"code": "c0de", "state": github_connect.make_state(user.id)})
    return {k: v[0] for k, v in parse_qs(urlparse(resp["Location"]).query).items()}


@responses.activate
def test_connecting_an_installation_creates_a_project_per_repo(client, make_user, syncs):
    user = make_user()
    assert connect(client, user) == {"tab": "github", "github": "connected", "count": "1", "projects": "2"}

    shop = Project.objects.get(github_repo_name="shop")
    assert (shop.name, shop.github_installation_id, shop.github_default_branch) == ("shop", "42", "trunk")
    assert shop.default_execution_mode == "draft_only"
    assert shop.service_names == ["shop"]
    assert shop.uptrace_managed and shop.uptrace_status == UptraceStatus.PROVISIONING
    assert shop.memberships.get().user == user and shop.memberships.get().role == ProjectRole.OWNER
    assert sorted(s.project_id for s in syncs) == sorted(Project.objects.values_list("id", flat=True))


@responses.activate
def test_reconnecting_does_not_bring_back_deleted_projects(client, make_user, syncs):
    user = make_user()
    connect(client, user)
    Project.objects.get(github_repo_name="cart").delete()
    assert connect(client, user)["projects"] == "0"
    assert list(Project.objects.values_list("github_repo_name", flat=True)) == ["shop"]


@responses.activate
def test_off_by_default_creates_nothing(client, make_user, settings, syncs):
    settings.SRE_GITHUB_AUTO_PROJECTS = False
    assert "projects" not in connect(client, make_user())
    assert not Project.objects.exists()


def added(sender_id, repos=("acme/cart",), installation_id=42):
    return {"action": "added", "installation": {"id": installation_id}, "sender": {"id": sender_id},
            "repositories_added": [{"full_name": r} for r in repos]}


def test_repos_added_later_get_projects_for_the_sender(client, make_user, syncs):
    alice = make_user(email="alice@example.com", github_id="111")
    bob = make_user(email="bob@example.com", github_id="222")
    for u in (alice, bob):
        GitHubInstallation.objects.create(user=u, installation_id="42", account_login="acme")

    resp = post_github(client, added(222), event="installation_repositories")
    assert resp.status_code == 200
    cart = Project.objects.get()
    assert cart.memberships.get().user == bob
    assert cart.github_default_branch == "main"
    # Redelivered: nothing new.
    assert post_github(client, added(222), event="installation_repositories").status_code == 202
    assert Project.objects.count() == 1


def test_installation_events_need_one_clear_owner(client, make_user, syncs):
    # Nobody connected yet: the connect callback creates them instead.
    assert post_github(client, added(111), event="installation_repositories").status_code == 202
    for email in ("a@example.com", "b@example.com"):
        GitHubInstallation.objects.create(user=make_user(email=email), installation_id="42",
                                          account_login="acme")
    # Two connected users, and the sender is neither.
    assert post_github(client, added(999), event="installation_repositories").status_code == 202
    assert not Project.objects.exists()


def test_installation_created_event_and_ignored_actions(client, make_user, syncs):
    user = make_user()
    GitHubInstallation.objects.create(user=user, installation_id="42", account_login="acme")
    removed = {"action": "removed", "installation": {"id": 42}, "repositories_removed": []}
    assert post_github(client, removed, event="installation_repositories").status_code == 202

    created = {"action": "created", "installation": {"id": 42}, "sender": {"id": 5},
               "repositories": [{"full_name": "acme/shop"}]}
    assert post_github(client, created, event="installation").status_code == 200
    assert Project.objects.get().github_default_branch == "trunk"


def test_service_name_already_used_in_the_org_is_left_empty(client, make_user, make_project, syncs):
    user = make_user()
    make_project(user, name="old", service_names=["cart"])
    GitHubInstallation.objects.create(user=user, installation_id="42", account_login="acme")
    post_github(client, added(None), event="installation_repositories")
    assert Project.objects.get(github_installation_id="42").service_names == []


# ---- GET /sre/cli/project ------------------------------------------------------

def test_cli_project_returns_dsn_and_otlp_endpoint(api_for, make_user, make_project):
    user = make_user()
    project = make_project(user, service_names=["shop-api"], uptrace_managed=True,
                           uptrace_status=UptraceStatus.READY,
                           uptrace_dsn_encrypted=encrypt("http://tok@uptrace.internal:14318?grpc=14317"))
    body = api_for(user).get("/cli/project?repo=ACME/shop").json()
    assert body == {
        "project_id": project.id, "name": "shop", "repo": "acme/shop", "service_name": "shop-api",
        "uptrace_status": "ready", "dsn": "https://tok@uptrace.example.test?grpc=14317",
        "otlp_endpoint": "https://uptrace.example.test",
    }


def test_cli_project_before_uptrace_is_ready(api_for, make_user, make_project):
    user = make_user()
    make_project(user, uptrace_managed=True, uptrace_status=UptraceStatus.PROVISIONING)
    body = api_for(user).get("/cli/project?repo=acme/shop").json()
    assert (body["dsn"], body["otlp_endpoint"], body["service_name"]) == ("", "", "shop")


def test_cli_project_access_and_ambiguity(api_for, make_user, make_project, add_member):
    owner, viewer = make_user(), make_user(email="v@example.com")
    first = make_project(owner)
    add_member(first, viewer, ProjectRole.VIEWER)
    assert api_for(viewer).get("/cli/project?repo=acme/shop").status_code == 404
    assert api_for(owner).get("/cli/project?repo=acme/other").status_code == 404
    assert api_for(owner).get("/cli/project?repo=shop").status_code == 400

    second = make_project(owner, name="shop staging")
    resp = api_for(owner).get("/cli/project?repo=acme/shop")
    assert resp.status_code == 409
    assert [p["id"] for p in resp.json()["projects"]] == [first.id, second.id]
    assert api_for(owner).get(f"/cli/project?repo=acme/shop&project_id={second.id}").json()["name"] == "shop staging"
