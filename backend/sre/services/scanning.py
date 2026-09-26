"""Remediation agents' scanner: one repo, read-only, looking for bugs before they alert.

Each kind gives the scanner different material (docs/MESH_AND_REMEDIATION.md):
  playbook_sweep   the playbooks to hunt for
  runbook_variant  runbooks that fixed bugs here or in neighbouring repos: find the same bug
                   elsewhere
  find_quiet       Uptrace error groups for this repo's services that never became incidents
Each finding becomes an incident (source=scan) that the usual pipeline verifies, matches and
fixes, with its execution mode capped by how strong the finding's evidence is.
"""

import ast
import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from ..llm.clients import LLMError, client_for, parse_json
from ..llm.resolve import get_llm_config
from ..models import (
    AgentKind, ExecutionMode, IncidentRun, LLMUsage, PipelineStep, Playbook, Project, Runbook,
    ScanRepo, ScanTrigger, ServiceGraph, ServiceNode, latest_incident_run, next_incident_key,
    rejected_fix_recurred,
)
from . import mesh
from .context import UNTRUSTED_NOTICE, untrusted
from .github import GitError, GitHubRepo
from .knowledge import playbook_card
from .sandbox import Sandbox, SandboxError
from .triage import CATEGORIES
from .uptrace import UptraceError, client_for as uptrace_client_for

logger = logging.getLogger(__name__)

MAX_TURNS = 30
MAX_PLAYBOOKS = 30
MAX_RUNBOOKS = 15
MAX_QUIET_GROUPS = 10
QUIET_WINDOW = timedelta(days=7)

MODE_RANK = {ExecutionMode.ADVISORY_ONLY: 0, ExecutionMode.DRAFT_ONLY: 1, ExecutionMode.AUTONOMOUS: 2}
# How far a fix may go on the finding's evidence alone. Scans never run autonomous: no
# production alert confirmed the bug.
EVIDENCE_CAP = {
    "runbook": ExecutionMode.DRAFT_ONLY,  # the same bug a person-approved fix already fixed
    "trace": ExecutionMode.DRAFT_ONLY,  # an error really happening in production
    "code": ExecutionMode.ADVISORY_ONLY,  # the scanner's reading of the code
}


def evidence_cap(evidence_kind: str, agent) -> str:
    """How far a finding may go on its evidence. An agent can let code-only findings reach
    draft PRs (code_findings_open_prs): a person still reviews every one."""
    if evidence_kind == "code" and agent.code_findings_open_prs:
        return ExecutionMode.DRAFT_ONLY
    return EVIDENCE_CAP[evidence_kind]


class ScanError(Exception):
    pass


class ScanSkipped(Exception):
    """Nothing to scan (e.g. no new commits since this agent last scanned the repo)."""


def lower_mode(a: str, b: str) -> str:
    return a if MODE_RANK[a] <= MODE_RANK[b] else b


def month_start():
    return timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def agent_usage_this_month(agent):
    return LLMUsage.objects.filter(scan_repo__scan_run__agent=agent, created_at__gte=month_start())


def agent_tokens_this_month(agent) -> int:
    totals = agent_usage_this_month(agent).aggregate(input=Sum("input_tokens"), output=Sum("output_tokens"))
    return (totals["input"] or 0) + (totals["output"] or 0)


def services_of(project: Project) -> list[str]:
    """The service.names this repo runs: its own list plus mesh nodes mapped to it."""
    names = list(project.service_names or [])
    graph = ServiceGraph.objects.filter(organization_id=project.organization_id,
                                        source=project.uptrace_source_id).first() \
        if project.uptrace_source_id else None
    if graph is not None:
        projects = mesh.mesh_projects(project.organization_id, project.uptrace_source_id)
        for node in graph.nodes.filter(kind=ServiceNode.Kind.SERVICE):
            mapped, _ = mesh.map_service(node.name, node.repo_url, projects)
            if mapped is not None and mapped.id == project.id and node.name not in names:
                names.append(node.name)
    return names


@dataclass
class Material:
    """What the scanner is shown, and the ids a finding may refer back to."""
    label: str
    instructions: str
    data: list = field(default_factory=list)
    playbook_ids: set[int] = field(default_factory=set)
    runbook_ids: set[int] = field(default_factory=set)
    groups: dict[str, dict] = field(default_factory=dict)


