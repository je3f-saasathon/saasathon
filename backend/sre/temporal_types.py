"""Workflow/activity payloads. No Django imports: the workflow sandbox loads this module."""

from dataclasses import dataclass, field

MAX_ATTEMPTS = 3
FAILING_THRESHOLD = 3
# Remediation agents: repos scanned at once within one scan run.
MAX_PARALLEL_REPOS = 2


@dataclass
class IncidentInput:
    incident_run_id: int
    project_id: int


@dataclass
class AnomalyResult:
    is_anomaly: bool
    reasoning: str


@dataclass
class Classification:
    category: str
    severity: str
    summary: str
    keywords: list[str] = field(default_factory=list)
    suspected_files: list[str] = field(default_factory=list)


@dataclass
class SearchInput:
    project_id: int
    keywords: list[str]
    # New fields default, so payloads from older workflow histories still decode.
    incident_run_id: int = 0
    category: str = ""


@dataclass
class Candidates:
    """find_candidates: the runbooks and playbooks worth showing the judge."""
    playbook_ids: list[int] = field(default_factory=list)
    runbook_ids: list[int] = field(default_factory=list)


@dataclass
class RunInput:
    incident_run_id: int
    playbook_id: int
    runbook_id: int | None = None


@dataclass
class PlaybookRunStatus:
    playbook_run_id: int
    status: str


@dataclass
class JudgeInput:
    incident_run_id: int
    candidate_ids: list[int]  # playbooks
    runbook_ids: list[int] = field(default_factory=list)


@dataclass
class JudgeResult:
    matched_playbook_id: int | None  # for a runbook match, the runbook's playbook
    confidence: float
    reasoning: str
    matched_runbook_id: int | None = None


@dataclass
class PlaybookRunInfo:
    playbook_run_id: int
    execution_mode: str


@dataclass
class AttemptInput:
    playbook_run_id: int
    attempt_number: int
    previous_feedback: str


@dataclass
class AttemptResult:
    outcome: str  # "succeeded" | "failed"
    error_output: str
    branch_name: str
    pr_url: str = ""
    summary: str = ""


@dataclass
class ApprovalDecision:
    approve: bool
    user_id: int  # 0 when decided on GitHub (kept an int so older workers can still decode it)
    # True when the PR was merged/closed on GitHub: the PR is already in its final state,
    # so the workflow mustn't mark it ready or close it again.
    via_github: bool = False


@dataclass
class RootCauseResult:
    """localize_root_cause: the linked child incident to start, if the culprit is elsewhere."""
    child_incident_run_id: int | None = None
    child_project_id: int = 0
    child_workflow_id: str = ""


@dataclass
class GraphTarget:
    """One service graph: an org and a pinned Uptrace project ("host/project id")."""
    organization_id: int
    source: str


@dataclass
class GraphRefreshInput:
    organization_id: int = 0  # 0 = every org
    source: str = ""  # "" = every source the org's projects use


@dataclass
class ScanInput:
    scan_run_id: int = 0
    agent_id: int = 0  # a scheduled run: create the scan run for this agent first


@dataclass
class ScanFinding:
    """A new scan incident whose pipeline the scan workflow starts."""
    incident_run_id: int
    project_id: int
    workflow_id: str


@dataclass
class StatusUpdate:
    incident_run_id: int
    status: str
    error_message: str = ""
