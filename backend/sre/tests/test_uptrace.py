import json

import pytest
import responses

from sre.crypto import encrypt
from sre.models import IncidentRun, Organization, UptraceCredential
from sre.orgs import personal_org
from sre.services.context import incident_context
from sre.services.uptrace import fetch_telemetry, resolve_credential

pytestmark = pytest.mark.django_db

API = "https://uptrace.buggly.dev"

# Trimmed from what Uptrace 2.1 (infra/uptrace/) returned for a real error alert.
ALERT = {"alert": {
    "id": 1, "projectId": 1, "name": "AttributeError: 'NoneType' object has no attribute 'loyalty_tier'",
    "attrs": {"_group_id": "7355350257491066919", "log_severity::str": "ERROR",
              "exception_type::str": "AttributeError"},
    "event": {"params": {"traceId": "b71c368ef5d2c666186e42420e443e10", "spanId": "00076951e0187c12"}},
}}
SPAN = {"span": {
    "id": "00076951e0187c12", "traceId": "b71c368ef5d2c666186e42420e443e10", "name": "POST /orders",
    "displayName": "AttributeError: 'NoneType' object has no attribute 'loyalty_tier'",
    "attrs": {
        "exception_stacktrace::str": "Traceback (most recent call last):\n  File \"catalog/views.py\", "
                                     "line 88, in create_order\nAttributeError: ...",
        "exception_type::str": "AttributeError",
        "service_name::str": "django-buggy-app",
        "deployment_environment_name::str": "prod",
    },
}}


@pytest.fixture
def owner(make_user):
    return make_user()


@pytest.fixture
def project(owner, make_project):
    return make_project(owner, uptrace_source_id="uptrace.buggly.dev/1")


def _credential(org, host="uptrace.buggly.dev", name="main", token="tok"):
    return UptraceCredential.objects.create(organization=org, name=name, host=host, api_base_url=API,
                                            token_encrypted=encrypt(token) if token else b"")


def _incident(project, alert_id="1"):
    return IncidentRun.objects.create(
        project=project, trace_id=f"uptrace-alert-{alert_id}", temporal_workflow_id=f"w-{alert_id}",
        raw_webhook_payload={"alert": {"id": alert_id, "url": f"{API}/alerting/1/alerts/{alert_id}"}},
    )


# ---- which credential a project uses -------------------------------------------------

def test_org_credential_is_found_by_the_pinned_host(project):
    credential = _credential(project.organization)
    _credential(project.organization, host="other.example", name="other")
    assert resolve_credential(project) == credential


def test_two_credentials_for_one_host_are_ambiguous(project):
    _credential(project.organization)
    _credential(project.organization, name="second")
    assert resolve_credential(project) is None


def test_projects_own_credential_wins_but_must_match_host_and_org(project, make_user):
    _credential(project.organization)
    chosen = _credential(project.organization, name="chosen")
    project.uptrace_credential = chosen
    assert resolve_credential(project) == chosen

    chosen.host = "other.example"
    assert resolve_credential(project) is None

    other_org = Organization.objects.create(name="other")
    project.uptrace_credential = _credential(other_org)
    assert resolve_credential(project) is None


def test_no_pin_or_no_token_means_no_credential(project):
    _credential(project.organization, token="")
    assert resolve_credential(project) is None
    project.uptrace_source_id = ""
    assert resolve_credential(project) is None


# ---- fetching -------------------------------------------------------------------------

@responses.activate
def test_fetch_normalizes_the_alerts_exception(project, settings):
    settings.SRE_UPTRACE_FETCH_ENABLED = True
    _credential(project.organization, token="secret-token")
    responses.get(f"{API}/internal/v1/alerts/1/1", json=ALERT)
    responses.get(f"{API}/internal/v1/traces/1/b71c368ef5d2c666186e42420e443e10/00076951e0187c12",
                  json=SPAN)
    telemetry = fetch_telemetry(_incident(project))
    assert telemetry == {
        "exception_type": "AttributeError",
        "message": "AttributeError: 'NoneType' object has no attribute 'loyalty_tier'",
        "stacktrace": SPAN["span"]["attrs"]["exception_stacktrace::str"],
        "service_name": "django-buggy-app",
        "span_name": "POST /orders",
        "trace_id": "b71c368ef5d2c666186e42420e443e10",
        "group_id": "7355350257491066919",
        "attrs": {"service_name": "django-buggy-app", "deployment_environment_name": "prod"},
    }
    assert responses.calls[0].request.headers["Authorization"] == "Bearer secret-token"


