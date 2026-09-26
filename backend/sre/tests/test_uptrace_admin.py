"""Managed Uptrace: provisioning and sync against an in-memory Uptrace that behaves like
2.1's internal API (checked against infra/uptrace/), plus the project endpoints."""

import itertools
import re

import pytest

from sre import temporal_client
from sre.crypto import encrypt
from sre.models import Project, ProjectMembership, ProjectRole, UptraceStatus
from sre.services import uptrace_admin
from sre.services.uptrace import UptraceClient, resolve_credential

BASE = "https://api.example.test"


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = b"x" if body is not None else b""
        self.text = str(body)

    def json(self):
        return self._body


class FakeUptrace:
    """Uptrace 2.1's internal API, as much as the platform uses of it."""

    def __init__(self):
        self.ids = itertools.count(10)
        self.projects = {1: {"id": 1, "orgId": 7, "name": "existing"}}
        self.monitors = {}  # id -> monitor
        self.channels = {}  # id -> channel
        self.links = {}  # monitor id -> [channel ids]
        self.test_messages = []  # urls Uptrace "posted" a test message to
        self.unreachable = set()  # urls whose test message fails
        self.calls = []
        self.alerts = []

    def _new_project(self, name, org):
        pid = next(self.ids)
        self.projects[pid] = {"id": pid, "orgId": org, "name": name}
        # Uptrace gives every new project a default error monitor.
        self._monitor(pid, {"name": "Notify on all errors", "type": "error", "status": "active",
                            "params": {"query": "sum($logs) | group by _group_id"}})
        return pid

    def _monitor(self, pid, body):
        mid = next(self.ids)
        self.monitors[mid] = {**body, "id": mid, "projectId": pid, "channelIds": []}
        self.links.setdefault(mid, [])
        return mid

    def _save_channel(self, pid, body, cid=None):
        url = body["params"]["url"]
        self.test_messages.append(url)
        if url in self.unreachable:
            return 400, {"error": {"message": f'sendTestMessage failed: Post "{url}": timeout'}}
        cid = cid or next(self.ids)
        self.channels[cid] = {**body, "id": cid, "projectId": pid, "monitorIds": []}
        for mid, linked in self.links.items():
            if cid in linked:
                linked.remove(cid)
        for mid in body.get("monitorIds", []):
            self.links[mid].append(cid)
        return 200, {"channel": self.channels[cid]}

    def request(self, method, url, json=None, timeout=None, headers=None):
        assert headers["Authorization"] == "Bearer admin-token"
        path = url.split("/internal/v1", 1)[1]
        self.calls.append((method, path))
        status, body = self.route(method, path, json)
        return FakeResponse(status, body)

    def route(self, method, path, body):
        if (m := re.fullmatch(r"/users/current", path)) and method == "GET":
            return 200, {"user": {}, "projects": list(self.projects.values())}
        if m := re.fullmatch(r"/orgs/(\d+)/projects", path):
            org = int(m[1])
            if method == "GET":
                return 200, {"projects": [p for p in self.projects.values() if p["orgId"] == org]}
            return 200, {"project": self.projects[self._new_project(body["name"], org)]}
        if m := re.fullmatch(r"/projects/(\d+)/tokens", path):
            # Behind a TLS-terminating proxy Uptrace sees http, and builds the DSN from that.
            return 200, {"tokens": [{"id": 1, "dsn": f"http://secret-{m[1]}@uptrace.example.test?grpc=4317"}]}
        if m := re.fullmatch(r"/monitors/(\d+)", path):
            pid = int(m[1])
            if method == "GET":  # lists leave channelIds empty, like the real one
                return 200, {"monitors": [{**mo, "channelIds": []} for mo in self.monitors.values()
                                          if mo["projectId"] == pid]}
            return 200, {"monitor": self.monitors[self._monitor(pid, body)]}
        if m := re.fullmatch(r"/monitors/(\d+)/(\d+)/paused", path):
            self.monitors[int(m[2])]["status"] = "paused"
            return 200, {}
        if m := re.fullmatch(r"/monitors/(\d+)/(\d+)", path):
            mid = int(m[2])
            if method == "GET":
                return 200, {"monitor": {**self.monitors[mid], "channelIds": list(self.links[mid])}}
            if method == "PUT":
                self.monitors[mid].update({k: v for k, v in body.items() if k != "channelIds"})
                return 200, {"monitor": self.monitors[mid]}
            del self.monitors[mid], self.links[mid]
            return 200, {}
        if m := re.fullmatch(r"/projects/(\d+)/notification-channels", path):
            pid = int(m[1])
            if method == "GET":
                return 200, {"channels": [c for c in self.channels.values() if c["projectId"] == pid]}
            return self._save_channel(pid, body)
        if m := re.fullmatch(r"/projects/(\d+)/notification-channels/(\d+)", path):
            cid = int(m[2])
            if method == "PUT":
                return self._save_channel(int(m[1]), body, cid)
            del self.channels[cid]
            for linked in self.links.values():
                if cid in linked:
                    linked.remove(cid)
            return 200, {}
        if (m := re.fullmatch(r"/alerts/(\d+)\?limit=100", path)) and method == "GET":
            return 200, {"alerts": [a for a in self.alerts if a["projectId"] == int(m[1])]}
        if (m := re.fullmatch(r"/alerts/(\d+)/resolve", path)) and method == "PUT":
            for a in self.alerts:
                if a["id"] in body["ids"]:
                    a["event"]["status"] = "resolved"
            return 200, {}
        return 200, "<html>SPA</html>"  # unknown /internal/ routes: the SPA's HTML

    # helpers for assertions
    def ours(self, pid):
        monitors = {m["name"]: m for m in self.monitors.values()
                    if m["projectId"] == pid and m["name"].startswith("platform:")}
        channels = {c["name"]: c for c in self.channels.values()
                    if c["projectId"] == pid and c["name"].startswith("platform:")}
        return monitors, channels


