import hashlib
import hmac
import json

import pytest
from django.utils import timezone

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
    calls = {"start": [], "signal": [], "reopen": []}
    monkeypatch.setattr(temporal_client, "start_incident_workflow",
                        lambda wid, inp: calls["start"].append((wid, inp)))
    monkeypatch.setattr(temporal_client, "signal_approval",
                        lambda wid, decision: calls["signal"].append((wid, decision)))
    monkeypatch.setattr(temporal_client, "signal_pull_request_reopened",
                        lambda wid: calls["reopen"].append(wid))
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


# What Uptrace's webhook notification channel actually posts (from its source:
# vue/src/alerting/NotifChannelWebhookForm.vue). No trace id, no headers, no signature.
def uptrace_alert(alert_id="123", event="created", state="open", name="AttributeError: 'NoneType'"):
    return {
        "id": "1676471814931265794", "eventName": event, "payload": {"env": "prod"},
        "createdAt": "2026-09-26T10:00:00Z",
        "alert": {"id": alert_id, "url": f"https://uptrace.example/alerting/1/alerts/{alert_id}",
                  "name": name, "type": "error", "state": state,
                  "createdAt": "2026-09-26T10:00:00Z"},
    }


def post_uptrace(client, project, body, token=None):
    token = project.uptrace_webhook_secret if token is None else token
    return client.post(f"/api/sre/webhooks/uptrace/{project.id}?token={token}",
                       data=json.dumps(body), content_type="application/json")


def test_real_uptrace_alert_with_url_token_starts_one_incident_per_alert(
        client, make_user, make_project, temporal_calls):
    project = make_project(make_user())
    first = post_uptrace(client, project, uptrace_alert())
    assert first.status_code == 200
    run = IncidentRun.objects.get(id=first.json()["incident_run_id"])
    assert run.trace_id == "uptrace-alert-123"
    assert run.raw_webhook_payload["alert"]["name"] == "AttributeError: 'NoneType'"
    # The same error recurring is the same Uptrace alert, so the same incident.
    again = post_uptrace(client, project, uptrace_alert(event="recurring"))
    assert again.json()["incident_run_id"] == run.id
    assert IncidentRun.objects.count() == 1


@pytest.mark.parametrize("event,state", [("state-changed", "closed"), ("created", "closed"),
                                         ("something-new", "open")])
def test_uptrace_closed_or_unknown_events_are_ignored(client, make_user, make_project,
                                                      temporal_calls, event, state):
    project = make_project(make_user())
    resp = post_uptrace(client, project, uptrace_alert(event=event, state=state))
    assert resp.status_code == 202
    assert not IncidentRun.objects.exists() and temporal_calls["start"] == []


def test_uptrace_url_token_must_match(client, make_user, make_project, temporal_calls):
    project = make_project(make_user())
    assert post_uptrace(client, project, uptrace_alert(), token="wrong").status_code == 401
    assert post_uptrace(client, project, uptrace_alert(), token="").status_code == 401


def test_first_alert_pins_the_uptrace_project_and_others_are_rejected(
        client, api_for, make_user, make_project, temporal_calls):
    project = make_project(make_user())
    assert post_uptrace(client, project, uptrace_alert("1")).status_code == 200
    project.refresh_from_db()
    assert project.uptrace_source_id == "uptrace.example/1"

    other_project = uptrace_alert("2")
    other_project["alert"]["url"] = "https://uptrace.example/alerting/7/alerts/2"
    other_instance = uptrace_alert("3")
    other_instance["alert"]["url"] = "https://other-uptrace.io/alerting/1/alerts/3"
    for body in (other_project, other_instance):
        resp = post_uptrace(client, project, body)
        assert resp.status_code == 409 and "uptrace.example/1" in resp.json()["detail"]
    assert IncidentRun.objects.count() == 1 and len(temporal_calls["start"]) == 1

    # The owner clears the pin; the next alert pins again.
    owner = api_for(project.memberships.get().user)
    assert owner.patch(f"/projects/{project.id}", {"uptrace_source_id": ""}).status_code == 200
    assert post_uptrace(client, project, other_project).status_code == 200
    project.refresh_from_db()
    assert project.uptrace_source_id == "uptrace.example/7"


