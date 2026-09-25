from ..models import LLMProvider, LLMProviderConfig, LLMStepOverride, PipelineStep, Project
from . import platform

# Jev only answers choice questions, so it can't author or execute playbooks.
JEV_STEPS = {
    PipelineStep.ANOMALY_DOUBLE_CHECK,
    PipelineStep.BUG_CLASSIFICATION,
    PipelineStep.PLAYBOOK_SIMILARITY_JUDGE,
}


class NoLLMConfigError(Exception):
    pass


def provider_supports_step(provider: str, step: str) -> bool:
    return provider != LLMProvider.JEV_CLOUDFLARE or step in JEV_STEPS


def get_llm_config(project: Project, step: PipelineStep) -> LLMProviderConfig:
    override = (
        LLMStepOverride.objects.select_related("llm_config")
        .filter(project=project, step=step)
        .first()
    )
    if override is not None:
        config = override.llm_config
    elif project.default_llm_config_id is not None:
        config = project.default_llm_config
    else:
        # Nothing picked: fall back to the company default, on our keys.
        config = platform.config_for(step)
        if config is None:
            raise NoLLMConfigError(
                f"Project {project.id} has no LLM config for step '{step}' and no default config"
            )
    if not provider_supports_step(config.provider, step):
        raise NoLLMConfigError(
            f"LLM config '{config.name}' uses Jev, which can't run step '{step}'; "
            "set a step override with a chat model"
        )
    if platform.billed_to(config) == platform.PLATFORM:
        try:
            platform.check_cap(project)
        except platform.PlatformCapReached as exc:
            raise NoLLMConfigError(str(exc)) from exc
    return config
