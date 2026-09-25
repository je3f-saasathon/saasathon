"""Per-call LLM usage, recorded against the incident run whose activity made the call.

Activities open a scope; clients record into it. Outside a scope (e.g. ad-hoc calls)
nothing is written. Each activity runs in its own worker thread, so scopes can't leak."""

from contextlib import contextmanager
from contextvars import ContextVar

_scope: ContextVar[tuple[int, str] | None] = ContextVar("sre_llm_usage_scope", default=None)


@contextmanager
def usage_scope(incident_run_id: int, step: str):
    token = _scope.set((incident_run_id, step))
    try:
        yield
    finally:
        _scope.reset(token)


def record_usage(config, usage: dict) -> None:
    scope = _scope.get()
    if scope is None:
        return
    from ..models import LLMUsage
    from .platform import billed_to

    incident_run_id, step = scope
    LLMUsage.objects.create(
        incident_run_id=incident_run_id,
        step=step,
        provider=config.provider,
        model=config.model,
        billed_to=billed_to(config),
        input_tokens=int(usage.get("input") or 0),
        output_tokens=int(usage.get("output") or 0),
    )
