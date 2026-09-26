from datetime import datetime

from ninja import Schema

from .models import (
    AgentKind, AgentTrigger, ExecutionMode, LLMProvider, OrgRole, PipelineStep, Playbook, ProjectRole,
    Runbook, ScanTrigger,
)


class UptraceWebhookIn(Schema):
    """Two shapes are accepted: Uptrace's own alert notification
    ({id, eventName, payload, createdAt, alert: {id, url, name, type, state, createdAt}}),
    or a direct call with a trace_id (scripts, other tools)."""
    trace_id: str | None = None
    exception_id: str | None = None
    source_id: str | None = None
    payload: dict | None = None  # Uptrace 2.1 sends null when the channel has no custom payload
    eventName: str | None = None
    alert: dict | None = None


class UptraceWebhookOut(Schema):
    incident_run_id: int
    temporal_workflow_id: str
    status: str


class ProjectOut(Schema):
    id: int
    name: str
    role: ProjectRole
    github_installation_id: str
    github_repo_owner: str
    github_repo_name: str
    github_default_branch: str
    uptrace_source_id: str
    default_execution_mode: ExecutionMode
    default_llm_config_id: int | None
    generate_tests: bool
    # Company-model mix used when no own config is picked (see GET /sre/platform presets).
    platform_preset: str
    # Tokens billed to the company default (our keys) this calendar month.
    platform_tokens_this_month: int
    created_at: datetime
    # True when an owner has proved (via Connect GitHub) access to the installation.
    github_verified: bool
    organization_id: int | None
    organization_name: str
    uptrace_credential_id: int | None
    # True when an Uptrace credential resolves (own, else the org's for the pinned host).
    uptrace_fetch_ready: bool
    # Uptrace service.names this repo runs (service mesh fallback mapping).
    service_names: list[str]
    # Managed Uptrace: the platform set up this project's Uptrace side itself.
    uptrace_managed: bool
    # "" (not managed) · provisioning · ready · error (see uptrace_error)
    uptrace_status: str
    uptrace_error: str
    uptrace_project_id: int | None
    # Where the project's apps send telemetry. Admins and owners only ("" otherwise).
    uptrace_dsn: str
    # Other projects in the same Uptrace project (one trace across linked services).
    uptrace_shared_with: list["ProjectRefOut"]


class ProjectRefOut(Schema):
    id: int
    name: str


class UptraceSetupIn(Schema):
    # Share this project's Uptrace project; null = one of its own.
    share_with_project_id: int | None = None


class ManagedUptraceOut(Schema):
    enabled: bool
    url: str


class ProjectCreatedOut(ProjectOut):
    webhook_secret: str
    webhook_url: str
    # webhook_url with the secret as ?token=: what to paste into Uptrace, whose
    # webhook channel can only take a URL (no headers).
    uptrace_webhook_url: str


class WebhookSecretOut(Schema):
    webhook_secret: str
    webhook_url: str
    # webhook_url with the secret as ?token=: what to paste into Uptrace, whose
    # webhook channel can only take a URL (no headers).
    uptrace_webhook_url: str


class ProjectCreateIn(Schema):
    name: str
    github_installation_id: str
    github_repo_owner: str
    github_repo_name: str
    github_default_branch: str = "main"
    uptrace_source_id: str = ""
    default_execution_mode: ExecutionMode = ExecutionMode.DRAFT_ONLY
    generate_tests: bool = True
    # Defaults to the caller's personal org.
    organization_id: int | None = None
    service_names: list[str] = []
    # Managed Uptrace: share this existing project's Uptrace project instead of getting a
    # new one (both need service_names). Ignored when managed Uptrace is off.
    uptrace_share_with_project_id: int | None = None
    platform_preset: str = "openai_jev"


class ProjectUpdateIn(Schema):
    name: str | None = None
    github_installation_id: str | None = None
    github_repo_owner: str | None = None
    github_repo_name: str | None = None
    github_default_branch: str | None = None
    uptrace_source_id: str | None = None
    default_execution_mode: ExecutionMode | None = None
    default_llm_config_id: int | None = None
    generate_tests: bool | None = None
    organization_id: int | None = None
    uptrace_credential_id: int | None = None
    service_names: list[str] | None = None
    platform_preset: str | None = None


class OrganizationOut(Schema):
    id: int
    name: str
    is_personal: bool
    role: OrgRole  # the caller's
    created_at: datetime


class OrganizationIn(Schema):
    name: str


class OrgMemberOut(Schema):
    user_id: int
    email: str
    name: str
    role: OrgRole


class OrgMemberAddIn(Schema):
    email: str
    role: OrgRole


class OrgMemberUpdateIn(Schema):
    role: OrgRole


