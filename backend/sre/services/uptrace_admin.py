"""Managed Uptrace: the platform sets up each project's Uptrace side with its own admin
token, so users never log into Uptrace. Per platform project it keeps:

  - an Uptrace project (its own, or one shared with other projects of the same org so
    linked services land in one trace), pinned as the project's uptrace_source_id;
  - an error monitor "platform: project <id>" over the span's exception events (Django
    also logs every 500 through django.request, which would open a second alert for the
    same bug), filtered to the project's service_names when the Uptrace project is shared;
  - a webhook channel of the same name that only that monitor notifies, pointing at the
    project's webhook URL.

Everything here is idempotent: sync_group() makes Uptrace match the database, removes the
monitors and channels of projects that left, and is safe to rerun. The internal API calls
were checked against Uptrace 2.1 (infra/uptrace/). Uptrace has no API to delete a project,
so a project nobody uses keeps existing, with no monitors or channels of ours.
"""

import logging
from urllib.parse import urlsplit, urlunsplit

import requests
from django.conf import settings

from ..crypto import decrypt, encrypt
from ..models import Project, UptraceCredential, UptraceStatus

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20
NAME_PREFIX = "platform: project "
ALL_PRIORITIES = ["info", "low", "medium", "high"]
LOGS_METRIC = [{"name": "uptrace_tracing_logs", "alias": "$logs"}]
EXCEPTIONS_QUERY = ('sum($logs{}) | group by _group_id | where _system in ("log:error", "log:fatal") '
                    '| where _event_name = "exception"')
SHARED_NEEDS_SERVICES = ("This Uptrace project is shared with other projects: set this project's "
                         "service names so its alerts can be told apart.")


class UptraceAdminError(Exception):
    pass


def configured() -> bool:
    return bool(settings.UPTRACE_MANAGED_TOKEN and settings.UPTRACE_MANAGED_URL)


def managed_host() -> str:
    return (urlsplit(settings.UPTRACE_MANAGED_URL).hostname or "").lower()


def source_id(uptrace_project_id: int) -> str:
    return f"{managed_host()}/{uptrace_project_id}"


def is_managed(project: Project) -> bool:
    return project.uptrace_managed and configured()


def managed_credential(project: Project) -> UptraceCredential:
    """An unsaved credential for the platform's own token, for the telemetry fetch and the
    service mesh. Its api_base_url comes from server settings, so it may be internal."""
    credential = UptraceCredential(
        organization_id=project.organization_id, name="managed", host=managed_host(),
        api_base_url=settings.UPTRACE_MANAGED_API_URL,
        token_encrypted=encrypt(settings.UPTRACE_MANAGED_TOKEN),
    )
    credential.platform_managed = True
    return credential


def public_dsn(dsn: str) -> str:
    """The DSN on UPTRACE_MANAGED_URL's scheme and host. Uptrace builds DSNs from the address
    it's reached on, which is plain http behind a TLS-terminating proxy (the Cloudflare
    tunnel), so apps would send telemetry, and the DSN's token with it, unencrypted."""
    if not dsn:
        return ""
    parts, public = urlsplit(dsn), urlsplit(settings.UPTRACE_MANAGED_URL)
    if not parts.username or not public.hostname:
        return dsn
    return urlunsplit((public.scheme, f"{parts.username}@{public.netloc}", parts.path, parts.query, ""))


def dsn_of(project: Project) -> str:
    try:
        stored = decrypt(project.uptrace_dsn_encrypted) if project.uptrace_dsn_encrypted else ""
    except Exception:  # key rotated: the next sync stores it again
        return ""
    return public_dsn(stored)


def monitor_name(project_id: int) -> str:
    return f"{NAME_PREFIX}{project_id}"


def monitor_query(service_names: list[str] | None) -> str:
    if not service_names:
        return EXCEPTIONS_QUERY
    names = ", ".join('"' + n.replace("\\", "\\\\").replace('"', '\\"') + '"' for n in service_names)
    return f"{EXCEPTIONS_QUERY} | where service_name in ({names})"


def webhook_url(base_url: str, project: Project) -> str:
    return (f"{base_url.rstrip('/')}/api/sre/webhooks/uptrace/{project.id}"
            f"?token={project.uptrace_webhook_secret}")


