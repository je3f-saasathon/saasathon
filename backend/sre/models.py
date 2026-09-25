import secrets

from django.conf import settings
from django.db import models


def _new_webhook_secret() -> str:
    return secrets.token_urlsafe(32)


class ExecutionMode(models.TextChoices):
    AUTONOMOUS = "autonomous", "Autonomous"
    DRAFT_ONLY = "draft_only", "Draft only"
    ADVISORY_ONLY = "advisory_only", "Advisory only"


class ProjectRole(models.TextChoices):
    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    VIEWER = "viewer", "Viewer"


ROLE_RANK = {ProjectRole.VIEWER: 0, ProjectRole.ADMIN: 1, ProjectRole.OWNER: 2}


class LLMProvider(models.TextChoices):
    ANTHROPIC = "anthropic", "Anthropic"
    OPENAI = "openai", "OpenAI"
    SELF_HOSTED = "self_hosted", "Self-hosted (OpenAI-compatible)"
    JEV_CLOUDFLARE = "jev_cloudflare", "Jev (Cloudflare Workers AI)"


class PipelineStep(models.TextChoices):
    ANOMALY_DOUBLE_CHECK = "anomaly_double_check", "Anomaly double-check"
    BUG_CLASSIFICATION = "bug_classification", "Bug classification"
    PLAYBOOK_SIMILARITY_JUDGE = "playbook_similarity_judge", "Playbook similarity judge"
    PLAYBOOK_CREATION = "playbook_creation", "Playbook creation"
    PLAYBOOK_EXECUTION = "playbook_execution", "Playbook execution"


class Project(models.Model):
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through="ProjectMembership", related_name="sre_projects"
    )
    name = models.CharField(max_length=255)
    github_installation_id = models.CharField(max_length=64)
    github_repo_owner = models.CharField(max_length=255)
    github_repo_name = models.CharField(max_length=255)
    github_default_branch = models.CharField(max_length=100, default="main")
    uptrace_webhook_secret = models.CharField(max_length=128, default=_new_webhook_secret)
    uptrace_source_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    default_llm_config = models.ForeignKey(
        "LLMProviderConfig",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="projects_default_for",
    )
    default_execution_mode = models.CharField(
        max_length=32, choices=ExecutionMode.choices, default=ExecutionMode.DRAFT_ONLY
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sre_project"
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class ProjectMembership(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sre_memberships"
    )
    role = models.CharField(max_length=16, choices=ProjectRole.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sre_project_membership"
        unique_together = [("project", "user")]


class LLMProviderConfig(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="llm_configs"
    )
    name = models.CharField(max_length=100)
    provider = models.CharField(max_length=32, choices=LLMProvider.choices)
    model = models.CharField(max_length=255)
    base_url = models.URLField(blank=True, default="")
    api_key_encrypted = models.BinaryField(blank=True, default=b"")
    extra_config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sre_llm_provider_config"
        ordering = ["-created_at"]

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key_encrypted)


class LLMStepOverride(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="step_overrides")
    step = models.CharField(max_length=64, choices=PipelineStep.choices)
    llm_config = models.ForeignKey(LLMProviderConfig, on_delete=models.CASCADE)

    class Meta:
        db_table = "sre_llm_step_override"
        unique_together = [("project", "step")]


class Playbook(models.Model):
    class Status(models.TextChoices):
        UNCONFIRMED = "unconfirmed", "Unconfirmed"
        CONFIRMED = "confirmed", "Confirmed"
        FAILING = "failing", "Failing"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="playbooks")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    keywords = models.JSONField(default=list)
    # Typed steps: {"type": "edit_file"|"run_command", ...}. No PR step on purpose:
    # the execution mode alone decides whether a PR is opened.
    steps = models.JSONField(default=list)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.UNCONFIRMED)
    execution_mode_override = models.CharField(
        max_length=32, choices=ExecutionMode.choices, null=True, blank=True
    )
    consecutive_failure_count = models.PositiveIntegerField(default=0)
    source_incident_run = models.OneToOneField(
        "IncidentRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_playbook",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sre_playbook"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class IncidentRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        NO_ANOMALY = "no_anomaly", "No anomaly confirmed"
        NEW_PLAYBOOK_CREATED = "new_playbook_created", "New playbook created"
        AWAITING_APPROVAL = "awaiting_approval", "Awaiting human approval"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        ADVISORY_COMPLETE = "advisory_complete", "Advisory complete"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="incident_runs")
    trace_id = models.CharField(max_length=255, db_index=True)
    uptrace_exception_id = models.CharField(max_length=255, blank=True, default="")
    temporal_workflow_id = models.CharField(max_length=255, unique=True)
    raw_webhook_payload = models.JSONField(default=dict)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.RUNNING)
    classification = models.JSONField(null=True, blank=True)
    matched_playbook = models.ForeignKey(
        Playbook, null=True, blank=True, on_delete=models.SET_NULL, related_name="incident_runs"
    )
    diagnosis_report = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sre_incident_run"
        ordering = ["-created_at"]


class PlaybookRun(models.Model):
    class Status(models.TextChoices):
        PENDING_APPROVAL = "pending_approval", "Pending approval"
        REJECTED = "rejected", "Rejected"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    incident_run = models.OneToOneField(
        IncidentRun, on_delete=models.CASCADE, related_name="playbook_run"
    )
    playbook = models.ForeignKey(Playbook, on_delete=models.CASCADE, related_name="runs")
    execution_mode = models.CharField(max_length=32, choices=ExecutionMode.choices)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.RUNNING)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    pr_url = models.URLField(blank=True, default="")
    branch_name = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sre_playbook_run"


class PlaybookExecutionAttempt(models.Model):
    class Outcome(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    playbook_run = models.ForeignKey(PlaybookRun, on_delete=models.CASCADE, related_name="attempts")
    attempt_number = models.PositiveSmallIntegerField()
    generated_steps = models.JSONField(default=list)
    previous_attempt_feedback = models.TextField(blank=True, default="")
    outcome = models.CharField(max_length=32, choices=Outcome.choices, default=Outcome.PENDING)
    summary = models.TextField(blank=True, default="")
    error_output = models.TextField(blank=True, default="")
    branch_name = models.CharField(max_length=255, blank=True, default="")
    langfuse_trace_id = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sre_playbook_execution_attempt"
        unique_together = [("playbook_run", "attempt_number")]
        ordering = ["attempt_number"]


class LLMUsage(models.Model):
    """One row per LLM call made for an incident, so the dashboard can show tokens and
    models without asking Langfuse. `step` is the trace step name (includes
    "diagnosis_report", which isn't a PipelineStep)."""

    incident_run = models.ForeignKey(IncidentRun, on_delete=models.CASCADE, related_name="llm_usage")
    step = models.CharField(max_length=64)
    provider = models.CharField(max_length=32, blank=True, default="")
    model = models.CharField(max_length=255, blank=True, default="")
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sre_llm_usage"
        ordering = ["created_at"]


class GitHubInstallation(models.Model):
    """A GitHub App installation this user proved access to (via their own GitHub token,
    GET /user/installations). Projects may only use installations their editor has here."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="github_installations"
    )
    installation_id = models.CharField(max_length=64)
    account_login = models.CharField(max_length=255)
    account_type = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sre_github_installation"
        unique_together = [("user", "installation_id")]
        ordering = ["account_login"]
