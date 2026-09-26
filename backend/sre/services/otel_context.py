"""Select a small, incident-focused slice of retained telemetry for LLM context."""

import json
import logging
import re

import requests
from django.conf import settings

logger = logging.getLogger(__name__)
TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
MAX_CONTEXT_CHARS = 3_000
MAX_SPANS = 12
MAX_LOGS = 24


def _attribute_map(attrs: list[dict]) -> dict:
    result = {}
    for item in attrs[:16]:
        value = item.get("value", {})
        if not isinstance(value, dict):
            continue
        # OTel AnyValue uses a oneof such as string_value / int_value.
        actual = next(iter(value.values()), None)
        if isinstance(actual, str | int | float | bool):
            result[str(item.get("key", ""))[:100]] = actual
    return result


def _selected(row: dict) -> dict | None:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError, KeyError):
        return None
    resource = _attribute_map(payload.get("resource", {}).get("attributes", []))
    scope = payload.get("scope", {})
    if row.get("signal") == "trace":
        record = payload.get("record", {})
        return {
            "kind": "span",
            "name": str(record.get("name") or row.get("name") or "")[:300],
            "span_id": record.get("span_id", ""),
            "parent_span_id": record.get("parent_span_id", ""),
            "status": record.get("status", {}),
            "duration_ns": max(0, int(record.get("end_time_unix_nano", 0)) -
                               int(record.get("start_time_unix_nano", 0))),
            "service": resource.get("service.name", ""),
            "attributes": _attribute_map(record.get("attributes", [])),
            "instrumentation_scope": scope.get("name", ""),
        }
    return {
        "kind": "log",
        "severity": row.get("severity_number", 0),
        "body": str(row.get("body") or "")[:1200],
        "service": resource.get("service.name", ""),
        "attributes": _attribute_map(payload.get("record", {}).get("attributes", [])),
    }


def fetch_trace_context(project, trace_id: str) -> dict:
    """Return matching spans and warning/error logs, capped to keep LLM prompts lean.

    Telemetry stays in ClickHouse for its retention period. This is a query-time filter:
    only matching trace records and warning-or-higher logs enter the incident object.
    Metrics and unrelated low-severity logs remain stored but are omitted from triage.
    """
    url = getattr(settings, "OTEL_CLICKHOUSE_URL", "")
    if not url or not TRACE_ID.fullmatch(trace_id or ""):
        return {}
    user = getattr(settings, "OTEL_CLICKHOUSE_USER", "default")
    auth = (user, getattr(settings, "OTEL_CLICKHOUSE_PASSWORD", "")) if user else None
    query = (
        "SELECT signal, name, severity_number, body, payload FROM sre_telemetry FINAL "
        f"WHERE organization_id = {int(project.organization_id)} AND project_id = {int(project.id)} "
        f"AND trace_id = '{trace_id.lower()}' AND "
        "(signal = 'trace' OR (signal = 'log' AND severity_number >= 13)) "
        "ORDER BY event_time DESC LIMIT 64 FORMAT JSONEachRow"
    )
    try:
        response = requests.post(url, params={"query": query}, auth=auth, timeout=5)
        response.raise_for_status()
        rows = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.info("OTLP context lookup skipped for project %s: %s", project.id, exc)
        return {}

    spans, logs = [], []
    for row in rows:
        item = _selected(row)
        if not item:
            continue
        target = spans if item["kind"] == "span" else logs
        max_items = MAX_SPANS if target is spans else MAX_LOGS
        if len(target) < max_items:
            target.append(item)
    result = {"spans": spans, "logs": logs}
    while len(json.dumps(result, separators=(",", ":"))) > MAX_CONTEXT_CHARS:
        if logs:
            logs.pop()
        elif spans:
            spans.pop()
        else:
            break
    return result if spans or logs else {}