def test_unrecognised_alert_url_does_not_pin_but_is_refused_once_pinned(
        client, make_user, make_project, temporal_calls):
    project = make_project(make_user())
    odd = uptrace_alert("1")
    odd["alert"]["url"] = ""
    assert post_uptrace(client, project, odd).status_code == 200
    project.refresh_from_db()
    assert project.uptrace_source_id == ""
    assert post_uptrace(client, project, uptrace_alert("2")).status_code == 200  # pins now
    odd["alert"]["id"] = "3"
    assert post_uptrace(client, project, odd).status_code == 409


# Captured from Uptrace 2.1 (infra/uptrace/): the message it sends when a webhook channel
# is saved. Anything but a 2xx makes Uptrace refuse to save the channel.
UPTRACE_21_TEST_MESSAGE = {
    "id": "1790397819508021754", "eventName": "test", "payload": None,
    "createdAt": 1790397819508.021,
    "alert": {"id": "4789318915246968593",
              "url": "https://uptrace.buggly.dev/alerting/1/alerts/4789318915246968593/0",
              "name": "Test message", "attrs": {}, "type": "metric",
              "state": "unresolved", "status": "unresolved", "createdAt": 1790397819508.021},
}


def test_uptrace_21_test_message_is_accepted_and_ignored(client, make_user, make_project,
                                                         temporal_calls):
    project = make_project(make_user())
    resp = post_uptrace(client, project, UPTRACE_21_TEST_MESSAGE)
    assert resp.status_code == 202
    assert not IncidentRun.objects.exists() and temporal_calls["start"] == []
    project.refresh_from_db()
    assert project.uptrace_source_id == ""


@pytest.mark.parametrize("event", ["created", "recurring", "status_changed", "state_changed"])
def test_uptrace_21_unresolved_alert_starts_an_incident(client, make_user, make_project,
                                                        temporal_calls, event):
    project = make_project(make_user())
    body = {**UPTRACE_21_TEST_MESSAGE, "eventName": event}
    resp = post_uptrace(client, project, body)
    assert resp.status_code == 200
    assert IncidentRun.objects.get().trace_id == "uptrace-alert-4789318915246968593"
    project.refresh_from_db()
    assert project.uptrace_source_id == "uptrace.buggly.dev/1"


@pytest.mark.parametrize("status", ["resolved", "archived"])
def test_uptrace_21_resolved_alert_is_ignored(client, make_user, make_project, temporal_calls,
                                              status):
    project = make_project(make_user())
    body = {**UPTRACE_21_TEST_MESSAGE, "eventName": "status_changed",
            "alert": {**UPTRACE_21_TEST_MESSAGE["alert"], "state": status, "status": status}}
    assert post_uptrace(client, project, body).status_code == 202
    assert not IncidentRun.objects.exists()


def _reopened(alert_id="123", event="state_changed"):
    body = uptrace_alert(alert_id, event=event, state="unresolved")
    body["alert"]["status"] = "unresolved"
    return body


def _finish(run_id, status=IncidentRun.Status.FAILED):
    IncidentRun.objects.filter(id=run_id).update(status=status)


@pytest.mark.parametrize("event", ["state_changed", "status_changed"])
def test_reopened_alert_starts_a_new_incident_once_the_last_one_finished(
        client, make_user, make_project, temporal_calls, event):
    project = make_project(make_user())
    first = post_uptrace(client, project, uptrace_alert()).json()
    _finish(first["incident_run_id"], IncidentRun.Status.REJECTED)

    second = post_uptrace(client, project, _reopened(event=event))
    assert second.status_code == 200
    run = IncidentRun.objects.get(id=second.json()["incident_run_id"])
    assert run.id != first["incident_run_id"]
    assert run.trace_id == "uptrace-alert-123-r2"
    assert run.temporal_workflow_id == f"sre-incident-{project.id}-uptrace-alert-123-r2"
    assert temporal_calls["start"][-1][0] == run.temporal_workflow_id

    # Uptrace redelivering the same reopen is the same incident.
    again = post_uptrace(client, project, _reopened(event=event))
    assert again.json()["incident_run_id"] == run.id

    _finish(run.id)
    third = post_uptrace(client, project, _reopened(event=event)).json()
    assert IncidentRun.objects.get(id=third["incident_run_id"]).trace_id == "uptrace-alert-123-r3"
    assert IncidentRun.objects.count() == 3


