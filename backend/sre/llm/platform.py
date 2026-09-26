"""The company default model: what a project runs on when it hasn't picked its own config,
paid for with our (the server's) API keys, and capped per project per month.

The configs built here are never saved. They're in-memory `LLMProviderConfig`s marked
`is_platform`, so the clients and usage recording treat them like any other config."""

from django.conf import settings
from django.db.models import Q, Sum
from django.utils import timezone

from ..crypto import encrypt
from ..models import LLMProvider, LLMProviderConfig, LLMUsage, PipelineStep, Project

PLATFORM = "platform"
USER = "user"

# Cheap, classification-style steps. Mirrors resolve.JEV_STEPS (Jev can only run these).
TRIAGE_STEPS = {
    PipelineStep.ANOMALY_DOUBLE_CHECK,
    PipelineStep.BUG_CLASSIFICATION,
    PipelineStep.PLAYBOOK_SIMILARITY_JUDGE,
}


class PlatformCapReached(Exception):
    pass


def jev_available() -> bool:
    return bool(settings.SRE_PLATFORM_TRIAGE_USES_JEV and settings.CLOUDFLARE_ACCOUNT_ID
                and settings.CLOUDFLARE_API_TOKEN)


def available() -> bool:
    """Every step needs a chat model for playbooks, so the OpenAI key is what enables it."""
    return bool(settings.SRE_PLATFORM_OPENAI_API_KEY)


def describe() -> dict:
    return {
        "available": available(),
        "triage_model": ("jev" if jev_available() else settings.SRE_PLATFORM_FAST_MODEL)
        if available() else "",
        "strong_model": settings.SRE_PLATFORM_STRONG_MODEL if available() else "",
        "monthly_token_cap": settings.SRE_PLATFORM_MONTHLY_TOKEN_CAP,
    }


def config_for(step: str) -> LLMProviderConfig | None:
    if not available():
        return None
    if step in TRIAGE_STEPS and jev_available():
        # No key on the config: JevClient falls back to the server's CLOUDFLARE_* credentials.
        config = LLMProviderConfig(name="Company default (Jev)", provider=LLMProvider.JEV_CLOUDFLARE,
                                   model=settings.CLOUDFLARE_JEV_MODEL)
    else:
        model = (settings.SRE_PLATFORM_FAST_MODEL if step in TRIAGE_STEPS
                 else settings.SRE_PLATFORM_STRONG_MODEL)
        config = LLMProviderConfig(name="Company default", provider=LLMProvider.OPENAI, model=model,
                                   api_key_encrypted=encrypt(settings.SRE_PLATFORM_OPENAI_API_KEY))
    config.is_platform = True
    return config


def billed_to(config: LLMProviderConfig) -> str:
    """A user's Jev config without its own Cloudflare token also runs on (and bills) our
    account, so it counts as ours too."""
    if getattr(config, "is_platform", False):
        return PLATFORM
    if config.provider == LLMProvider.JEV_CLOUDFLARE and not config.api_key_encrypted:
        return PLATFORM
    return USER


def tokens_this_month(project: Project) -> int:
    start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    totals = LLMUsage.objects.filter(
        Q(incident_run__project=project) | Q(scan_repo__project=project),
        billed_to=PLATFORM, created_at__gte=start,
    ).aggregate(input=Sum("input_tokens"), output=Sum("output_tokens"))
    return (totals["input"] or 0) + (totals["output"] or 0)


def check_cap(project: Project) -> None:
    """Checked when a step picks its model, so one long agent run can overshoot a little."""
    cap = settings.SRE_PLATFORM_MONTHLY_TOKEN_CAP
    if cap and tokens_this_month(project) >= cap:
        raise PlatformCapReached(
            f"Project {project.id} has used its {cap:,} tokens this month on the company model. "
            "Add your own model config in Settings → Models to keep going."
        )