def sweep_material(agent, project: Project) -> Material:
    from .playbooks import visible_playbooks

    playbooks = visible_playbooks(project).exclude(status=Playbook.Status.FAILING)
    chosen = agent.playbooks.all()
    if chosen.exists():
        playbooks = playbooks.filter(id__in=chosen.values("id"))
    cards = [playbook_card(p) for p in playbooks.order_by("id")[:MAX_PLAYBOOKS]]
    return Material(
        "playbooks",
        "Hunt for bugs of the classes these playbooks describe. Set playbook_id on each finding "
        "to the playbook it matches.",
        cards, playbook_ids={c["id"] for c in cards},
    )


def variant_material(agent, project: Project) -> Material:
    """Runbooks from this repo and the org's other repos on the same GitHub account
    (whose code this repo's reviewers can see), neighbours in the service mesh first."""
    candidates = Runbook.objects.filter(
        project__organization_id=project.organization_id,
        project__github_installation_id=project.github_installation_id,
    ).exclude(status=Playbook.Status.FAILING).select_related("project")
    neighbours = {p.id for p in mesh.neighbour_projects(
        project, (services_of(project) or [""])[0], limit=10)}

    def rank(runbook):
        where = 0 if runbook.project_id in neighbours else 1 if runbook.project_id == project.id else 2
        confirmed = 0 if runbook.status == Playbook.Status.CONFIRMED else 1
        return where, confirmed, -runbook.id

    runbooks = sorted(candidates, key=rank)[:MAX_RUNBOOKS]
    cards = [{"id": r.id, "playbook_id": r.playbook_id, "title": r.title, "area": r.area,
              "repo": f"{r.project.github_repo_owner}/{r.project.github_repo_name}",
              "service": r.service_name, "description": r.description[:600], "steps": r.steps[:10]}
             for r in runbooks]
    return Material(
        "runbooks",
        "Each runbook fixed a real bug, in this repo or a related one. Look for the SAME bug "
        "pattern in this repo in places the fix did not cover (another handler, another "
        "client, a copy of the same code). Set runbook_id on each finding to the runbook whose "
        "bug it repeats.",
        cards, playbook_ids={r.playbook_id for r in runbooks}, runbook_ids={r.id for r in runbooks},
    )


def quiet_material(project: Project) -> Material:
    """Error groups of this repo's services that no incident has been raised for."""
    material = Material(
        "quiet_errors",
        "These errors are happening in production in this repo's services but never became "
        "an incident. Find the code that causes each one that looks like a real bug. Set "
        "group_id on each finding to the error group it explains.",
    )
    services = set(services_of(project))
    client = uptrace_client_for(project)
    if not services or client is None:
        return material
    now = timezone.now()
    try:
        groups = [g for g in client.error_groups(now - QUIET_WINDOW, now) if g["service_name"] in services]
    except UptraceError as exc:
        raise ScanError(f"Uptrace: {exc}") from exc
    # Compared in Python: SQLite's JSON lookups turn "123" into a number, and Uptrace's
    # group ids are unsigned 64-bit, past what a signed integer holds.
    raised = {str(t.get("group_id") or "") for t in IncidentRun.objects.filter(
        project__organization_id=project.organization_id, telemetry__has_key="group_id",
    ).values_list("telemetry", flat=True)}
    groups = [g for g in groups if g["group_id"] not in raised][:MAX_QUIET_GROUPS]
    material.data = groups
    material.groups = {g["group_id"]: g for g in groups}
    return material


def scanner_system(max_findings: int) -> str:
    return (
        "You are an SRE agent looking for bugs in a repository before they cause production "
        "incidents. The repository is checked out read-only at /workspace. Report only real "
        "bugs you can point to in the code, with concrete evidence: no style issues, no "
        "missing tests, no speculative hardening. " + UNTRUSTED_NOTICE + "\n\n"
        "Each reply must be exactly one JSON object choosing one action:\n"
        '{"action": "list_files", "path": "."}\n'
        '{"action": "read_file", "path": "src/app.py"}\n'
        '{"action": "run_command", "command": "grep -rn \\"requests.get(\\" src"}\n'
        '{"action": "finish", "findings": [{"category": one of '
        + ", ".join(f'"{c}"' for c in CATEGORIES) + ', "title": "<short>", '
        '"message": "<what goes wrong, and when>", "location": "<path>:<line>", '
        '"evidence": "<the code, and why it fails>", "playbook_id": <id or null>, '
        '"runbook_id": <id or null>, "group_id": "<error group id or null>"}]}\n'
        f"Report at most {max_findings} findings, the most severe first, or finish with an "
        "empty list if you found none. Commands can't change files or reach the network."
    )


