"""OTLP/HTTP ingestion for customer-side OpenTelemetry Collectors."""

import gzip
import hashlib
import hmac
import json
import logging
import zlib
from datetime import UTC, datetime

import requests
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from ninja import Router
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from google.protobuf.json_format import MessageToDict

from .models import Project

logger = logging.getLogger(__name__)
router = Router(auth=None, tags=["otlp"])
MAX_BODY_BYTES = 16 * 1024 * 1024
SIGNAL_REQUESTS = {
    "traces": (ExportTraceServiceRequest, "trace"),
    "logs": (ExportLogsServiceRequest, "log"),
    "metrics": (ExportMetricsServiceRequest, "metric"),
}


def _read_payload(request: HttpRequest) -> bytes:
    # Read one byte over the compressed limit without Django's default 2.5 MiB
    # request.body cap, while still bounding memory for protobuf batches.
    raw = request.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("payload too large")
    if request.headers.get("Content-Encoding", "").lower() != "gzip":
        return raw
    # Bound decompression to avoid a small gzip bomb allocating unbounded memory.
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    data = inflater.decompress(raw, MAX_BODY_BYTES + 1)
    if len(data) > MAX_BODY_BYTES or inflater.unconsumed_tail:
        raise ValueError("payload too large")
    data += inflater.flush(MAX_BODY_BYTES + 1 - len(data))
    if len(data) > MAX_BODY_BYTES:
        raise ValueError("payload too large")
    return data


def _iso_time(nanos: int) -> str:
    if not nanos:
        return datetime.now(UTC).isoformat(timespec="microseconds")
    seconds, remainder = divmod(nanos, 1_000_000_000)
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=remainder // 1000).isoformat(
        timespec="microseconds"
    )


def _body_text(value) -> str:
    kind = value.WhichOneof("value")
    if kind == "string_value":
        return value.string_value
    if kind == "bytes_value":
        return value.bytes_value.decode("utf-8", errors="replace")
    return json.dumps(MessageToDict(value, preserving_proto_field_name=True), separators=(",", ":"))


def _rows(message, signal: str, project: Project) -> list[dict]:
    """Flatten signal records while preserving the full record/resource JSON."""
    rows = []
    if signal == "trace":
        for resource in message.resource_spans:
            resource_json = MessageToDict(resource.resource, preserving_proto_field_name=True)
            for scope in resource.scope_spans:
                scope_json = MessageToDict(scope.scope, preserving_proto_field_name=True)
                for span in scope.spans:
                    record_json = MessageToDict(span, preserving_proto_field_name=True)
                    rows.append({
                        "event_time": _iso_time(span.start_time_unix_nano),
                        "organization_id": project.organization_id,
                        "project_id": project.id,
                        "signal": signal,
                        "trace_id": span.trace_id.hex(),
                        "record_id": span.span_id.hex(),
                        "name": span.name,
                        "severity_number": 0,
                        "body": "",
                        "payload": json.dumps({"resource": resource_json, "scope": scope_json,
                                               "record": record_json}),
                    })
    elif signal == "log":
        for resource in message.resource_logs:
            resource_json = MessageToDict(resource.resource, preserving_proto_field_name=True)
            for scope in resource.scope_logs:
                scope_json = MessageToDict(scope.scope, preserving_proto_field_name=True)
                for record in scope.log_records:
                    record_json = MessageToDict(record, preserving_proto_field_name=True)
                    rows.append({
                        "event_time": _iso_time(record.time_unix_nano or record.observed_time_unix_nano),
                        "organization_id": project.organization_id,
                        "project_id": project.id,
                        "signal": signal,
                        "trace_id": record.trace_id.hex(),
                        "record_id": record.span_id.hex(),
                        "name": "",
                        "severity_number": record.severity_number,
                        "body": _body_text(record.body),
                        "payload": json.dumps({"resource": resource_json, "scope": scope_json,
                                               "record": record_json}),
                    })
    else:
        for resource in message.resource_metrics:
            resource_json = MessageToDict(resource.resource, preserving_proto_field_name=True)
            for scope in resource.scope_metrics:
                scope_json = MessageToDict(scope.scope, preserving_proto_field_name=True)
                for metric in scope.metrics:
                    data_field = metric.WhichOneof("data")
                    data = getattr(metric, data_field) if data_field else None
                    for point in getattr(data, "data_points", []):
                        point_json = MessageToDict(point, preserving_proto_field_name=True)
                        payload = {"resource": resource_json, "scope": scope_json,
                                   "metric": MessageToDict(metric, preserving_proto_field_name=True),
                                   "data_point": point_json}
                        rows.append({
                            "event_time": _iso_time(getattr(point, "time_unix_nano", 0)),
                            "organization_id": project.organization_id,
                            "project_id": project.id,
                            "signal": signal,
                            "trace_id": "",
                            "record_id": "",
                            "name": metric.name,
                            "severity_number": 0,
                            "body": "",
                            "payload": json.dumps(payload),
                        })
    for row in rows:
        fingerprint = json.dumps(row, sort_keys=True, separators=(",", ":"))
        row["dedupe_key"] = hashlib.sha256(fingerprint.encode()).hexdigest()
    return rows


