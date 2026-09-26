"""Per-call LLM usage, recorded against the incident run whose activity made the call (or,
for a remediation agent's scan, the scanned repo: scan_usage_scope).

Activities open a scope; clients record into it. Outside a scope (e.g. ad-hoc calls)
nothing is written. Each activity runs in its own worker thread, so scopes can't leak."""

from contextlib import contextmanager
from contextvars import ContextVar

# {"incident_run_id" | "scan_repo_id": id, "step": step[, "turn": n]}
_scope: ContextVar[dict | None] = ContextVar("sre_llm_usage_scope", default=None)


@contextmanager
def _scoped(value: dict):
    token = _scope.set(value)
    try:
        yield
    finally:
        _scope.reset(token)


def usage_scope(incident_run_id: int, step: str):
    return _scoped({"incident_run_id": incident_run_id, "step": step})


def scan_usage_scope(scan_repo_id: int, step: str):
    return _scoped({"scan_repo_id": scan_repo_id, "step": step})


@contextmanager
def usage_turn(turn: int):
    """Tags the calls made inside it with the agent loop's turn (no-op outside a scope)."""
    scope = _scope.get()
    if scope is None:
        yield
        return
    with _scoped({**scope, "turn": turn}):
        yield


def record_usage(config, usage: dict) -> None:
    scope = _scope.get()
    if scope is None:
        return
    from ..models import LLMUsage
    from .platform import billed_to

    LLMUsage.objects.create(
        **scope,
        provider=config.provider,
        model=config.model,
        billed_to=billed_to(config),
        input_tokens=int(usage.get("input") or 0),
        cached_input_tokens=int(usage.get("cached_input") or 0),
        output_tokens=int(usage.get("output") or 0),
    )


def tokens_by_model(rows) -> list[dict]:
    """Sums an LLMUsage queryset per (provider, model), biggest first. Tokens from different
    models are priced differently, so totals are also reported split by model."""
    from django.db.models import Count, Sum

    totals = rows.values("provider", "model").annotate(
        calls=Count("id"), input_tokens=Sum("input_tokens"),
        cached_input_tokens=Sum("cached_input_tokens"), output_tokens=Sum("output_tokens"),
    ).order_by()
    out = [{**t, "total_tokens": t["input_tokens"] + t["output_tokens"]} for t in totals]
    return sorted(out, key=lambda t: (-t["total_tokens"], t["provider"], t["model"]))