@dataclass
class Finding:
    category: str
    title: str
    message: str
    location: str
    evidence: str
    evidence_kind: str
    playbook_id: int | None = None
    runbook_id: int | None = None
    group: dict | None = None
    symbol: str = ""  # the function or class around the finding's line, when found

    @property
    def fingerprint(self) -> str:
        """The same bug found by a later scan keeps the same fingerprint (and so the same
        incident): the scanner's line numbers and categories drift between scans, so it's
        the playbook (else the category), the file and the enclosing function."""
        if self.group:
            return self.group["group_id"]
        anchor = f"p{self.playbook_id}" if self.playbook_id else self.category
        path = self.location.split(":", 1)[0]
        return hashlib.sha1(f"{anchor}|{path}|{self.symbol}".encode()).hexdigest()[:16]


def enclosing_symbol(file: Path, location: str) -> str:
    """"Class.method" or "function" around the location's line in a Python file ("" if
    there's no line, the file isn't Python or doesn't parse, or the line is at module level).
    Parsed, never run."""
    _, _, line = location.partition(":")
    line_no = _int_or_none(line.split(":", 1)[0].split("-", 1)[0].strip())
    if not line_no or file.suffix != ".py":
        return ""
    try:
        tree = ast.parse(file.read_text(errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return ""
    names: list[str] = []

    def visit(node, prefix: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                end = getattr(child, "end_lineno", None) or child.lineno
                if child.lineno <= line_no <= end:
                    names[:] = prefix + [child.name]
                    visit(child, names[:])

    visit(tree, [])
    return ".".join(names)


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def clean_findings(raw, material: Material, kind: str, work_tree: Path, limit: int) -> list[Finding]:
    """Keeps findings that point at a real file, and only ids the scanner was shown."""
    findings: list[Finding] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        location = str(item.get("location") or "").strip().lstrip("/").removeprefix("workspace/")
        path = location.split(":", 1)[0]
        target = (work_tree / path).resolve()
        if not path or not target.is_relative_to(work_tree.resolve()) or not target.is_file():
            continue
        category = item.get("category") if item.get("category") in CATEGORIES else "other"
        playbook_id = _int_or_none(item.get("playbook_id"))
        runbook_id = _int_or_none(item.get("runbook_id"))
        group = material.groups.get(str(item.get("group_id") or ""))
        playbook_id = playbook_id if playbook_id in material.playbook_ids else None
        runbook_id = runbook_id if runbook_id in material.runbook_ids else None
        if kind == AgentKind.RUNBOOK_VARIANT and runbook_id is not None:
            evidence_kind = "runbook"
        elif kind == AgentKind.FIND_QUIET and group is not None:
            evidence_kind = "trace"
        else:
            evidence_kind = "code"
        findings.append(Finding(
            category=category, title=str(item.get("title") or "")[:200],
            message=str(item.get("message") or "")[:2000], location=location[:500],
            evidence=str(item.get("evidence") or "")[:4000], evidence_kind=evidence_kind,
            playbook_id=playbook_id, runbook_id=runbook_id, group=group,
            symbol=enclosing_symbol(target, location),
        ))
    # The scanner can report the same bug twice in one reply (two lines of one function).
    unique = {f.fingerprint: f for f in reversed(findings)}
    return [f for f in findings if unique.get(f.fingerprint) is f][:limit]


class RepositoryScanner:
    """One repo of a scan run: clone, let the scanner read (read-only, offline), record
    what it found. Returns the new scan incidents; the workflow starts their pipelines."""

    def __init__(self, scan_repo: ScanRepo, heartbeat: Callable[..., None] = lambda *a: None):
        self.scan_repo = scan_repo
        self.scan_run = scan_repo.scan_run
        self.agent = self.scan_run.agent
        self.project = scan_repo.project
        self.heartbeat = heartbeat

    def material(self) -> Material:
        if self.agent.kind == AgentKind.RUNBOOK_VARIANT:
            return variant_material(self.agent, self.project)
        if self.agent.kind == AgentKind.FIND_QUIET:
            return quiet_material(self.project)
        return sweep_material(self.agent, self.project)

    def scan(self) -> list[IncidentRun]:
        budget = self.agent.monthly_token_budget
        if budget and agent_tokens_this_month(self.agent) >= budget:
            raise ScanError(f"This agent has used its monthly token budget ({budget})")
        material = self.material()
        if not material.data:
            return []  # nothing to hunt for: no tokens spent
        repo = GitHubRepo(self.project)
        diff = None
        if self.scan_repo.base_sha and self.scan_repo.head_sha:
            diff = repo.compare(self.scan_repo.base_sha, self.scan_repo.head_sha)
            if not diff:
                return []
        elif self.scan_run.trigger == ScanTrigger.SCHEDULE:
            self._skip_if_scanned(repo)
        workdir = Path(settings.SRE_WORKDIR) / f"scan-{self.scan_repo.id}"
        shutil.rmtree(workdir, ignore_errors=True)
        try:
            workdir.mkdir(parents=True)
            work_tree = workdir / "tree"
            repo.clone_snapshot(work_tree, self.scan_repo.branch)
            self.heartbeat("cloned")
            with Sandbox(work_tree, name=f"sre-scan-{self.scan_repo.id}", network="none",
                         read_only=True) as box:
                raw = self._agent_loop(box, material, diff)
            findings = clean_findings(raw, material, self.agent.kind, work_tree,
                                      self.agent.max_findings_per_repo)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        return self.record(findings)

    def _skip_if_scanned(self, repo: GitHubRepo) -> None:
        """A scheduled whole-repo scan of a commit this agent already scanned would only
        find the same bugs again: records the commit and skips it."""
        branch = self.scan_repo.branch or self.project.github_default_branch
        head = repo.head_sha(branch)
        ScanRepo.objects.filter(id=self.scan_repo.id).update(head_sha=head[:64])
        earlier = ScanRepo.objects.filter(
            scan_run__agent=self.agent, project=self.project, branch=self.scan_repo.branch,
            base_sha="", head_sha=head[:64], status=ScanRepo.Status.SUCCEEDED,
        ).exclude(id=self.scan_repo.id).order_by("-id").first()
        if earlier is not None:
            raise ScanSkipped(f"No new commits on {branch} since scan run {earlier.scan_run_id}")

    def _kickoff(self, material: Material, diff: list[dict] | None) -> str:
        repo = f"{self.project.github_repo_owner}/{self.project.github_repo_name}"
        text = (f"Repository: {repo}. Services it runs: {', '.join(services_of(self.project)) or 'unknown'}."
                f"\n\n{material.instructions}\n" + untrusted(material.label, material.data))
        if diff is not None:
            text += ("\n\nOnly look for bugs this change introduced or touches (read the "
                     "surrounding code as needed):\n" + untrusted("diff", diff))
        return text

    def _agent_loop(self, box: Sandbox, material: Material, diff: list[dict] | None) -> list:
        client = client_for(get_llm_config(self.project, PipelineStep.REPOSITORY_SCAN))
        system = scanner_system(self.agent.max_findings_per_repo)
        messages = [{"role": "user", "content": self._kickoff(material, diff)}]
        for turn in range(MAX_TURNS):
            self.heartbeat(f"turn {turn}")
            reply = client.chat(system, messages, name=f"scan_turn_{turn}")
            messages.append({"role": "assistant", "content": reply})
            try:
                action = parse_json(reply)
            except LLMError as exc:
                messages.append({"role": "user", "content": f"Invalid reply: {exc}. Reply with one JSON action."})
                continue
            if action.get("action") == "finish":
                return action.get("findings") or []
            messages.append({"role": "user", "content": untrusted("tool_result", self._do(box, action))})
        raise ScanError(f"The scanner ran out of turns after {MAX_TURNS} steps")

    def _do(self, box: Sandbox, action: dict) -> str:
        kind = action.get("action")
        try:
            if kind == "list_files":
                return box.list_files(str(action.get("path", ".")))
            if kind == "read_file":
                return box.read_file(str(action["path"]))
            if kind == "run_command":
                exit_code, output = box.run(str(action["command"]))
                return f"exit code {exit_code}\n{output}"
        except KeyError as exc:
            return f"missing field {exc}"
        except SandboxError as exc:
            return f"error: {exc}"
        return f"unknown action {kind!r}"

    def record(self, findings: list[Finding]) -> list[IncidentRun]:
        """One incident per finding never raised before (the workflow id is the finding's
        fingerprint, so a later scan finding the same bug adds nothing) - unless the last
        incident for it was rejected: that fix was never merged, so the bug is still in the
        repo and finding it again is new work. Recurrences are keyed {fingerprint}-r2, -r3, ..."""
        created = []
        services = services_of(self.project)
        for finding in findings:
            base = f"scan-{self.agent.kind}-{finding.fingerprint}"
            latest, count = latest_incident_run(self.project, base)
            key = next_incident_key(base, count) if rejected_fix_recurred(latest) else base
            cap = lower_mode(self.agent.execution_mode, evidence_cap(finding.evidence_kind, self.agent))
            group = finding.group or {}
            run, is_new = IncidentRun.objects.get_or_create(
                temporal_workflow_id=f"sre-incident-{self.project.id}-{key}",
                defaults={
                    "project": self.project,
                    "trace_id": key,
                    "source": IncidentRun.Source.SCAN,
                    "scan_run": self.scan_run,
                    "scan_kind": self.agent.kind,
                    "execution_mode_cap": cap,
                    "raw_webhook_payload": {"scan_run_id": self.scan_run.id, "agent": self.agent.name},
                    "telemetry": {
                        "exception_type": finding.category,
                        "title": finding.title,
                        "message": finding.message,
                        "location": finding.location,
                        "evidence": finding.evidence,
                        "evidence_kind": finding.evidence_kind,
                        "service_name": group.get("service_name") or (services[0] if services else ""),
                        "suggested_playbook_id": finding.playbook_id,
                        "suggested_runbook_id": finding.runbook_id,
                        "group_id": group.get("group_id", ""),
                        "trace_id": group.get("trace_id", ""),
                    },
                },
            )
            if is_new:
                created.append(run)
        return created


def scan_repository(scan_repo: ScanRepo, heartbeat=lambda *a: None) -> list[IncidentRun]:
    """Runs the scan and records its outcome on the ScanRepo. Ordinary failures (git, the
    sandbox, the model, the budget) mark the repo failed instead of raising."""
    ScanRepo.objects.filter(id=scan_repo.id).update(status=ScanRepo.Status.RUNNING, updated_at=timezone.now())
    try:
        created = RepositoryScanner(scan_repo, heartbeat).scan()
    except ScanSkipped as exc:
        ScanRepo.objects.filter(id=scan_repo.id).update(
            status=ScanRepo.Status.SKIPPED, error=str(exc)[:5000], updated_at=timezone.now())
        return []
    except (ScanError, GitError, SandboxError, LLMError) as exc:
        ScanRepo.objects.filter(id=scan_repo.id).update(
            status=ScanRepo.Status.FAILED, error=str(exc)[:5000], updated_at=timezone.now())
        return []
    except Exception as exc:
        logger.exception("scan of repo %s failed", scan_repo.id)
        ScanRepo.objects.filter(id=scan_repo.id).update(
            status=ScanRepo.Status.FAILED, error=f"{type(exc).__name__}: {exc}"[:5000],
            updated_at=timezone.now())
        raise
    ScanRepo.objects.filter(id=scan_repo.id).update(
        status=ScanRepo.Status.SUCCEEDED, finding_count=len(created), updated_at=timezone.now())
    return created


def create_scan_run(agent, trigger: str, trigger_ref: str, workflow_id: str,
                    repos: list[tuple[Project, str, str, str]] | None = None):
    """A scan run and its repos: `repos` is [(project, branch, base_sha, head_sha)], or
    None for every covered project's default branch, whole repo."""
    from ..models import ScanRun

    scan_run = ScanRun.objects.create(agent=agent, trigger=trigger, trigger_ref=trigger_ref[:255],
                                      temporal_workflow_id=workflow_id)
    if repos is None:
        repos = [(p, p.github_default_branch, "", "") for p in agent.covered_projects()]
    for project, branch, base, head in repos:
        ScanRepo.objects.create(scan_run=scan_run, project=project, branch=branch[:255],
                                base_sha=base[:64], head_sha=head[:64])
    return scan_run


def finish_scan_run(scan_run) -> str:
    """succeeded when no repo failed, failed when none succeeded, else partial. Repos the
    workflow never finished count as failed."""
    from ..models import ScanRun

    scan_run.repos.filter(status__in=[ScanRepo.Status.PENDING, ScanRepo.Status.RUNNING]).update(
        status=ScanRepo.Status.FAILED, error="The scan ended before this repo finished")
    statuses = list(scan_run.repos.values_list("status", flat=True))
    failed = statuses.count(ScanRepo.Status.FAILED)
    if not failed:
        status = ScanRun.Status.SUCCEEDED
    elif failed == len(statuses):
        status = ScanRun.Status.FAILED
    else:
        status = ScanRun.Status.PARTIAL
    scan_run.status, scan_run.finished_at = status, timezone.now()
    scan_run.save(update_fields=["status", "finished_at"])
    return status