def _clickhouse_insert(rows: list[dict]) -> None:
    base_url = getattr(settings, "OTEL_CLICKHOUSE_URL", "")
    if not base_url:
        raise RuntimeError("OTEL_CLICKHOUSE_URL is not configured")
    auth = None
    user = getattr(settings, "OTEL_CLICKHOUSE_USER", "default")
    password = getattr(settings, "OTEL_CLICKHOUSE_PASSWORD", "")
    if user:
        auth = (user, password)
    # DDL is idempotent; this also gives a simple first-run path. Credentials should
    # be restricted to the dedicated telemetry database in deployed environments.
    ddl = ("CREATE TABLE IF NOT EXISTS sre_telemetry ("
           "event_time DateTime64(6, 'UTC'), organization_id UInt64, project_id UInt64, "
           "signal LowCardinality(String), trace_id String, record_id String, name String, "
           "severity_number UInt8, body String, payload String, dedupe_key String) "
           "ENGINE = ReplacingMergeTree PARTITION BY toYYYYMM(event_time) "
           "ORDER BY (organization_id, project_id, dedupe_key) "
           "TTL event_time + INTERVAL 30 DAY")
    common = {"auth": auth, "timeout": 10}
    created = requests.post(base_url, params={"query": ddl}, **common)
    created.raise_for_status()
    if rows:
        body = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
        response = requests.post(base_url, params={"query": "INSERT INTO sre_telemetry FORMAT JSONEachRow"},
                                 data=body.encode(), headers={"Content-Type": "application/json"}, **common)
        response.raise_for_status()


def _handle(request: HttpRequest, signal: str) -> HttpResponse:
    token = request.headers.get("Authorization", "")
    if not token.startswith("Bearer "):
        return HttpResponse(status=401)
    digest = hashlib.sha256(token[7:].encode()).hexdigest()
    project = Project.objects.select_related("organization").filter(otel_token_hash=digest).first()
    if project is None or not hmac.compare_digest(project.otel_token_hash, digest):
        return HttpResponse(status=401)
    try:
        raw = _read_payload(request)
    except (ValueError, zlib.error, OSError):
        return HttpResponse(status=413)
    proto_type, signal_name = SIGNAL_REQUESTS[signal]
    message = proto_type()
    try:
        message.ParseFromString(raw)
        rows = _rows(message, signal_name, project)
    except Exception:
        return HttpResponse(status=400)
    try:
        _clickhouse_insert(rows)
    except Exception:
        logger.exception("OTLP write failed for project %s", project.id)
        return HttpResponse(status=503)
    return HttpResponse(b"", content_type="application/x-protobuf", status=200)


@router.post("/v1/traces", auth=None)
def ingest_traces(request: HttpRequest):
    return _handle(request, "traces")


@router.post("/v1/logs", auth=None)
def ingest_logs(request: HttpRequest):
    return _handle(request, "logs")


@router.post("/v1/metrics", auth=None)
def ingest_metrics(request: HttpRequest):
    return _handle(request, "metrics")