@responses.activate
def test_fetch_is_off_by_default(project):
    _credential(project.organization)
    assert fetch_telemetry(_incident(project)) == {}
    assert not responses.calls


@responses.activate
def test_a_failed_fetch_returns_nothing(project, settings):
    settings.SRE_UPTRACE_FETCH_ENABLED = True
    _credential(project.organization)
    responses.get(f"{API}/internal/v1/alerts/1/1", status=500)
    assert fetch_telemetry(_incident(project)) == {}


@responses.activate
def test_a_direct_trace_id_call_has_no_alert_to_fetch(project, settings):
    settings.SRE_UPTRACE_FETCH_ENABLED = True
    _credential(project.organization)
    run = IncidentRun.objects.create(project=project, trace_id="t1", temporal_workflow_id="w-t1",
                                     raw_webhook_payload={"trace_id": "t1"})
    assert fetch_telemetry(run) == {}
    assert not responses.calls


def test_private_api_url_is_refused_at_call_time(project, settings):
    settings.SRE_UPTRACE_FETCH_ENABLED = True
    settings.SRE_ALLOW_PRIVATE_UPTRACE_URLS = False
    credential = _credential(project.organization)
    credential.api_base_url = "http://127.0.0.1:14418"
    credential.save()
    assert fetch_telemetry(_incident(project)) == {}


def test_telemetry_only_reaches_the_prompt_when_fetched(project):
    run = _incident(project)
    assert "telemetry" not in incident_context(run)
    run.telemetry = {"exception_type": "AttributeError"}
    assert '"telemetry"' in incident_context(run)


# ---- API --------------------------------------------------------------------------------

def _create(api, org_id, **extra):
    body = {"name": "main", "host": "https://Uptrace.Buggly.dev/", "api_base_url": API,
            "token": "super-secret", **extra}
    return api.post(f"/organizations/{org_id}/uptrace-credentials", body)


def test_credential_api_never_returns_the_token(owner, project, api_for, settings):
    settings.SRE_ALLOW_PRIVATE_UPTRACE_URLS = True
    api = api_for(owner)
    org_id = project.organization_id
    resp = _create(api, org_id)
    assert resp.status_code == 201
    body = resp.json()
    assert body["host"] == "uptrace.buggly.dev" and body["has_token"] is True
    assert "super-secret" not in resp.content.decode()
    assert "super-secret" not in api.get(f"/organizations/{org_id}/uptrace-credentials").content.decode()
    assert api.get(f"/projects/{project.id}").json()["uptrace_fetch_ready"] is True

    rotated = api.patch(f"/uptrace-credentials/{body['id']}", {"token": "new-secret"})
    assert rotated.status_code == 200 and "new-secret" not in rotated.content.decode()
    assert _create(api, org_id).status_code == 409
    assert api.delete(f"/uptrace-credentials/{body['id']}").status_code == 204


def test_credential_api_needs_org_admin(owner, project, api_for, make_user):
    org = Organization.objects.create(name="Acme")
    member = make_user("m@x.com")
    org.memberships.create(user=owner, role="owner")
    org.memberships.create(user=member, role="member")
    assert _create(api_for(member), org.id).status_code == 403
    assert _create(api_for(make_user("z@x.com")), org.id).status_code == 404


def test_private_api_url_is_refused_on_save(owner, project, api_for, settings):
    settings.SRE_ALLOW_PRIVATE_UPTRACE_URLS = False
    resp = _create(api_for(owner), project.organization_id, api_base_url="http://127.0.0.1:14418")
    assert resp.status_code == 400


def test_project_credential_must_be_in_its_org(owner, project, api_for):
    other_org = Organization.objects.create(name="other")
    foreign = _credential(other_org)
    api = api_for(owner)
    assert api.patch(f"/projects/{project.id}", {"uptrace_credential_id": foreign.id}).status_code == 400
    own = _credential(personal_org(owner), name="own")
    resp = api.patch(f"/projects/{project.id}", {"uptrace_credential_id": own.id})
    assert resp.status_code == 200 and resp.json()["uptrace_credential_id"] == own.id
    cleared = api.patch(f"/projects/{project.id}", {"uptrace_credential_id": None})
    assert cleared.json()["uptrace_credential_id"] is None