class UptraceCredentialOut(Schema):
    id: int
    organization_id: int
    name: str
    host: str
    api_base_url: str
    has_token: bool
    created_by_id: int | None
    created_at: datetime
    updated_at: datetime


class UptraceCredentialIn(Schema):
    name: str
    host: str
    api_base_url: str
    token: str


class UptraceCredentialUpdateIn(Schema):
    name: str | None = None
    host: str | None = None
    api_base_url: str | None = None
    token: str | None = None  # a new value rotates it


class MemberOut(Schema):
    user_id: int
    email: str
    name: str
    role: ProjectRole


class MemberAddIn(Schema):
    email: str
    role: ProjectRole


class MemberUpdateIn(Schema):
    role: ProjectRole


class LLMConfigOut(Schema):
    id: int
    name: str
    provider: LLMProvider
    model: str
    base_url: str
    has_api_key: bool
    extra_config: dict
    created_at: datetime


class LLMConfigIn(Schema):
    name: str
    provider: LLMProvider
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    extra_config: dict = {}


class LLMConfigUpdateIn(Schema):
    name: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # "" clears the stored key
    extra_config: dict | None = None


class StepOverrideOut(Schema):
    step: PipelineStep
    llm_config_id: int
    llm_config_name: str


class StepOverridesIn(Schema):
    # step -> llm_config_id, or null to remove the override. Unlisted steps are unchanged.
    overrides: dict[PipelineStep, int | None]


class PlaybookOut(Schema):
    id: int
    project_id: int | None
    organization_id: int | None
    origin: Playbook.Origin
    created_by_id: int | None
    is_generic: bool
    category: str
    symptoms: str
    title: str
    description: str
    keywords: list[str]
    steps: list[dict]
    status: Playbook.Status
    execution_mode_override: ExecutionMode | None
    consecutive_failure_count: int
    source_incident_run_id: int | None
    created_at: datetime
    updated_at: datetime


class PlaybookListOut(Schema):
    playbooks: list[PlaybookOut]
    total: int


class PlaybookCreateIn(Schema):
    title: str
    description: str = ""
    keywords: list[str] = []
    steps: list[dict] = []
    execution_mode_override: ExecutionMode | None = None
    category: str = ""
    symptoms: str = ""


class PlaybookUpdateIn(Schema):
    title: str | None = None
    description: str | None = None
    keywords: list[str] | None = None
    steps: list[dict] | None = None
    status: Playbook.Status | None = None
    execution_mode_override: ExecutionMode | None = None
    category: str | None = None
    symptoms: str | None = None


class RunbookOut(Schema):
    id: int
    project_id: int
    playbook_id: int
    title: str
    description: str
    area: str
    keywords: list[str]
    steps: list[dict]
    status: Playbook.Status
    origin: Runbook.Origin
    created_by_id: int | None
    repo_owner: str
    repo_name: str
    service_name: str
    consecutive_failure_count: int
    source_playbook_run_id: int | None
    created_at: datetime
    updated_at: datetime


class RunbookListOut(Schema):
    runbooks: list[RunbookOut]
    total: int


class RunbookCreateIn(Schema):
    playbook_id: int
    title: str
    description: str = ""
    area: str = ""
    keywords: list[str] = []
    steps: list[dict] = []
    service_name: str = ""


class RunbookUpdateIn(Schema):
    playbook_id: int | None = None
    title: str | None = None
    description: str | None = None
    area: str | None = None
    keywords: list[str] | None = None
    steps: list[dict] | None = None
    service_name: str | None = None
    status: Playbook.Status | None = None


class AttemptOut(Schema):
    attempt_number: int
    outcome: str
    summary: str
    error_output: str
    generated_steps: list[dict]
    branch_name: str
    langfuse_trace_id: str
    created_at: datetime


class PlaybookRunOut(Schema):
    id: int
    incident_run_id: int
    playbook_id: int
    runbook_id: int | None
    execution_mode: ExecutionMode
    generate_tests: bool
    status: str
    approved_by_id: int | None
    approved_at: datetime | None
    pr_url: str
    branch_name: str
    attempts: list[AttemptOut]


class PlaybookBriefOut(Schema):
    id: int
    title: str
    status: Playbook.Status
    source: str  # "matched" (reused) or "created" (written from this incident)


class StepUsageOut(Schema):
    step: str
    provider: str
    model: str
    billed_to: str  # "platform" (our keys) or "user"
    calls: int
    input_tokens: int
    cached_input_tokens: int  # the part of input_tokens served from the provider's prompt cache
    output_tokens: int


class UsageOut(Schema):
    calls: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_tokens: int
    platform_tokens: int  # the part of total_tokens billed to our keys
    models: list[str]
    by_step: list[StepUsageOut]


