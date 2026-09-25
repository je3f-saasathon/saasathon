import hashlib
import hmac
import json

import pytest

from sre import temporal_client
from sre.models import (
    IncidentRun,
    LLMUsage,
    LLMProviderConfig,
    LLMStepOverride,
    Playbook,
    PlaybookRun,
    ProjectRole,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def temporal_calls(monkeypatch):
    calls = {"start": [], "signal": []}
    monkeypatch.setattr(temporal_client, "start_incident_workflow",
                        lambda wid, inp: calls["start"].append((wid, inp)))
    monkeypatch.setattr(temporal_client, "signal_approval",
                        lambda wid, decision: calls["signal"].append((wid, decision)))
    return calls


def post_webhook(client, project, body, signature=None, token=None):
    raw = json.dumps(body)
    headers = {}
    if signature is not None:
        headers["HTTP_X_SRE_SIGNATURE"] = signature
    elif token is not None:
        headers["HTTP_X_SRE_WEBHOOK_SECRET"] = token
    else:
        digest = hmac.new(project.uptrace_webhook_secret.encode(), raw.encode(), hashlib.sha256)
        headers["HTTP_X_SRE_SIGNATURE"] = "sha256=" + digest.hexdigest()
    return client.post(f"/api/sre/webhooks/uptrace/{project.id}", data=raw,
                       content_type="application/json", **headers)


# ---- webhook -------------------------------------------------------------------

def test_webhook_with_valid_hmac_starts_workflow(client, make_user, make_project, temporal_calls):
    project = make_project(make_user())
    resp = post_webhook(client, project, {"trace_id": "abc", "payload": {"msg": "boom"}})
    assert resp.status_code == 200
    run = IncidentRun.objects.get(id=resp.json()["incident_run_id"])
    assert run.temporal_workflow_id == f"sre-incident-{project.id}-abc"
    assert run.raw_webhook_payload["payload"] == {"msg": "boom"}
    assert temporal_calls["start"][0][0] == run.temporal_workflow_id


def test_webhook_accepts_plain_secret_header(client, make_user, make_project, temporal_calls):
    project = make_project(make_user())
    resp = post_webhook(client, project, {"trace_id": "abc"}, token=project.uptrace_webhook_secret)
    assert resp.status_code == 200


@pytest.mark.parametrize("signature", ["sha256=deadbeef", "", "md5=whatever"])
def test_webhook_rejects_bad_or_missing_signature(client, make_user, make_project, temporal_calls, signature):
    project = make_project(make_user())
    resp = post_webhook(client, project, {"trace_id": "abc"}, signature=signature)
    assert resp.status_code == 401
    assert not IncidentRun.objects.exists()
    assert temporal_calls["start"] == []


def test_webhook_unknown_project_looks_like_bad_signature(client, temporal_calls):
    resp = client.post("/api/sre/webhooks/uptrace/9999", data='{"trace_id": "abc"}',
                       content_type="application/json",
                       HTTP_X_SRE_WEBHOOK_SECRET="x")
    assert resp.status_code == 401


def test_duplicate_webhook_returns_same_run(client, make_user, make_project, temporal_calls):
    project = make_project(make_user())
    first = post_webhook(client, project, {"trace_id": "dup"})
    second = post_webhook(client, project, {"trace_id": "dup"})
    assert first.status_code == second.status_code == 200
    assert first.json()["incident_run_id"] == second.json()["incident_run_id"]
    assert IncidentRun.objects.count() == 1


def test_webhook_returns_503_when_temporal_is_down(client, make_user, make_project, monkeypatch):
    def down(*args):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(temporal_client, "start_incident_workflow", down)
    project = make_project(make_user())
    resp = post_webhook(client, project, {"trace_id": "abc"})
    assert resp.status_code == 503


# ---- projects & membership ---------------------------------------------------------

def test_create_project_returns_secret_once(api_for, make_user, monkeypatch):
    from sre.models import GitHubInstallation
    from sre.services import github_connect
    user = make_user()
    GitHubInstallation.objects.create(user=user, installation_id="1", account_login="acme")
    monkeypatch.setattr(github_connect, "installation_repos",
                        lambda _id: [{"owner": "acme", "name": "shop", "default_branch": "main",
                                      "private": True}])
    api = api_for(user)
    resp = api.post("/projects", {"name": "shop", "github_installation_id": "1",
                                  "github_repo_owner": "acme", "github_repo_name": "shop"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "owner"
    assert body["default_execution_mode"] == "draft_only"
    assert body["webhook_secret"]
    assert body["webhook_url"].endswith(f"/api/sre/webhooks/uptrace/{body['id']}")

    listed = api.get("/projects").json()
    assert "webhook_secret" not in listed[0]


def test_admin_can_toggle_test_generation(api_for, make_user, make_project, add_member):
    owner, admin = make_user(email="o@example.com"), make_user(email="a@example.com")
    project = make_project(owner)
    add_member(project, admin, ProjectRole.ADMIN)
    assert api_for(owner).get(f"/projects/{project.id}").json()["generate_tests"] is True
    resp = api_for(admin).patch(f"/projects/{project.id}", {"generate_tests": False})
    assert resp.status_code == 200 and resp.json()["generate_tests"] is False
    # An unrelated update leaves it alone.
    assert api_for(admin).patch(f"/projects/{project.id}", {"name": "x"}).json()["generate_tests"] is False


def test_non_member_gets_404(api_for, make_user, make_project):
    project = make_project(make_user(email="owner@example.com"))
    outsider = api_for(make_user(email="outsider@example.com"))
    assert outsider.get(f"/projects/{project.id}").status_code == 404
    assert outsider.get(f"/projects/{project.id}/playbooks").status_code == 404


def test_roles_gate_actions(api_for, make_user, make_project, add_member):
    owner = make_user(email="owner@example.com")
    admin = make_user(email="admin@example.com")
    viewer = make_user(email="viewer@example.com")
    project = make_project(owner)
    add_member(project, admin, ProjectRole.ADMIN)
    add_member(project, viewer, ProjectRole.VIEWER)

    assert api_for(viewer).get(f"/projects/{project.id}").status_code == 200
    assert api_for(viewer).patch(f"/projects/{project.id}", {"name": "x"}).status_code == 403
    assert api_for(admin).patch(f"/projects/{project.id}", {"default_execution_mode": "autonomous"}).status_code == 200
    # repo wiring is owner-only
    assert api_for(admin).patch(f"/projects/{project.id}", {"github_repo_name": "evil"}).status_code == 403
    assert api_for(admin).post(f"/projects/{project.id}/members",
                               {"email": "someone@example.com", "role": "viewer"}).status_code == 403
    assert api_for(admin).post(f"/projects/{project.id}/webhook-secret/rotate").status_code == 403


def test_owner_manages_members_and_last_owner_is_protected(api_for, make_user, make_project):
    owner = make_user(email="owner@example.com")
    make_user(email="friend@example.com")
    project = make_project(owner)
    api = api_for(owner)

    resp = api.post(f"/projects/{project.id}/members", {"email": "Friend@Example.com", "role": "admin"})
    assert resp.status_code == 201
    friend_id = resp.json()["user_id"]
    assert api.post(f"/projects/{project.id}/members",
                    {"email": "friend@example.com", "role": "admin"}).status_code == 409
    assert api.post(f"/projects/{project.id}/members",
                    {"email": "nobody@example.com", "role": "admin"}).status_code == 404

    assert api.patch(f"/projects/{project.id}/members/{owner.id}", {"role": "viewer"}).status_code == 409
    assert api.delete(f"/projects/{project.id}/members/{owner.id}").status_code == 409
    assert api.patch(f"/projects/{project.id}/members/{friend_id}", {"role": "owner"}).status_code == 200
    # now there are two owners, so the original can step down
    assert api.patch(f"/projects/{project.id}/members/{owner.id}", {"role": "viewer"}).status_code == 200


def test_member_can_leave_and_their_configs_are_detached(api_for, make_user, make_project, add_member):
    owner = make_user(email="owner@example.com")
    admin = make_user(email="admin@example.com")
    project = make_project(owner)
    add_member(project, admin, ProjectRole.ADMIN)
    admin_api = api_for(admin)

    config_id = admin_api.post("/llm-configs", {"name": "mine", "provider": "anthropic",
                                                 "model": "claude-sonnet-5", "api_key": "sk-1"}).json()["id"]
    assert admin_api.patch(f"/projects/{project.id}", {"default_llm_config_id": config_id}).status_code == 200
    assert admin_api.put(f"/projects/{project.id}/step-overrides",
                         {"overrides": {"playbook_execution": config_id}}).status_code == 200

    assert admin_api.delete(f"/projects/{project.id}/members/{admin.id}").status_code == 204
    project.refresh_from_db()
    assert project.default_llm_config_id is None
    assert not LLMStepOverride.objects.filter(project=project).exists()
    assert LLMProviderConfig.objects.filter(id=config_id).exists()  # still the admin's config


# ---- LLM configs -------------------------------------------------------------------

def test_api_key_is_encrypted_and_never_returned(api_for, make_user):
    api = api_for(make_user())
    resp = api.post("/llm-configs", {"name": "c", "provider": "anthropic", "model": "m",
                                     "api_key": "sk-super-secret"})
    assert resp.status_code == 201
    assert "sk-super-secret" not in resp.content.decode()
    assert resp.json()["has_api_key"] is True
    assert "sk-super-secret" not in api.get("/llm-configs").content.decode()
    stored = LLMProviderConfig.objects.get()
    assert b"sk-super-secret" not in bytes(stored.api_key_encrypted)

    from sre.crypto import decrypt
    assert decrypt(stored.api_key_encrypted) == "sk-super-secret"


@pytest.mark.parametrize("url", ["http://api.example.com", "https://localhost:11434",
                                 "https://127.0.0.1", "https://10.0.0.5", "https://169.254.169.254"])
def test_private_or_plain_http_base_url_rejected(api_for, make_user, url):
    resp = api_for(make_user()).post("/llm-configs", {"name": "c", "provider": "self_hosted",
                                                      "model": "m", "base_url": url})
    assert resp.status_code == 400


def test_private_base_url_allowed_when_flag_set(api_for, make_user, settings):
    settings.SRE_ALLOW_PRIVATE_LLM_URLS = True
    resp = api_for(make_user()).post("/llm-configs", {"name": "local", "provider": "self_hosted",
                                                      "model": "llama", "base_url": "http://localhost:11434/v1"})
    assert resp.status_code == 201


def test_cannot_attach_another_users_config(api_for, make_user, make_project):
    owner = make_user(email="owner@example.com")
    stranger = make_user(email="stranger@example.com")
    project = make_project(owner)
    theirs = LLMProviderConfig.objects.create(owner=stranger, name="x", provider="openai", model="m")
    api = api_for(owner)
    assert api.patch(f"/projects/{project.id}", {"default_llm_config_id": theirs.id}).status_code == 404
    assert api.put(f"/projects/{project.id}/step-overrides",
                   {"overrides": {"bug_classification": theirs.id}}).status_code == 404


def test_jev_rejected_for_steps_it_cannot_run(api_for, make_user, make_project):
    owner = make_user()
    project = make_project(owner)
    api = api_for(owner)
    jev = api.post("/llm-configs", {"name": "jev", "provider": "jev_cloudflare", "model": "typesafe/jev"}).json()
    assert api.put(f"/projects/{project.id}/step-overrides",
                   {"overrides": {"bug_classification": jev["id"]}}).status_code == 200
    assert api.put(f"/projects/{project.id}/step-overrides",
                   {"overrides": {"playbook_execution": jev["id"]}}).status_code == 400


# ---- playbooks ---------------------------------------------------------------------

def test_playbook_steps_drop_pr_and_unknown_types(api_for, make_user, make_project):
    owner = make_user()
    project = make_project(owner)
    resp = api_for(owner).post(f"/projects/{project.id}/playbooks", {
        "title": "Fix pool exhaustion",
        "keywords": ["Timeout", " db "],
        "steps": [
            {"type": "edit_file", "path": "db.py", "instructions": "raise pool size"},
            {"type": "create_pr", "title": "sneaky"},
            {"type": "run_command", "command": "pytest"},
            {"type": "deploy"},
        ],
    })
    assert resp.status_code == 201
    body = resp.json()
    assert [s["type"] for s in body["steps"]] == ["edit_file", "run_command"]
    assert body["keywords"] == ["timeout", "db"]
    assert body["status"] == "confirmed"


def test_confirming_failing_playbook_resets_streak(api_for, make_user, make_project):
    owner = make_user()
    project = make_project(owner)
    playbook = Playbook.objects.create(project=project, title="p", status="failing",
                                       consecutive_failure_count=3)
    resp = api_for(owner).patch(f"/playbooks/{playbook.id}", {"status": "confirmed"})
    assert resp.status_code == 200
    assert resp.json()["consecutive_failure_count"] == 0


# ---- incident runs & approval ---------------------------------------------------------

def make_pending_run(project, status=PlaybookRun.Status.PENDING_APPROVAL):
    run = IncidentRun.objects.create(project=project, trace_id="t",
                                     temporal_workflow_id=f"sre-incident-{project.id}-t",
                                     status=IncidentRun.Status.AWAITING_APPROVAL)
    playbook = Playbook.objects.create(project=project, title="p")
    return PlaybookRun.objects.create(incident_run=run, playbook=playbook,
                                      execution_mode="draft_only", status=status)


def test_incident_runs_are_scoped_to_member_projects(api_for, make_user, make_project):
    mine = make_project(make_user(email="me@example.com"), name="mine")
    theirs = make_project(make_user(email="them@example.com"), name="theirs")
    IncidentRun.objects.create(project=mine, trace_id="a", temporal_workflow_id="w-a")
    other = IncidentRun.objects.create(project=theirs, trace_id="b", temporal_workflow_id="w-b")
    api = api_for(mine.memberships.get().user)
    body = api.get("/incident-runs").json()
    assert body["total"] == 1 and body["runs"][0]["trace_id"] == "a"
    assert api.get(f"/incident-runs/{other.id}").status_code == 404


def test_incident_run_includes_pr_playbook_and_usage(api_for, make_user, make_project):
    project = make_project(make_user(), name="shop")
    playbook_run = make_pending_run(project, status=PlaybookRun.Status.SUCCEEDED)
    run = playbook_run.incident_run
    run.matched_playbook = playbook_run.playbook
    run.save()
    playbook_run.pr_url = "https://github.com/acme/shop/pull/1"
    playbook_run.save()
    for step, model, tokens in [("bug_classification", "small", (10, 2)),
                                ("playbook_execution", "big", (100, 20)),
                                ("playbook_execution", "big", (50, 5))]:
        LLMUsage.objects.create(incident_run=run, step=step, provider="openai", model=model,
                                input_tokens=tokens[0], output_tokens=tokens[1])

    body = api_for(project.memberships.get().user).get("/incident-runs").json()["runs"][0]
    assert body["project_name"] == "shop"
    assert body["pr_url"] == "https://github.com/acme/shop/pull/1"
    assert body["playbook_run_status"] == "succeeded" and body["execution_mode"] == "draft_only"
    assert body["playbook"] == {"id": playbook_run.playbook_id, "title": "p",
                                "status": "unconfirmed", "source": "matched"}
    usage = body["usage"]
    assert (usage["calls"], usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (3, 160, 27, 187)
    assert usage["models"] == ["small", "big"]
    execution = next(s for s in usage["by_step"] if s["step"] == "playbook_execution")
    assert (execution["calls"], execution["input_tokens"]) == (2, 150)


def test_incident_run_without_playbook_run_reports_created_playbook(api_for, make_user, make_project):
    project = make_project(make_user())
    run = IncidentRun.objects.create(project=project, trace_id="n", temporal_workflow_id="w-n",
                                     status=IncidentRun.Status.NEW_PLAYBOOK_CREATED)
    playbook = Playbook.objects.create(project=project, title="new", source_incident_run=run)
    body = api_for(project.memberships.get().user).get(f"/incident-runs/{run.id}").json()
    assert body["playbook"]["id"] == playbook.id and body["playbook"]["source"] == "created"
    assert body["pr_url"] == "" and body["playbook_run_status"] is None
    assert body["usage"] == {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                             "total_tokens": 0, "platform_tokens": 0, "models": [], "by_step": []}


def test_viewer_cannot_approve_admin_can(api_for, make_user, make_project, add_member, temporal_calls):
    owner = make_user(email="owner@example.com")
    admin = make_user(email="admin@example.com")
    viewer = make_user(email="viewer@example.com")
    project = make_project(owner)
    add_member(project, admin, ProjectRole.ADMIN)
    add_member(project, viewer, ProjectRole.VIEWER)
    playbook_run = make_pending_run(project)

    path = f"/playbook-runs/{playbook_run.id}/approve"
    assert api_for(viewer).post(path, {"approve": True}).status_code == 403
    resp = api_for(admin).post(path, {"approve": True})
    assert resp.status_code == 200
    assert resp.json()["approved_by_id"] == admin.id
    wid, decision = temporal_calls["signal"][0]
    assert wid == playbook_run.incident_run.temporal_workflow_id
    assert decision.approve is True and decision.user_id == admin.id

    # second decision on the same run is refused
    assert api_for(owner).post(path, {"approve": False}).status_code == 409
    assert len(temporal_calls["signal"]) == 1


def test_approve_requires_pending_state(api_for, make_user, make_project, temporal_calls):
    owner = make_user()
    playbook_run = make_pending_run(make_project(owner), status=PlaybookRun.Status.RUNNING)
    resp = api_for(owner).post(f"/playbook-runs/{playbook_run.id}/approve", {"approve": True})
    assert resp.status_code == 409


def test_approve_is_released_if_signal_fails(api_for, make_user, make_project, monkeypatch):
    def down(*args):
        raise RuntimeError("unreachable")

    monkeypatch.setattr(temporal_client, "signal_approval", down)
    owner = make_user()
    playbook_run = make_pending_run(make_project(owner))
    resp = api_for(owner).post(f"/playbook-runs/{playbook_run.id}/approve", {"approve": True})
    assert resp.status_code == 503
    playbook_run.refresh_from_db()
    assert playbook_run.approved_at is None  # can be retried


def test_sre_endpoints_require_auth(client):
    assert client.get("/api/sre/projects").status_code == 401
