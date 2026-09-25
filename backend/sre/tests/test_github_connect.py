import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
import responses

from sre.models import GitHubInstallation, ProjectRole
from sre.services import github_connect

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def app_settings(settings):
    settings.GITHUB_APP_SLUG = "sre-app"
    settings.GITHUB_APP_CLIENT_ID = "Iv1.client"
    settings.GITHUB_APP_CLIENT_SECRET = "secret"
    settings.FRONTEND_URL = "http://frontend"
    return settings


@pytest.fixture
def repos(monkeypatch):
    listed = {"42": [{"owner": "acme", "name": "shop", "default_branch": "main", "private": True}]}
    monkeypatch.setattr(github_connect, "installation_repos", lambda i: listed.get(i, []))
    return listed


def callback(client, state, code="c0de"):
    resp = client.get("/api/sre/github/callback", {"code": code, "state": state})
    assert resp.status_code == 302
    url = urlparse(resp["Location"])
    assert url.path == "/settings"
    return {k: v[0] for k, v in parse_qs(url.query).items()}


def mock_github(installations):
    responses.post("https://github.com/login/oauth/access_token", json={"access_token": "user-tok"})
    responses.get("https://api.github.com/user/installations", json={"installations": installations})


def installation(id, login, app_slug="sre-app"):
    return {"id": id, "app_slug": app_slug, "account": {"login": login, "type": "Organization"}}


def test_connect_returns_install_and_authorize_urls_with_state(api_for, make_user):
    user = make_user()
    body = api_for(user).post("/github/connect").json()
    assert body["install_url"].startswith("https://github.com/apps/sre-app/installations/new?state=")
    state = parse_qs(urlparse(body["authorize_url"]).query)["state"][0]
    assert github_connect.verify_state(state) == user.id


def test_connect_is_400_when_app_not_configured(api_for, make_user, settings):
    settings.GITHUB_APP_CLIENT_SECRET = ""
    assert api_for(make_user()).post("/github/connect").status_code == 400
    assert api_for(make_user(email="b@example.com")).get("/github/status").json()["configured"] is False


@responses.activate
def test_callback_saves_only_our_apps_installations_and_drops_stale(client, make_user):
    user = make_user()
    GitHubInstallation.objects.create(user=user, installation_id="7", account_login="gone")
    mock_github([installation(42, "acme"), installation(99, "other", app_slug="someone-elses-app")])

    params = callback(client, github_connect.make_state(user.id))
    assert params == {"tab": "github", "github": "connected", "count": "1"}
    assert list(user.github_installations.values_list("installation_id", "account_login")) == [("42", "acme")]


@pytest.mark.parametrize("kind", ["garbage", "wrong_key", "wrong_purpose", "expired"])
@responses.activate
def test_callback_rejects_bad_state_without_writing(client, make_user, settings, kind):
    user = make_user()
    mock_github([installation(42, "acme")])
    claims = {"purpose": github_connect.STATE_PURPOSE, "user_id": user.id, "iat": int(time.time())}
    key = settings.SECRET_KEY
    if kind == "wrong_key":
        key = "not-the-secret-key-but-long-enough-for-hs256"
    elif kind == "wrong_purpose":
        claims["purpose"] = "github"  # a login state must not work here
    elif kind == "expired":
        claims["iat"] = int(time.time()) - github_connect.STATE_TTL_SECONDS - 1
    state = "garbage" if kind == "garbage" else jwt.encode(claims, key, "HS256")

    params = callback(client, state)
    assert params["github_error"] == ("state_expired" if kind == "expired" else "invalid_state")
    assert not GitHubInstallation.objects.exists()
    assert len(responses.calls) == 0


def test_callback_without_code_is_an_error(client, make_user):
    user = make_user()
    params = callback(client, github_connect.make_state(user.id), code="")
    assert params["github_error"] == "authorization_missing"


@responses.activate
def test_callback_token_exchange_failure(client, make_user):
    user = make_user()
    responses.post("https://github.com/login/oauth/access_token", json={"error": "bad_verification_code"})
    params = callback(client, github_connect.make_state(user.id))
    assert params["github_error"] == "token_exchange_failed"


def test_create_project_requires_a_connected_installation_and_reachable_repo(api_for, make_user, repos):
    user = make_user()
    api = api_for(user)
    body = {"name": "shop", "github_installation_id": "42", "github_repo_owner": "acme",
            "github_repo_name": "shop"}
    assert api.post("/projects", body).status_code == 400  # not connected

    GitHubInstallation.objects.create(user=user, installation_id="42", account_login="acme")
    assert api.post("/projects", {**body, "github_repo_name": "secret"}).status_code == 400
    resp = api.post("/projects", body)
    assert resp.status_code == 201
    assert resp.json()["github_verified"] is True


def test_existing_projects_are_grandfathered_but_changes_are_checked(api_for, make_user, make_project, repos):
    owner = make_user()
    project = make_project(owner)  # installation "1", never verified
    api = api_for(owner)
    assert api.get(f"/projects/{project.id}").json()["github_verified"] is False
    assert api.patch(f"/projects/{project.id}", {"name": "renamed"}).status_code == 200
    # Resending the unchanged wiring is fine; changing it needs proof.
    assert api.patch(f"/projects/{project.id}", {"github_installation_id": "1"}).status_code == 200
    assert api.patch(f"/projects/{project.id}", {"github_installation_id": "42"}).status_code == 400

    GitHubInstallation.objects.create(user=owner, installation_id="42", account_login="acme")
    resp = api.patch(f"/projects/{project.id}", {"github_installation_id": "42"})
    assert resp.status_code == 200 and resp.json()["github_verified"] is True


def test_installations_are_private_to_their_user(api_for, make_user, repos):
    mine = GitHubInstallation.objects.create(user=make_user(email="me@example.com"),
                                             installation_id="42", account_login="acme")
    other = api_for(make_user(email="other@example.com"))
    assert other.get("/github/installations").json() == []
    assert other.get(f"/github/installations/{mine.id}/repos").status_code == 404
    assert other.delete(f"/github/installations/{mine.id}").status_code == 404

    me = api_for(mine.user)
    assert me.get(f"/github/installations/{mine.id}/repos").json()[0]["name"] == "shop"
    assert me.delete(f"/github/installations/{mine.id}").status_code == 204


def test_verified_needs_an_owner_not_just_any_member(api_for, make_user, make_project, add_member):
    owner, viewer = make_user(email="o@example.com"), make_user(email="v@example.com")
    project = make_project(owner)
    add_member(project, viewer, ProjectRole.VIEWER)
    GitHubInstallation.objects.create(user=viewer, installation_id="1", account_login="acme")
    assert api_for(owner).get(f"/projects/{project.id}").json()["github_verified"] is False