class IncidentRunOut(Schema):
    id: int
    project_id: int
    trace_id: str
    uptrace_exception_id: str
    temporal_workflow_id: str
    status: str
    classification: dict | None
    matched_playbook_id: int | None
    created_playbook_id: int | None
    playbook_run_id: int | None
    diagnosis_report: str
    error_message: str
    created_at: datetime
    updated_at: datetime
    project_name: str
    playbook: PlaybookBriefOut | None
    pr_url: str
    playbook_run_status: str | None
    execution_mode: ExecutionMode | None
    generate_tests: bool | None  # frozen on the playbook run; None without one
    usage: UsageOut
    matched_runbook_id: int | None
    runbook: PlaybookBriefOut | None  # matched, else saved from this incident's fix
    telemetry: dict
    source: str  # alert | linked | scan
    parent_incident_run_id: int | None
    root_cause: dict  # the service mesh's trace walk; {} when not run
    scan_run_id: int | None
    scan_kind: str


class IncidentRunListOut(Schema):
    runs: list[IncidentRunOut]
    total: int


class ApprovePlaybookRunIn(Schema):
    approve: bool


class GitHubStatusOut(Schema):
    configured: bool
    app_slug: str


class GitHubConnectOut(Schema):
    install_url: str
    authorize_url: str


class GitHubInstallationOut(Schema):
    id: int
    installation_id: str
    account_login: str
    account_type: str


class GitHubRepoOut(Schema):
    owner: str
    name: str
    default_branch: str
    private: bool


class PlatformPresetOut(Schema):
    key: str
    label: str
    triage_model: str  # "jev", or a chat model
    strong_model: str


class PlatformOut(Schema):
    available: bool  # false = no company default; projects must bring their own model
    triage_model: str  # of the default preset: "jev", or the fast chat model
    strong_model: str
    monthly_token_cap: int  # per project; 0 = unlimited
    default_preset: str
    presets: list[PlatformPresetOut]


class ServiceNodeOut(Schema):
    id: int
    name: str
    kind: str  # service | system
    project_id: int | None
    project_name: str
    mapped_by: str  # vcs_attr | service_names | ""
    repo_url: str
    first_seen_at: datetime
    last_seen_at: datetime


class ServiceEdgeOut(Schema):
    id: int
    client_id: int
    server_id: int
    type: str
    count: int
    error_count: int
    error_rate: float
    duration_avg_ms: float
    duration_max_ms: float
    rate_per_min: float
    first_seen_at: datetime
    last_seen_at: datetime


class ServiceGraphOut(Schema):
    organization_id: int
    source: str
    refreshed_at: datetime | None
    nodes: list[ServiceNodeOut]
    edges: list[ServiceEdgeOut]


class AgentIn(Schema):
    name: str
    kind: AgentKind = AgentKind.PLAYBOOK_SWEEP
    trigger: AgentTrigger = AgentTrigger.ON_MERGE
    schedule_cron: str = ""
    branch_pattern: str = ""
    project_ids: list[int] = []  # [] = every project in the org
    playbook_ids: list[int] = []  # playbook_sweep; [] = every visible playbook
    execution_mode: ExecutionMode | None = None  # default depends on kind
    max_findings_per_repo: int = 3
    monthly_token_budget: int = 0
    enabled: bool = True


class AgentUpdateIn(Schema):
    name: str | None = None
    kind: AgentKind | None = None
    trigger: AgentTrigger | None = None
    schedule_cron: str | None = None
    branch_pattern: str | None = None
    project_ids: list[int] | None = None
    playbook_ids: list[int] | None = None
    execution_mode: ExecutionMode | None = None
    max_findings_per_repo: int | None = None
    monthly_token_budget: int | None = None
    enabled: bool | None = None


class AgentOut(Schema):
    id: int
    organization_id: int
    name: str
    kind: AgentKind
    trigger: AgentTrigger
    schedule_cron: str
    branch_pattern: str
    project_ids: list[int]
    playbook_ids: list[int]
    execution_mode: ExecutionMode
    max_findings_per_repo: int
    monthly_token_budget: int
    tokens_this_month: int
    enabled: bool
    created_by_id: int | None
    created_at: datetime
    updated_at: datetime
    last_scan_run_id: int | None


class ScanRepoOut(Schema):
    project_id: int
    project_name: str
    status: str
    finding_count: int
    incident_run_ids: list[int]
    error: str


class ScanRunOut(Schema):
    id: int
    agent_id: int
    trigger: ScanTrigger
    trigger_ref: str
    status: str
    repos: list[ScanRepoOut]
    finding_count: int
    usage: UsageOut
    error_message: str
    started_at: datetime
    finished_at: datetime | None


class ScanRunListOut(Schema):
    scan_runs: list[ScanRunOut]
    total: int