@pytest.fixture
def uptrace(monkeypatch, settings):
    settings.UPTRACE_MANAGED_URL = "https://uptrace.example.test"
    settings.UPTRACE_MANAGED_API_URL = "https://uptrace.example.test"
    settings.UPTRACE_MANAGED_TOKEN = "admin-token"
    settings.UPTRACE_MANAGED_ORG_ID = 0
    fake = FakeUptrace()
    monkeypatch.setattr(uptrace_admin.requests, "request", fake.request)
    return fake


@pytest.fixture
def syncs(monkeypatch, uptrace):
    """Runs the sync workflow's steps inline instead of through Temporal."""
    started = []

    def run(inp):
        started.append(inp)
        targets = list(inp.uptrace_project_ids)
        project = Project.objects.filter(id=inp.project_id, uptrace_managed=True).first()
        if project is not None:
            targets.append(uptrace_admin.provision(project))
        for target in sorted(set(targets)):
            uptrace_admin.sync_group(target, inp.base_url)

    monkeypatch.setattr(temporal_client, "start_uptrace_sync", run)
    return started


@pytest.fixture
def repo_ok(monkeypatch):
    from sre import api
    monkeypatch.setattr(api, "_check_github_repo", lambda *a: None)


def create(api, **extra):
    body = {"name": extra.pop("name", "shop"), "github_installation_id": "1",
            "github_repo_owner": "acme", "github_repo_name": "shop", **extra}
    resp = api.post("/projects", body)
    assert resp.status_code == 201, resp.content
    return resp.json()


# ---- provisioning ----------------------------------------------------------------