@pytest.mark.parametrize("status", ["running", "awaiting_approval"])
def test_reopen_while_the_incident_is_still_active_is_the_same_incident(
        client, make_user, make_project, temporal_calls, status):
    project = make_project(make_user())
    first = post_uptrace(client, project, uptrace_alert()).json()
    _finish(first["incident_run_id"], status)
    assert post_uptrace(client, project, _reopened()).json()["incident_run_id"] == first["incident_run_id"]
    assert IncidentRun.objects.count() == 1


def test_recurring_alert_after_its_incident_finished_does_not_start_another(
        client, make_user, make_project, temporal_calls):
    project = make_project(make_user())
    first = post_uptrace(client, project, uptrace_alert()).json()
    _finish(first["incident_run_id"])
    again = post_uptrace(client, project, uptrace_alert(event="recurring"))
    assert again.json()["incident_run_id"] == first["incident_run_id"]
    assert IncidentRun.objects.count() == 1


def test_reopen_keys_do_not_collide_across_alerts_or_projects(
        client, make_user, make_project, temporal_calls):
    user = make_user()
    project, other = make_project(user), make_project(user)
    for alert_id in ("12", "123"):
        _finish(post_uptrace(client, project, uptrace_alert(alert_id)).json()["incident_run_id"])
    reopened = post_uptrace(client, project, _reopened("12")).json()
    assert IncidentRun.objects.get(id=reopened["incident_run_id"]).trace_id == "uptrace-alert-12-r2"
    # Another project's first alert with the same Uptrace id is its own first incident.
    fresh = post_uptrace(client, other, _reopened("12")).json()
    assert IncidentRun.objects.get(id=fresh["incident_run_id"]).trace_id == "uptrace-alert-12"


def test_webhook_needs_an_alert_or_a_trace_id(client, make_user, make_project, temporal_calls):
    project = make_project(make_user())
    assert post_uptrace(client, project, {"payload": {"x": 1}}).status_code == 422
    assert not IncidentRun.objects.exists()


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
    assert body["uptrace_webhook_url"] == f"{body['webhook_url']}?token={body['webhook_secret']}"

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
    assert body["usage"] == {"calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
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


GITHUB_SECRET = "gh-webhook-secret"


def post_github(client, body, event="pull_request", secret=GITHUB_SECRET):
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/api/sre/github/webhook", data=raw, content_type="application/json",
                       HTTP_X_HUB_SIGNATURE_256=signature, HTTP_X_GITHUB_EVENT=event)


def pr_closed(playbook_run, merged, repo="acme/shop"):
    return {"action": "closed", "repository": {"full_name": repo},
            "pull_request": {"merged": merged, "html_url": "https://github.com/acme/shop/pull/9",
                             "head": {"ref": playbook_run.branch_name}}}


@pytest.fixture
def github_secret(settings):
    settings.GITHUB_APP_WEBHOOK_SECRET = GITHUB_SECRET


def pending_with_branch(project, **kwargs):
    playbook_run = make_pending_run(project, **kwargs)
    playbook_run.branch_name = "sre/incident-1-a1"
    playbook_run.save()
    return playbook_run


@pytest.mark.parametrize("merged", [True, False])
def test_github_merge_approves_and_close_rejects(client, make_user, make_project, temporal_calls,
                                                 github_secret, merged):
    playbook_run = pending_with_branch(make_project(make_user()))
    resp = post_github(client, pr_closed(playbook_run, merged))
    assert resp.status_code == 200
    wid, decision = temporal_calls["signal"][0]
    assert wid == playbook_run.incident_run.temporal_workflow_id
    assert (decision.approve, decision.via_github, decision.user_id) == (merged, True, 0)
    playbook_run.refresh_from_db()
    assert playbook_run.approved_at is not None and playbook_run.approved_by is None
    assert playbook_run.pr_url == "https://github.com/acme/shop/pull/9"

    # A redelivery (or a later in-app decision) doesn't decide twice.
    assert post_github(client, pr_closed(playbook_run, merged)).status_code == 202
    assert len(temporal_calls["signal"]) == 1


def test_github_webhook_rejects_bad_signature_and_unconfigured(client, make_user, make_project,
                                                               temporal_calls, settings):
    playbook_run = pending_with_branch(make_project(make_user()))
    settings.GITHUB_APP_WEBHOOK_SECRET = ""
    assert post_github(client, pr_closed(playbook_run, True)).status_code == 503
    settings.GITHUB_APP_WEBHOOK_SECRET = GITHUB_SECRET
    assert post_github(client, pr_closed(playbook_run, True), secret="wrong").status_code == 401
    assert temporal_calls["signal"] == []