class UptraceAdmin:
    def __init__(self):
        if not configured():
            raise UptraceAdminError("Managed Uptrace isn't configured on this server")
        self.base = settings.UPTRACE_MANAGED_API_URL
        self.token = settings.UPTRACE_MANAGED_TOKEN

    def call(self, method: str, path: str, body=None) -> dict:
        try:
            resp = requests.request(
                method, f"{self.base}/internal/v1{path}", json=body, timeout=TIMEOUT_SECONDS,
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            )
        except requests.RequestException as exc:
            raise UptraceAdminError(f"Uptrace is unreachable: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json()["error"]["message"]
            except Exception:
                detail = resp.text[:200]
            raise UptraceAdminError(f"Uptrace refused {method} {path.split('?')[0]} "
                                    f"({resp.status_code}): {detail}")
        try:
            return resp.json() if resp.content else {}
        except ValueError as exc:
            # Unknown /internal/ routes answer with the SPA's HTML and a 200.
            raise UptraceAdminError(f"Uptrace returned a non-JSON answer for {path}") from exc

    def org_id(self) -> int:
        if settings.UPTRACE_MANAGED_ORG_ID:
            return settings.UPTRACE_MANAGED_ORG_ID
        # /users/current lists the user's projects (each with its orgId), not its orgs.
        org_ids = sorted({p["orgId"] for p in self.call("GET", "/users/current").get("projects", [])})
        if not org_ids:
            raise UptraceAdminError("Can't tell which Uptrace org to use; set UPTRACE_MANAGED_ORG_ID")
        return org_ids[0]

    def ensure_project(self, name: str) -> int:
        org = self.org_id()
        for project in self.call("GET", f"/orgs/{org}/projects").get("projects", []):
            if project["name"] == name:
                return project["id"]
        return self.call("POST", f"/orgs/{org}/projects",
                         {"name": name, "orgId": org, "displayLogSeverity": True})["project"]["id"]

    def dsn(self, uptrace_project_id: int) -> str:
        tokens = self.call("GET", f"/projects/{uptrace_project_id}/tokens").get("tokens", [])
        if not tokens:
            raise UptraceAdminError(f"Uptrace project {uptrace_project_id} has no ingest token")
        return tokens[0]["dsn"]

    def monitors(self, pid: int) -> list[dict]:
        return self.call("GET", f"/monitors/{pid}").get("monitors", [])

    def channels(self, pid: int) -> list[dict]:
        return self.call("GET", f"/projects/{pid}/notification-channels").get("channels", [])

    def upsert_monitor(self, pid: int, existing: dict | None, name: str, query: str) -> int:
        body = {"name": name, "type": "error", "status": "active", "notifyEveryoneByEmail": False,
                "repeatInterval": {"strategy": "fixed", "interval": 60000},
                "trendAggFunc": "sum", "trendSensitivity": "medium",
                "params": {"query": query, "metrics": LOGS_METRIC}}
        if existing is None:
            return self.call("POST", f"/monitors/{pid}", body)["monitor"]["id"]
        # Uptrace stores queries with typed attributes (service_name::str).
        stored = existing.get("params", {}).get("query", "").replace("::str", "")
        if stored != query or existing.get("status") != "active":
            # Lists leave channelIds empty; a PUT must carry the real ones.
            current = self.call("GET", f"/monitors/{pid}/{existing['id']}")["monitor"]
            self.call("PUT", f"/monitors/{pid}/{existing['id']}", {**current, **body})
        return existing["id"]

    def upsert_channel(self, pid: int, existing: dict | None, name: str, url: str,
                       monitor_id: int) -> int:
        """Uptrace posts a test message to the URL whenever a channel is saved and refuses
        to save unless it gets a 2xx, so a channel is only rewritten when it changed."""
        body = {"name": name, "type": "webhook", "params": {"url": url, "payload": None},
                "matchAll": False, "monitorIds": [monitor_id], "priorities": ALL_PRIORITIES,
                "condition": ""}
        if existing is None:
            return self.call("POST", f"/projects/{pid}/notification-channels", body)["channel"]["id"]
        linked = self.call("GET", f"/monitors/{pid}/{monitor_id}")["monitor"].get("channelIds") or []
        if (existing["params"].get("url") != url or existing.get("matchAll")
                or linked != [existing["id"]]):
            self.call("PUT", f"/projects/{pid}/notification-channels/{existing['id']}",
                      {**existing, **body})
        return existing["id"]

    def delete_monitor(self, pid: int, monitor_id: int) -> None:
        self.call("DELETE", f"/monitors/{pid}/{monitor_id}")

    def delete_channel(self, pid: int, channel_id: int) -> None:
        self.call("DELETE", f"/projects/{pid}/notification-channels/{channel_id}")

    def pause_monitor(self, pid: int, monitor_id: int) -> None:
        self.call("PUT", f"/monitors/{pid}/{monitor_id}/paused", {})


def _by_name(items: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    """Ours by name, keeping the oldest of any duplicates (two syncs racing); plus the rest."""
    ours, extras = {}, []
    for item in sorted(items, key=lambda i: i["id"]):
        if not item["name"].startswith(NAME_PREFIX):
            continue
        if item["name"] in ours:
            extras.append(item)
        else:
            ours[item["name"]] = item
    return ours, extras


def provision(project: Project) -> int:
    """Gives the project an Uptrace project of its own unless it has one (or shares one)."""
    if project.uptrace_project_id:
        return project.uptrace_project_id
    org = project.organization.name if project.organization_id else "personal"
    upid = UptraceAdmin().ensure_project(f"{org} / {project.name} (#{project.id})")
    project.uptrace_project_id = upid
    project.uptrace_source_id = source_id(upid)
    project.save(update_fields=["uptrace_project_id", "uptrace_source_id", "updated_at"])
    return upid


def sync_group(uptrace_project_id: int, base_url: str) -> None:
    """Makes one Uptrace project's monitors and channels match the platform projects using
    it. Records the outcome on each project; raises if Uptrace itself failed (to retry)."""
    admin = UptraceAdmin()
    members = list(Project.objects.filter(uptrace_managed=True,
                                          uptrace_project_id=uptrace_project_id).order_by("id"))
    shared = len(members) > 1
    dsn = public_dsn(admin.dsn(uptrace_project_id)) if members else ""

    monitors = admin.monitors(uptrace_project_id)
    for monitor in monitors:  # Uptrace's own "Notify on all errors" would only duplicate ours
        if not monitor["name"].startswith(NAME_PREFIX) and monitor.get("status") == "active":
            admin.pause_monitor(uptrace_project_id, monitor["id"])
    our_monitors, extra_monitors = _by_name(monitors)
    our_channels, extra_channels = _by_name(admin.channels(uptrace_project_id))

    keep = set()
    for member in members:
        name = monitor_name(member.id)
        member.uptrace_source_id = source_id(uptrace_project_id)
        member.uptrace_dsn_encrypted = encrypt(dsn)
        if shared and not member.service_names:
            member.uptrace_status, member.uptrace_error = UptraceStatus.ERROR, SHARED_NEEDS_SERVICES
        else:
            try:
                monitor_id = admin.upsert_monitor(uptrace_project_id, our_monitors.get(name), name,
                                                  monitor_query(member.service_names if shared else None))
                admin.upsert_channel(uptrace_project_id, our_channels.get(name), name,
                                     webhook_url(base_url, member), monitor_id)
                keep.add(name)
                member.uptrace_status, member.uptrace_error = UptraceStatus.READY, ""
            except UptraceAdminError as exc:
                if "sendTestMessage" in str(exc):
                    # Uptrace couldn't reach our webhook; retrying won't change that.
                    exc = UptraceAdminError(
                        f"Uptrace couldn't deliver a test alert to {base_url.rstrip('/')}: is this "
                        f"server reachable from Uptrace? ({exc})")
                member.uptrace_status, member.uptrace_error = UptraceStatus.ERROR, str(exc)[:500]
        member.save(update_fields=["uptrace_source_id", "uptrace_dsn_encrypted", "uptrace_status",
                                   "uptrace_error", "updated_at"])

    # Projects that left or were deleted, projects that can't be told apart, and duplicates.
    for monitor in [m for n, m in our_monitors.items() if n not in keep] + extra_monitors:
        admin.delete_monitor(uptrace_project_id, monitor["id"])
    for channel in [c for n, c in our_channels.items() if n not in keep] + extra_channels:
        admin.delete_channel(uptrace_project_id, channel["id"])


def mark_error(project_ids: list[int], message: str) -> None:
    Project.objects.filter(id__in=project_ids, uptrace_managed=True).update(
        uptrace_status=UptraceStatus.ERROR, uptrace_error=message[:500])


def resolve_open_alerts(project: Project) -> int:
    """Resolves the project's open alerts in its Uptrace project, so the next occurrence of
    each error reopens it and starts a new incident (what testing a fix again needs).
    Only the project's own monitor's alerts: a shared Uptrace project's other alerts stay."""
    if not is_managed(project) or not project.uptrace_project_id:
        raise UptraceAdminError("This project's Uptrace isn't managed by the platform")
    admin, upid = UptraceAdmin(), project.uptrace_project_id
    ours = {m["id"] for m in admin.monitors(upid) if m["name"] == monitor_name(project.id)}
    alerts = admin.call("GET", f"/alerts/{upid}?limit=100").get("alerts", [])
    open_ids = [a["id"] for a in alerts if a.get("monitorId") in ours
                and (a.get("event") or {}).get("status") == "unresolved"]
    if open_ids:
        admin.call("PUT", f"/alerts/{upid}/resolve", {"ids": open_ids})
    return len(open_ids)
