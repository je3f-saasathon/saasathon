from contextlib import contextmanager

from django.conf import settings

_client = None


def _langfuse():
    global _client
    if not (settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY):
        return None
    if _client is None:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST or None,
        )
    return _client


class _Noop:
    trace_id = ""

    def update(self, **kwargs):
        pass


@contextmanager
def trace_step(step: str, *, project_id: int, incident_run_id: int):
    """One Langfuse trace per LLM-calling activity, grouped by incident run as the session."""
    lf = _langfuse()
    if lf is None:
        yield _Noop()
        return
    with lf.start_as_current_span(name=f"sre.{step}") as span:
        lf.update_current_trace(
            name=f"sre.{step}",
            session_id=f"incident-run-{incident_run_id}",
            tags=["sre", step, f"project:{project_id}"],
            metadata={"project_id": project_id, "incident_run_id": incident_run_id, "step": step},
        )
        yield span
    lf.flush()


@contextmanager
def trace_generation(name: str, *, model: str, input):
    lf = _langfuse()
    if lf is None:
        yield _Noop()
        return
    with lf.start_as_current_generation(name=name, model=model, input=input) as generation:
        yield generation