def test_github_webhook_ignores_other_events_and_prs(client, make_user, make_project,
                                                    temporal_calls, github_secret):
    project = make_project(make_user())
    playbook_run = pending_with_branch(project)
    body = pr_closed(playbook_run, True)
    assert post_github(client, {"zen": "hi"}, event="ping").status_code == 202
    assert post_github(client, {**body, "action": "opened"}).status_code == 202
    assert post_github(client, pr_closed(playbook_run, True, repo="other/repo")).status_code == 202
    body["pull_request"]["head"]["ref"] = "someone-elses-branch"
    assert post_github(client, body).status_code == 202
    # Autonomous runs don't wait for a decision.
    PlaybookRun.objects.filter(id=playbook_run.id).update(execution_mode="autonomous")
    assert post_github(client, pr_closed(playbook_run, True)).status_code == 202
    assert temporal_calls["signal"] == []


def test_github_decision_before_run_is_pending_still_counts(client, make_user, make_project,
                                                             temporal_calls, github_secret):
    playbook_run = pending_with_branch(make_project(make_user()), status=PlaybookRun.Status.RUNNING)
    assert post_github(client, pr_closed(playbook_run, True)).status_code == 200
    assert len(temporal_calls["signal"]) == 1


def test_github_decision_is_released_if_signal_fails(client, make_user, make_project,
                                                     monkeypatch, github_secret):
    def down(*args):
        raise RuntimeError("unreachable")

    monkeypatch.setattr(temporal_client, "signal_approval", down)
    playbook_run = pending_with_branch(make_project(make_user()))
    assert post_github(client, pr_closed(playbook_run, True)).status_code == 503
    playbook_run.refresh_from_db()
    assert playbook_run.approved_at is None  # a redelivery can decide


def rejected_run(project):
    playbook_run = pending_with_branch(project, status=PlaybookRun.Status.REJECTED)
    playbook_run.approved_at = timezone.now()
    playbook_run.save()
    return playbook_run


@pytest.mark.parametrize("action", ["reopened", "opened"])
def test_github_reopen_puts_a_rejected_run_back_up_for_review(client, make_user, make_project,
                                                              temporal_calls, github_secret, action):
    playbook_run = rejected_run(make_project(make_user()))
    resp = post_github(client, {**pr_closed(playbook_run, False), "action": action})
    assert resp.status_code == 200
    assert temporal_calls["reopen"] == [playbook_run.incident_run.temporal_workflow_id]
    playbook_run.refresh_from_db()
    assert playbook_run.approved_at is None  # undecided again


def test_github_merge_of_a_rejected_run_approves(client, make_user, make_project, temporal_calls,
                                                 github_secret):
    playbook_run = rejected_run(make_project(make_user()))
    assert post_github(client, pr_closed(playbook_run, True)).status_code == 200
    assert temporal_calls["signal"][0][1].approve is True
    # ...but closing it again unmerged is nothing new.
    assert post_github(client, pr_closed(playbook_run, False)).status_code == 202


def test_github_reopen_ignored_unless_rejected(client, make_user, make_project, temporal_calls,
                                               github_secret):
    project = make_project(make_user())
    pending = pending_with_branch(project)
    assert post_github(client, {**pr_closed(pending, False), "action": "reopened"}).status_code == 202
    for status in (PlaybookRun.Status.SUCCEEDED, PlaybookRun.Status.FAILED):
        PlaybookRun.objects.filter(id=pending.id).update(status=status)
        assert post_github(client, {**pr_closed(pending, False), "action": "reopened"}).status_code == 202
        assert post_github(client, pr_closed(pending, True)).status_code == 202
    assert temporal_calls["reopen"] == [] and temporal_calls["signal"] == []


def test_github_event_for_a_finished_workflow_is_ignored(client, make_user, make_project,
                                                         monkeypatch, github_secret):
    def finished(wid):
        raise temporal_client.WorkflowFinished(wid)

    monkeypatch.setattr(temporal_client, "signal_pull_request_reopened", finished)
    playbook_run = rejected_run(make_project(make_user()))
    before = playbook_run.approved_at
    resp = post_github(client, {**pr_closed(playbook_run, False), "action": "reopened"})
    assert resp.status_code == 202 and "finished" in resp.json()["detail"]
    playbook_run.refresh_from_db()
    assert playbook_run.approved_at == before  # claim released