def test_new_project_gets_its_own_uptrace_project_monitor_and_channel(
        api_for, make_user, uptrace, syncs, repo_ok):
    owner = api_for(make_user())
    out = create(owner)

    project = Project.objects.get(id=out["id"])
    upid = project.uptrace_project_id
    assert out["uptrace_managed"] and out["uptrace_status"] == "ready", out["uptrace_error"]
    assert uptrace.projects[upid]["name"].endswith(f"/ shop (#{project.id})")
    assert project.uptrace_source_id == f"uptrace.example.test/{upid}"
    assert out["uptrace_dsn"] == f"https://secret-{upid}@uptrace.example.test?grpc=4317"
    assert syncs[0].base_url == "http://testserver"

    monitors, channels = uptrace.ours(upid)
    monitor, channel = monitors[f"platform: project {project.id}"], channels[f"platform: project {project.id}"]
    assert '_event_name = "exception"' in monitor["params"]["query"]
    assert "service_name" not in monitor["params"]["query"]  # not shared: every error is ours
    assert channel["params"]["url"] == (f"http://testserver/api/sre/webhooks/uptrace/{project.id}"
                                        f"?token={project.uptrace_webhook_secret}")
    assert channel["matchAll"] is False and uptrace.links[monitor["id"]] == [channel["id"]]
    # Uptrace's own catch-all monitor would only duplicate ours.
    default = next(m for m in uptrace.monitors.values()
                   if m["projectId"] == upid and m["name"] == "Notify on all errors")
    assert default["status"] == "paused"


def test_sync_is_idempotent_and_does_not_resend_test_messages(api_for, make_user, uptrace, syncs, repo_ok):
    out = create(api_for(make_user()))
    project = Project.objects.get(id=out["id"])
    sent = len(uptrace.test_messages)
    writes = len([c for c in uptrace.calls if c[0] != "GET"])
    uptrace_admin.sync_group(project.uptrace_project_id, "http://testserver")
    assert len(uptrace.test_messages) == sent
    assert len([c for c in uptrace.calls if c[0] != "GET"]) == writes


def test_unreachable_webhook_is_reported_on_the_project(api_for, make_user, uptrace, syncs, repo_ok, monkeypatch):
    uptrace.unreachable = {"never-matches"}
    original = uptrace._save_channel

    def refuse(pid, body, cid=None):
        uptrace.unreachable.add(body["params"]["url"])
        return original(pid, body, cid)
    monkeypatch.setattr(uptrace, "_save_channel", refuse)
    out = create(api_for(make_user()))
    assert out["uptrace_status"] == "error"
    assert "couldn't deliver a test alert to http://testserver" in out["uptrace_error"]


def test_uptrace_down_marks_the_project_and_the_workflow_retries(api_for, make_user, uptrace, repo_ok, monkeypatch):
    from sre import activities
    from sre.temporal_types import UptraceSyncInput

    monkeypatch.setattr(temporal_client, "start_uptrace_sync", lambda inp: None)
    out = create(api_for(make_user()))
    assert out["uptrace_status"] == "provisioning"

    def down(*a, **kw):
        raise uptrace_admin.requests.ConnectionError("refused")
    monkeypatch.setattr(uptrace_admin.requests, "request", down)
    with pytest.raises(Exception):
        activities.provision_managed_uptrace.__wrapped__(UptraceSyncInput("http://testserver", out["id"]))
    project = Project.objects.get(id=out["id"])
    assert project.uptrace_status == "error" and "unreachable" in project.uptrace_error


def test_temporal_down_at_create_leaves_a_retryable_error(api_for, make_user, uptrace, repo_ok, monkeypatch):
    def down(inp):
        raise RuntimeError("temporal down")
    monkeypatch.setattr(temporal_client, "start_uptrace_sync", down)
    out = create(api_for(make_user()))
    assert out["uptrace_status"] == "error" and "try again" in out["uptrace_error"]


def test_managed_off_keeps_the_manual_flow(api_for, make_user, settings, repo_ok, monkeypatch):
    settings.UPTRACE_MANAGED_TOKEN = ""
    monkeypatch.setattr(temporal_client, "start_uptrace_sync",
                        lambda inp: pytest.fail("no sync without managed Uptrace"))
    owner = api_for(make_user())
    out = create(owner)
    assert not out["uptrace_managed"] and out["uptrace_status"] == "" and out["uptrace_dsn"] == ""
    assert owner.get("/uptrace/managed").json() == {"enabled": False, "url": ""}


