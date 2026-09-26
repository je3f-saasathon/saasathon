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


# Company-model mixes a project can choose. "triage" runs the classification-style steps
# ("jev", or a chat model); "strong" writes and runs playbooks. None = the env-configured
# fast/strong model (SRE_PLATFORM_FAST_MODEL / SRE_PLATFORM_STRONG_MODEL).
PRESETS = {
    "openai_jev": {"label": "OpenAI + Jev", "triage": "jev", "strong": None},
    "openai": {"label": "OpenAI", "triage": None, "strong": None},
    "gpt_5_4": {"label": "GPT-5.4 only", "triage": "gpt-5.4", "strong": "gpt-5.4"},
    "gpt_5_5": {"label": "GPT-5.5 only", "triage": "gpt-5.5", "strong": "gpt-5.5"},
}
DEFAULT_PRESET = "openai_jev"


class PlatformCapReached(Exception):
    pass


def jev_available() -> bool:
    return bool(settings.SRE_PLATFORM_TRIAGE_USES_JEV and settings.CLOUDFLARE_ACCOUNT_ID
                and settings.CLOUDFLARE_API_TOKEN)


def available() -> bool:
    """Every step needs a chat model for playbooks, so the OpenAI key is what enables it."""
    return bool(settings.SRE_PLATFORM_OPENAI_API_KEY)


def preset_models(preset: str) -> tuple[str, str]:
    """(triage, strong) for a preset. Triage is "jev" only when Jev is really configured;
    otherwise the preset falls back to the fast chat model for those steps."""
    spec = PRESETS.get(preset) or PRESETS[DEFAULT_PRESET]
    strong = spec["strong"] or settings.SRE_PLATFORM_STRONG_MODEL
    triage = spec["triage"] or settings.SRE_PLATFORM_FAST_MODEL
    if triage == "jev" and not jev_available():
        triage = settings.SRE_PLATFORM_FAST_MODEL
    return triage, strong


def describe() -> dict:
    ok = available()
    presets = []
    for key, spec in PRESETS.items():
        triage, strong = preset_models(key)
        presets.append({"key": key, "label": spec["label"], "triage_model": triage,
                        "strong_model": strong})
    triage, strong = preset_models(DEFAULT_PRESET)
    return {
        "available": ok,
        "triage_model": triage if ok else "",
        "strong_model": strong if ok else "",
        "monthly_token_cap": settings.SRE_PLATFORM_MONTHLY_TOKEN_CAP,
        "default_preset": DEFAULT_PRESET,
        "presets": presets if ok else [],
    }


def config_for(step: str, preset: str = DEFAULT_PRESET) -> LLMProviderConfig | None:
    if not available():
        return None
    triage, strong = preset_models(preset)
    model = triage if step in TRIAGE_STEPS else strong
    if model == "jev":
        # No key on the config: JevClient falls back to the server's CLOUDFLARE_* credentials.
        config = LLMProviderConfig(name="Company default (Jev)", provider=LLMProvider.JEV_CLOUDFLARE,
                                   model=settings.CLOUDFLARE_JEV_MODEL)
    else:
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


def usage_this_month(project: Project):
    """The project's LLM calls this month billed to our keys."""
    start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return LLMUsage.objects.filter(
        Q(incident_run__project=project) | Q(scan_repo__project=project),
        billed_to=PLATFORM, created_at__gte=start,
    )


def tokens_this_month(project: Project) -> int:
    totals = usage_this_month(project).aggregate(input=Sum("input_tokens"), output=Sum("output_tokens"))
    return (totals["input"] or 0) + (totals["output"] or 0)


def check_cap(project: Project) -> None:
    """Checked when a step picks its model, so one long agent run can overshoot a little."""
    cap = settings.SRE_PLATFORM_MONTHLY_TOKEN_CAP
    if cap and tokens_this_month(project) >= cap:
        raise PlatformCapReached(
            f"Project {project.id} has used its {cap:,} tokens this month on the company model. "
            "Add your own model config in Settings → Models to keep going."
        )
