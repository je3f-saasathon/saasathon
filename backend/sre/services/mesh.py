"""The service mesh: an org's service graph per Uptrace project, which service belongs to
which project (repo), and the trace walk that finds where an error started.

See docs/MESH_AND_REMEDIATION.md. The graph is copied from Uptrace's service graph
(refresh_graph) and topped up from each alert's trace (merge_trace). Which project a
service maps to is worked out on read, so changing a project's service_names or repo takes
effect immediately.
"""

import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import Organization, Project, ServiceEdge, ServiceGraph, ServiceNode
from .uptrace import UptraceClient, UptraceError, _clean_attrs, _truncate, client_for

logger = logging.getLogger(__name__)

REFRESH_WINDOW = timedelta(hours=1)
# Uptrace names a span's system "<type>:<service>" for a service's own spans and
# "<type>:<vendor>" (e.g. "db:postgresql") for calls into a dependency.
DEPENDENCY_SYSTEM_TYPES = ("db", "messaging", "cache", "rpc")

_GITHUB_REPO = re.compile(r"github\.com[/:]([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?$", re.IGNORECASE)


def repo_key(url: str) -> str:
    """"https://github.com/Acme/api.git" -> "acme/api"; "" for anything else."""
    match = _GITHUB_REPO.search(url.strip())
    return f"{match.group(1)}/{match.group(2)}".lower() if match else ""


# ---- which project a service belongs to ---------------------------------------------

def mesh_projects(organization_id: int, source: str) -> list[Project]:
    """The projects a service in this graph may map to: same org, same Uptrace project."""
    return list(Project.objects.filter(organization_id=organization_id, uptrace_source_id=source))


def map_service(name: str, repo_url: str, projects: list[Project]) -> tuple[Project | None, str]:
    """(project, mapped_by). The repo attribute wins over service_names lists."""
    key = repo_key(repo_url)
    if key:
        for project in projects:
            if f"{project.github_repo_owner}/{project.github_repo_name}".lower() == key:
                return project, "vcs_attr"
    for project in projects:
        if name and name in (project.service_names or []):
            return project, "service_names"
    return None, ""


# ---- the graph ------------------------------------------------------------------------

def graph_for(organization_id: int, source: str) -> ServiceGraph:
    graph, _ = ServiceGraph.objects.get_or_create(organization_id=organization_id, source=source)
    return graph


def _node(graph: ServiceGraph, name: str, kind: str, now, repo_url: str = "") -> ServiceNode:
    node, created = ServiceNode.objects.get_or_create(
        graph=graph, name=name[:255],
        defaults={"kind": kind, "repo_url": repo_url[:500], "first_seen_at": now, "last_seen_at": now},
    )
    if not created:
        node.last_seen_at = now
        fields = ["last_seen_at"]
        if repo_url and node.repo_url != repo_url:
            node.repo_url = repo_url[:500]
            fields.append("repo_url")
        node.save(update_fields=fields)
    return node


def _edge(graph, client, server, type_, now, stats: dict | None = None) -> None:
    edge, _ = ServiceEdge.objects.get_or_create(
        client=client, server=server, type=type_[:32],
        defaults={"graph": graph, "first_seen_at": now, "last_seen_at": now},
    )
    edge.last_seen_at = now
    if stats is not None:
        for field, value in stats.items():
            setattr(edge, field, value)
    edge.save()


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _node_name(attr: str, name: str) -> tuple[str, str]:
    kind = ServiceNode.Kind.SERVICE if attr == "service_name" else ServiceNode.Kind.SYSTEM
    return str(name or "")[:255], kind


def apply_uptrace_graph(graph: ServiceGraph, edges: list[dict], repos: dict[str, str], now) -> None:
    """Upserts Uptrace's service graph edges (and each service's repo URL) into `graph`."""
    with transaction.atomic():
        for raw in edges:
            client_name, client_kind = _node_name(raw.get("clientAttr") or "", raw.get("clientName"))
            server_name, server_kind = _node_name(raw.get("serverAttr") or "", raw.get("serverName"))
            if not client_name or not server_name:
                continue
            client = _node(graph, client_name, client_kind, now, repos.get(client_name, ""))
            server = _node(graph, server_name, server_kind, now, repos.get(server_name, ""))
            _edge(graph, client, server, str(raw.get("type") or ""), now, {
                "count": int(_num(raw.get("count"))),
                "error_count": int(_num(raw.get("errorCount"))),
                "duration_avg_ms": _num(raw.get("durationAvg")),
                "duration_max_ms": _num(raw.get("durationMax")),
                "rate_per_min": _num(raw.get("rate")),
            })
        # Services that only show up in the repo grouping still get their repo recorded.
        for name, url in repos.items():
            if url:
                ServiceNode.objects.filter(graph=graph, name=name[:255]).update(repo_url=url[:500])


def expire(graph: ServiceGraph, now) -> None:
    cutoff = now - timedelta(days=settings.SRE_SERVICE_GRAPH_TTL_DAYS)
    graph.edges.filter(last_seen_at__lt=cutoff).delete()
    graph.nodes.filter(last_seen_at__lt=cutoff, outgoing__isnull=True, incoming__isnull=True).delete()