def test_manually_pinned_project_is_not_managed(api_for, make_user, uptrace, syncs, repo_ok):
    out = create(api_for(make_user()), uptrace_source_id="app.uptrace.dev/5")
    assert not out["uptrace_managed"] and syncs == []


# ---- sharing -----------------------------------------------------------------------

def test_sharing_filters_each_projects_alerts_by_service(api_for, make_user, uptrace, syncs, repo_ok):
    owner = api_for(make_user())
    cart = create(owner, name="cart", service_names=["sales-cart"])
    order = create(owner, name="order", service_names=["sales-order"],
                   uptrace_share_with_project_id=cart["id"])

    assert order["uptrace_project_id"] == cart["uptrace_project_id"]
    assert order["uptrace_status"] == "ready"
    assert order["uptrace_shared_with"] == [{"id": cart["id"], "name": "cart"}]
    assert owner.get(f"/projects/{cart['id']}").json()["uptrace_shared_with"] == [
        {"id": order["id"], "name": "order"}]
    monitors, channels = uptrace.ours(cart["uptrace_project_id"])
    assert monitors[f"platform: project {cart['id']}"]["params"]["query"].endswith(
        'where service_name in ("sales-cart")')
    assert monitors[f"platform: project {order['id']}"]["params"]["query"].endswith(
        'where service_name in ("sales-order")')
    assert len(channels) == 2


def test_sharing_needs_service_names_on_both(api_for, make_user, uptrace, syncs, repo_ok):
    owner = api_for(make_user())
    cart = create(owner, name="cart")
    resp = owner.post("/projects", {"name": "order", "github_installation_id": "1",
                                    "github_repo_owner": "acme", "github_repo_name": "o",
                                    "service_names": ["sales-order"],
                                    "uptrace_share_with_project_id": cart["id"]})
    assert resp.status_code == 400 and "service names" in resp.json()["detail"]


def test_sharing_needs_admin_on_the_other_project_and_the_same_org(
        api_for, make_user, make_project, uptrace, syncs, repo_ok):
    alice, bob = make_user("alice@example.com"), make_user("bob@example.com")
    theirs = make_project(bob, service_names=["x"], uptrace_managed=True, uptrace_project_id=1)
    ProjectMembership.objects.create(project=theirs, user=alice, role=ProjectRole.VIEWER)
    resp = api_for(alice).post("/projects", {
        "name": "mine", "github_installation_id": "1", "github_repo_owner": "a",
        "github_repo_name": "b", "service_names": ["y"], "uptrace_share_with_project_id": theirs.id})
    assert resp.status_code == 403

    ProjectMembership.objects.filter(project=theirs, user=alice).update(role=ProjectRole.ADMIN)
    resp = api_for(alice).post("/projects", {
        "name": "mine", "github_installation_id": "1", "github_repo_owner": "a",
        "github_repo_name": "b", "service_names": ["y"], "uptrace_share_with_project_id": theirs.id})
    assert resp.status_code == 400 and "same organization" in resp.json()["detail"]


def test_changing_service_names_in_a_shared_group_resyncs_it(api_for, make_user, uptrace, syncs, repo_ok):
    owner = api_for(make_user())
    cart = create(owner, name="cart", service_names=["sales-cart"])
    create(owner, name="order", service_names=["sales-order"], uptrace_share_with_project_id=cart["id"])
    assert owner.patch(f"/projects/{cart['id']}", {"service_names": ["cart-v2"]}).status_code == 200
    monitors, _ = uptrace.ours(cart["uptrace_project_id"])
    assert monitors[f"platform: project {cart['id']}"]["params"]["query"].endswith('in ("cart-v2")')
    resp = owner.patch(f"/projects/{cart['id']}", {"service_names": []})
    assert resp.status_code == 400


