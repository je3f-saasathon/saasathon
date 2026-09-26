"""Reads an alert's exception from the Uptrace API, so triage sees the real stack trace and
service instead of just the alert name.

Two calls, checked against Uptrace 2.1 (self-hosted, infra/uptrace/):
  GET /internal/v1/alerts/{uptrace_project_id}/{alert_id}
      -> alert.attrs (_group_id, exception_type::str) and alert.event.params.{traceId, spanId},
         a sample span of the error group.
  GET /internal/v1/traces/{uptrace_project_id}/{trace_id}/{span_id}
      -> span.attrs (exception_type::str, exception_stacktrace::str, service_name::str, ...),
         span.displayName ("Type: message") and span.name (the operation, e.g. "POST /orders").
Both take the user-scoped API token as `Authorization: Bearer <token>`.
"""

import json
import logging

import requests
from django.conf import settings

from ..crypto import decrypt
from ..models import IncidentRun, Project, UptraceCredential
from ..validators import UnsafeURLError, validate_uptrace_api_url
from .context import MAX_UNTRUSTED_CHARS

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 2_000_000
MAX_ATTRS = 40


class UptraceError(Exception):
    pass


def pinned_source(project: Project) -> tuple[str, str] | None:
    """(host, uptrace project id) from uptrace_source_id, e.g. "uptrace.buggly.dev/1"."""
    host, _, project_id = project.uptrace_source_id.partition("/")
    return (host.lower(), project_id) if host and project_id.isdigit() else None


def resolve_credential(project: Project) -> UptraceCredential | None:
    """The project's own credential, else the one org credential for its pinned host.
    Never a credential from another org, and never one for a different host."""
    source = pinned_source(project)
    if source is None or project.organization_id is None:
        return None
    host = source[0]
    credential = project.uptrace_credential
    if credential is not None:
        if credential.organization_id != project.organization_id or credential.host != host:
            return None
        return credential if credential.has_token else None
    matches = [c for c in UptraceCredential.objects.filter(organization_id=project.organization_id,
                                                           host=host) if c.has_token]
    return matches[0] if len(matches) == 1 else None


def alert_id_of(run: IncidentRun) -> str:
    alert = (run.raw_webhook_payload or {}).get("alert") or {}
    return str(alert.get("id") or "")


def _clean_attrs(attrs: dict) -> dict:
    """Uptrace suffixes attribute keys with their type ("service_name::str")."""
    return {str(k).split("::", 1)[0]: v for k, v in list(attrs.items())[:MAX_ATTRS]}


def _truncate(text, limit: int) -> str:
    return str(text or "")[:limit]


class UptraceClient:
    def __init__(self, credential: UptraceCredential, uptrace_project_id: str):
        self.base = credential.api_base_url.rstrip("/")
        self.token = decrypt(credential.token_encrypted)
        self.project_id = uptrace_project_id

    def _get(self, path: str) -> dict:
        try:
            validate_uptrace_api_url(self.base)
        except UnsafeURLError as exc:
            raise UptraceError(str(exc)) from exc
        try:
            resp = requests.get(
                f"{self.base}{path}", headers={"Authorization": f"Bearer {self.token}"},
                timeout=TIMEOUT_SECONDS, stream=True, allow_redirects=False,
            )
            with resp:
                body = resp.raw.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
        except (requests.RequestException, OSError) as exc:
            raise UptraceError(f"Uptrace request failed: {exc}") from exc
        if resp.status_code != 200:
            raise UptraceError(f"Uptrace returned {resp.status_code} for {path}")
        if len(body) > MAX_RESPONSE_BYTES:
            raise UptraceError("Uptrace response too large")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise UptraceError("Uptrace returned invalid JSON") from exc

    def alert_telemetry(self, alert_id: str) -> dict:
        if not alert_id.isdigit():
            raise UptraceError(f"not an Uptrace alert id: {alert_id!r}")
        alert = self._get(f"/internal/v1/alerts/{self.project_id}/{alert_id}").get("alert") or {}
        params = (alert.get("event") or {}).get("params") or {}
        trace_id, span_id = str(params.get("traceId") or ""), str(params.get("spanId") or "")
        alert_attrs = _clean_attrs(alert.get("attrs") or {})
        span = {}
        if trace_id.isalnum() and span_id.isalnum():
            span = self._get(f"/internal/v1/traces/{self.project_id}/{trace_id}/{span_id}").get("span") or {}
        attrs = _clean_attrs(span.get("attrs") or {})
        exception_type = attrs.pop("exception_type", "") or alert_attrs.get("exception_type", "")
        message = attrs.pop("exception_message", "") or span.get("displayName") or alert.get("name", "")
        return {
            "exception_type": _truncate(exception_type, 200),
            "message": _truncate(message, 2000),
            "stacktrace": _truncate(attrs.pop("exception_stacktrace", ""), MAX_UNTRUSTED_CHARS // 2),
            "service_name": _truncate(attrs.get("service_name", ""), 200),
            "span_name": _truncate(span.get("name", ""), 500),
            "trace_id": trace_id,
            "group_id": str(alert_attrs.get("_group_id", "")),
            "attrs": {k: _truncate(v, 500) for k, v in attrs.items()},
        }


def fetch_telemetry(run: IncidentRun) -> dict:
    """{} whenever there's nothing to fetch or the fetch fails: telemetry only ever adds
    context, it never fails an incident."""
    if not settings.SRE_UPTRACE_FETCH_ENABLED:
        return {}
    project = run.project
    source, alert_id = pinned_source(project), alert_id_of(run)
    credential = resolve_credential(project)
    if source is None or not alert_id or credential is None:
        return {}
    try:
        return UptraceClient(credential, source[1]).alert_telemetry(alert_id)
    except Exception as exc:  # includes a token that no longer decrypts
        logger.warning("Uptrace fetch failed for incident %s: %s", run.id, exc)
        return {}