def uptrace_client(organization_id: int, source: str) -> UptraceClient | None:
    """A client through any of the org's projects pinned to `source` that has a credential."""
    for project in mesh_projects(organization_id, source):
        client = client_for(project)
        if client is not None:
            return client
    return None


def refresh_graph(organization_id: int, source: str) -> bool:
    """Copies the last hour of Uptrace's service graph in. False if there's no credential
    or Uptrace fails; the graph is left as it was."""
    client = uptrace_client(organization_id, source)
    if client is None:
        return False
    now = timezone.now()
    try:
        edges = client.service_graph(now - REFRESH_WINDOW, now)
        repos = client.service_repos(now - REFRESH_WINDOW, now)
    except UptraceError as exc:
        logger.warning("service graph refresh failed for org %s / %s: %s", organization_id, source, exc)
        return False
    graph = graph_for(organization_id, source)
    apply_uptrace_graph(graph, edges, repos, now)
    expire(graph, now)
    graph.refreshed_at = now
    graph.save(update_fields=["refreshed_at"])
    return True


def refresh_targets(organization_id: int | None = None) -> list[tuple[int, str]]:
    """Every (org, source) pair some project is pinned to."""
    projects = Project.objects.exclude(uptrace_source_id="").exclude(organization__isnull=True)
    if organization_id is not None:
        projects = projects.filter(organization_id=organization_id)
    return sorted(set(projects.values_list("organization_id", "uptrace_source_id")))


def org_sources(organization: Organization) -> list[str]:
    return [source for _, source in refresh_targets(organization.id)]


# ---- traces ---------------------------------------------------------------------------

def _service(span: dict) -> str:
    return str(_clean_attrs(span.get("attrs") or {}).get("service_name") or "")


def _dependency(span: dict) -> str:
    """"db:postgresql" for a span calling into a dependency, else ""."""
    system = str(span.get("system") or "")
    return system if system.split(":", 1)[0] in DEPENDENCY_SYSTEM_TYPES else ""


def _edge_type(span: dict) -> str:
    """The callee span's system type, named the way Uptrace's service graph names it."""
    kind = str(span.get("system") or "").split(":", 1)[0]
    return "http" if kind.startswith("http") else kind


def merge_trace(graph: ServiceGraph, spans: list[dict]) -> None:
    """Adds the edges one trace shows: a span whose parent is in another service, and a
    service's calls into dependencies. Counts are left to the periodic refresh."""
    by_id = {span.get("id"): span for span in spans}
    now = timezone.now()
    with transaction.atomic():
        for span in spans:
            service = _service(span)
            if not service:
                continue
            repo_url = str(_clean_attrs(span.get("attrs") or {}).get("vcs_repository_url_full") or "")
            parent = by_id.get(span.get("parentId"))
            parent_service = _service(parent) if parent else ""
            if parent_service and parent_service != service:
                client = _node(graph, parent_service, ServiceNode.Kind.SERVICE, now)
                server = _node(graph, service, ServiceNode.Kind.SERVICE, now, repo_url)
                _edge(graph, client, server, _edge_type(span), now)
            dependency = _dependency(span)
            if dependency:
                client = _node(graph, service, ServiceNode.Kind.SERVICE, now, repo_url)
                server = _node(graph, dependency, ServiceNode.Kind.SYSTEM, now)
                _edge(graph, client, server, dependency.split(":", 1)[0], now)


def _exception_log(span: dict) -> dict | None:
    for log in span.get("logs") or []:
        system = str(log.get("system") or "")
        if log.get("eventName") == "exception" or system in ("log:error", "log:fatal"):
            return log
    return None


def _failed(span: dict) -> bool:
    return span.get("statusCode") == "error" or _exception_log(span) is not None


@dataclass
class Culprit:
    span: dict
    path: list[str]


def find_culprit(spans: list[dict]) -> Culprit | None:
    """The deepest failed span with no failed children (the earliest if several), and the
    services from the trace's root down to it."""
    by_id = {span.get("id"): span for span in spans}
    failed = [span for span in spans if _failed(span)]
    if not failed:
        return None
    failed_parents = {span.get("parentId") for span in failed}
    # A malformed trace (a parent cycle) can leave no leaves; fall back to every failure.
    leaves = [span for span in failed if span.get("id") not in failed_parents] or failed

    def depth(span: dict) -> int:
        seen, d = set(), 0
        while span.get("parentId") in by_id and span.get("id") not in seen:
            seen.add(span.get("id"))
            span, d = by_id[span["parentId"]], d + 1
        return d

    culprit = min(leaves, key=lambda span: (-depth(span), _num(span.get("time"))))
    services, span, seen = [], culprit, set()
    while span is not None and span.get("id") not in seen:
        seen.add(span.get("id"))
        service = _service(span)
        if service and (not services or services[-1] != service):
            services.append(service)
        span = by_id.get(span.get("parentId"))
    return Culprit(culprit, list(reversed(services)))