def test_deleting_a_project_removes_its_wiring_and_unshares_the_rest(api_for, make_user, uptrace, syncs, repo_ok):
    owner = api_for(make_user())
    cart = create(owner, name="cart", service_names=["sales-cart"])
    order = create(owner, name="order", service_names=["sales-order"], uptrace_share_with_project_id=cart["id"])
    assert owner.delete(f"/projects/{order['id']}").status_code == 204

    monitors, channels = uptrace.ours(cart["uptrace_project_id"])
    assert set(monitors) == set(channels) == {f"platform: project {cart['id']}"}
    # Alone again: the remaining project takes every error in its Uptrace project.
    assert "service_name" not in monitors[f"platform: project {cart['id']}"]["params"]["query"]


def test_setup_moves_an_existing_project_to_managed_and_back_to_its_own(
        api_for, make_user, make_project, uptrace, syncs, repo_ok):
    user = make_user()
    owner = api_for(user)
    cart = create(owner, name="cart", service_names=["sales-cart"])
    legacy = make_project(user, name="legacy", service_names=["legacy-svc"],
                          uptrace_source_id="app.uptrace.dev/3")

    out = owner.post(f"/projects/{legacy.id}/uptrace/setup", {"share_with_project_id": cart["id"]}).json()
    assert out["uptrace_managed"] and out["uptrace_project_id"] == cart["uptrace_project_id"]
    assert out["uptrace_source_id"] == f"uptrace.example.test/{cart['uptrace_project_id']}"

    own = owner.post(f"/projects/{legacy.id}/uptrace/setup", {}).json()
    assert own["uptrace_status"] == "ready"
    assert own["uptrace_project_id"] not in (None, cart["uptrace_project_id"])
    monitors, _ = uptrace.ours(cart["uptrace_project_id"])
    assert set(monitors) == {f"platform: project {cart['id']}"}  # left the shared one cleanly


def test_setup_needs_owner_and_managed_uptrace(api_for, make_user, make_project, uptrace, syncs, settings):
    user, admin = make_user(), make_user("admin@example.com")
    project = make_project(user)
    ProjectMembership.objects.create(project=project, user=admin, role=ProjectRole.ADMIN)
    assert api_for(admin).post(f"/projects/{project.id}/uptrace/setup", {}).status_code == 403
    settings.UPTRACE_MANAGED_TOKEN = ""
    assert api_for(user).post(f"/projects/{project.id}/uptrace/setup", {}).status_code == 400


def test_rotating_the_secret_updates_the_channel(api_for, make_user, uptrace, syncs, repo_ok):
    owner = api_for(make_user())
    out = create(owner)
    new_secret = owner.post(f"/projects/{out['id']}/webhook-secret/rotate").json()["webhook_secret"]
    _, channels = uptrace.ours(out["uptrace_project_id"])
    assert channels[f"platform: project {out['id']}"]["params"]["url"].endswith(f"?token={new_secret}")


def test_managed_pin_cannot_be_edited_by_hand(api_for, make_user, uptrace, syncs, repo_ok):
    owner = api_for(make_user())
    out = create(owner)
    resp = owner.patch(f"/projects/{out['id']}", {"uptrace_source_id": "app.uptrace.dev/9"})
    assert resp.status_code == 400


def test_duplicate_monitors_from_racing_syncs_are_cleaned_up(api_for, make_user, uptrace, syncs, repo_ok):
    out = create(api_for(make_user()))
    upid = out["uptrace_project_id"]
    uptrace._monitor(upid, {"name": f"platform: project {out['id']}", "type": "error",
                            "status": "active", "params": {"query": "x"}})
    uptrace_admin.sync_group(upid, "http://testserver")
    monitors = [m for m in uptrace.monitors.values()
                if m["projectId"] == upid and m["name"] == f"platform: project {out['id']}"]
    assert len(monitors) == 1


# ---- visibility and telemetry ---------------------------------------------------

