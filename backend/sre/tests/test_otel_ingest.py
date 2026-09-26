import hashlib
import secrets

import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

from sre import otel_ingest
from sre.models import ProjectRole

pytestmark = pytest.mark.django_db


def test_owner_can_rotate_project_otlp_token(make_user, make_project, api_for):
    project = make_project(make_user())
    response = api_for(project.memberships.get(role=ProjectRole.OWNER).user).post(
        f"/projects/{project.id}/otel-token/rotate", {}
    )
    assert response.status_code == 200
    token = response.json()["otel_token"]
    project.refresh_from_db()
    assert len(project.otel_token_hash) == 64
    assert project.otel_token_hash != token


def test_otlp_logs_are_assigned_to_project_and_org_from_token(
    client, make_user, make_project, monkeypatch
):
    project = make_project(make_user())
    token = secrets.token_urlsafe(32)
    project.otel_token_hash = hashlib.sha256(token.encode()).hexdigest()
    project.save(update_fields=["otel_token_hash"])

    request = ExportLogsServiceRequest()
    log = request.resource_logs.add().scope_logs.add().log_records.add()
    log.time_unix_nano = 1_800_000_000_000_000_000
    log.severity_number = 17
    log.body.string_value = "database unavailable"
    attribute = log.attributes.add()
    attribute.key = "project_id"
    attribute.value.string_value = "attacker-controlled-project"
    captured = {}
    monkeypatch.setattr(otel_ingest, "_clickhouse_insert", lambda rows: captured.setdefault("rows", rows))

    response = client.post(
        "/api/sre/otel/v1/logs",
        data=request.SerializeToString(),
        content_type="application/x-protobuf",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert response.status_code == 200
    row = captured["rows"][0]
    assert row["organization_id"] == project.organization_id
    assert row["project_id"] == project.id
    assert row["body"] == "database unavailable"
    assert len(row["dedupe_key"]) == 64


def test_otlp_endpoint_rejects_missing_or_invalid_bearer_token(client, make_user, make_project):
    project = make_project(make_user())
    project.otel_token_hash = ""
    project.save(update_fields=["otel_token_hash"])
    request = ExportLogsServiceRequest().SerializeToString()
    for headers in ({}, {"HTTP_AUTHORIZATION": "Bearer invalid"}):
        response = client.post(
            "/api/sre/otel/v1/logs", data=request,
            content_type="application/x-protobuf", **headers,
        )
        assert response.status_code == 401
