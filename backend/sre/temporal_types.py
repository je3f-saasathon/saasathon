"""Workflow/activity payloads. No Django imports: the workflow sandbox loads this module."""

from dataclasses import dataclass, field

MAX_ATTEMPTS = 3
FAILING_THRESHOLD = 3


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


@dataclass
class RunInput:
    incident_run_id: int
    playbook_id: int


@dataclass
class PlaybookRunStatus:
    playbook_run_id: int
    status: str


@dataclass
class JudgeInput:
    incident_run_id: int
    candidate_ids: list[int]


@dataclass
class JudgeResult:
    matched_playbook_id: int | None
    confidence: float
    reasoning: str


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
    user_id: int


@dataclass
class StatusUpdate:
    incident_run_id: int
    status: str
    error_message: str = ""