def test_dsn_is_shown_to_admins_only(api_for, make_user, uptrace, syncs, repo_ok):
    owner_user, viewer = make_user(), make_user("viewer@example.com")
    out = create(api_for(owner_user))
    ProjectMembership.objects.create(project_id=out["id"], user=viewer, role=ProjectRole.VIEWER)
    assert api_for(viewer).get(f"/projects/{out['id']}").json()["uptrace_dsn"] == ""
    assert api_for(owner_user).get(f"/projects/{out['id']}").json()["uptrace_dsn"].startswith("https://")


def test_managed_projects_fetch_telemetry_with_the_platform_token(api_for, make_user, uptrace, syncs, repo_ok, settings):
    settings.UPTRACE_MANAGED_API_URL = "http://uptrace:80"  # internal: trusted, it's ours
    out = create(api_for(make_user()))
    project = Project.objects.get(id=out["id"])
    credential = resolve_credential(project)
    assert credential is not None and credential.pk is None and credential.host == "uptrace.example.test"
    client = UptraceClient(credential, str(project.uptrace_project_id))
    assert client.token == "admin-token" and client.trusted_url
    assert out["uptrace_fetch_ready"] is True


def test_managed_credential_never_serves_another_host(make_user, make_project, uptrace):
    project = make_project(make_user(), uptrace_managed=True, uptrace_source_id="app.uptrace.dev/1",
                           uptrace_status=UptraceStatus.READY, uptrace_dsn_encrypted=encrypt("x"))
    assert resolve_credential(project) is None


def test_managed_status_endpoint(api_for, make_user, uptrace):
    assert api_for(make_user()).get("/uptrace/managed").json() == {
        "enabled": True, "url": "https://uptrace.example.test"}


def test_dsn_is_always_on_the_public_https_url(make_user, make_project, uptrace):
    # Stored before this fix: plain http, as Uptrace built it.
    project = make_project(make_user(), uptrace_managed=True,
                           uptrace_dsn_encrypted=encrypt("http://tok@uptrace.example.test?grpc=4317"))
    assert uptrace_admin.dsn_of(project) == "https://tok@uptrace.example.test?grpc=4317"
    assert uptrace_admin.public_dsn("") == ""


def test_resolving_alerts_only_touches_the_projects_own_open_alerts(api_for, make_user, uptrace, syncs, repo_ok):
    owner = api_for(make_user())
    cart = create(owner, name="cart", service_names=["sales-cart"])
    order = create(owner, name="order", service_names=["sales-order"], uptrace_share_with_project_id=cart["id"])
    upid = cart["uptrace_project_id"]
    monitors, _ = uptrace.ours(upid)
    mine, theirs = monitors[f"platform: project {cart['id']}"]["id"], monitors[f"platform: project {order['id']}"]["id"]
    uptrace.alerts = [
        {"id": 1, "projectId": upid, "monitorId": mine, "event": {"status": "unresolved"}},
        {"id": 2, "projectId": upid, "monitorId": mine, "event": {"status": "resolved"}},
        {"id": 3, "projectId": upid, "monitorId": theirs, "event": {"status": "unresolved"}},
    ]
    resp = owner.post(f"/projects/{cart['id']}/uptrace/resolve-alerts")
    assert resp.status_code == 200 and resp.json() == {"resolved": 1}
    assert [a["event"]["status"] for a in uptrace.alerts] == ["resolved", "resolved", "unresolved"]


def test_resolving_alerts_needs_admin_and_managed_uptrace(api_for, make_user, make_project, uptrace):
    owner_user, viewer = make_user(), make_user("viewer@example.com")
    project = make_project(owner_user)  # not managed
    ProjectMembership.objects.create(project=project, user=viewer, role=ProjectRole.VIEWER)
    assert api_for(viewer).post(f"/projects/{project.id}/uptrace/resolve-alerts").status_code == 403
    assert api_for(owner_user).post(f"/projects/{project.id}/uptrace/resolve-alerts").status_code == 400