def culprit_telemetry(culprit: Culprit, trace_id: str) -> dict:
    """The culprit span in the same shape as fetch_telemetry, for a linked child incident."""
    span = culprit.span
    attrs = _clean_attrs(span.get("attrs") or {})
    log = _exception_log(span) or {}
    log_attrs = _clean_attrs(log.get("attrs") or {})
    exception_type = (log_attrs.get("exception_type") or attrs.get("exception_type")
                      or span.get("statusMessage") or "")
    message = (log_attrs.get("exception_message") or log.get("displayName")
               or span.get("statusMessage") or span.get("displayName") or "")
    return {
        "exception_type": _truncate(exception_type, 200),
        "message": _truncate(message, 2000),
        "stacktrace": _truncate(log_attrs.get("exception_stacktrace") or "", 6000),
        "service_name": _truncate(attrs.get("service_name") or "", 200),
        "span_name": _truncate(span.get("name") or "", 500),
        "trace_id": trace_id,
        "group_id": str(span.get("groupId") or ""),
        "attrs": {k: _truncate(v, 500) for k, v in list(attrs.items())[:40]},
    }


def root_cause(culprit: Culprit, telemetry: dict, project: Project | None, mapped_by: str) -> dict:
    return {
        "service_name": telemetry["service_name"],
        "span_name": telemetry["span_name"],
        "span_id": str(culprit.span.get("id") or ""),
        "exception_type": telemetry["exception_type"],
        "message": telemetry["message"][:500],
        "path": culprit.path,
        "system": _dependency(culprit.span),
        "project_id": project.id if project else None,
        "mapped_by": mapped_by,
        "linked_incident_run_id": None,
    }


def neighbour_projects(project: Project, service_name: str, limit: int) -> list[Project]:
    """Projects running the services that call, or are called by, `service_name` in the
    project's graph, excluding the project itself. Mapped the same way as everywhere else."""
    if not service_name or not project.uptrace_source_id or project.organization_id is None:
        return []
    graph = ServiceGraph.objects.filter(organization_id=project.organization_id,
                                        source=project.uptrace_source_id).first()
    node = graph.nodes.filter(name=service_name).first() if graph else None
    if node is None:
        return []
    cutoff = timezone.now() - timedelta(days=settings.SRE_SERVICE_GRAPH_TTL_DAYS)
    neighbours = [e.server for e in node.outgoing.filter(last_seen_at__gte=cutoff).select_related("server")]
    neighbours += [e.client for e in node.incoming.filter(last_seen_at__gte=cutoff).select_related("client")]
    projects = mesh_projects(project.organization_id, project.uptrace_source_id)
    found: list[Project] = []
    for neighbour in neighbours:
        if neighbour.kind != ServiceNode.Kind.SERVICE:
            continue
        mapped, _ = map_service(neighbour.name, neighbour.repo_url, projects)
        if mapped is not None and mapped.id != project.id and mapped not in found:
            found.append(mapped)
        if len(found) >= limit:
            break
    return found


# ---- the pipeline step ----------------------------------------------------------------

def _trace_id(run) -> str:
    trace_id = str((run.telemetry or {}).get("trace_id") or "")
    # A direct webhook call's incident key is the trace id itself.
    if not trace_id and run.source == run.Source.ALERT and not run.trace_id.startswith("uptrace-alert-"):
        trace_id = run.trace_id
    return trace_id if trace_id.isalnum() else ""


def localize(run):
    """Walks the incident's trace: records the root cause on the run, adds the trace's
    edges to the graph, and, when the culprit runs in another project of the same org and
    Uptrace project, creates the linked child incident (returned; not started). Linked
    incidents never delegate again, so a mapping mistake can't bounce an incident around."""
    from ..models import IncidentRun

    project = run.project
    trace_id, client = _trace_id(run), client_for(project)
    if not trace_id or client is None or project.organization_id is None:
        return None
    try:
        spans = client.trace(trace_id)
    except UptraceError as exc:
        logger.warning("trace fetch failed for incident %s: %s", run.id, exc)
        return None
    merge_trace(graph_for(project.organization_id, project.uptrace_source_id), spans)
    culprit = find_culprit(spans)
    if culprit is None:
        return None
    telemetry = culprit_telemetry(culprit, trace_id)
    repo_url = str(telemetry["attrs"].get("vcs_repository_url_full") or "")
    target, mapped_by = map_service(telemetry["service_name"], repo_url,
                                    mesh_projects(project.organization_id, project.uptrace_source_id))
    cause = root_cause(culprit, telemetry, target, mapped_by)
    child = None
    if target is not None and target.id != project.id and run.source != IncidentRun.Source.LINKED:
        child, _ = IncidentRun.objects.get_or_create(
            temporal_workflow_id=f"sre-incident-{target.id}-linked-{run.id}",
            defaults={
                "project": target,
                "trace_id": trace_id,
                "source": IncidentRun.Source.LINKED,
                "parent_incident_run": run,
                "telemetry": telemetry,
                "root_cause": cause,
                "raw_webhook_payload": {"linked_from_incident_run_id": run.id},
            },
        )
        cause = {**cause, "linked_incident_run_id": child.id}
    run.root_cause = cause
    run.save(update_fields=["root_cause", "updated_at"])
    return child
