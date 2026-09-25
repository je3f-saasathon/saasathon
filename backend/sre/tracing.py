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


class _Step:
    def __init__(self, trace_id: str):
        self.trace_id = trace_id


@contextmanager
def trace_step(step: str, *, project_id: int, incident_run_id: int):
    """One Langfuse trace per LLM-calling activity, grouped by incident run as the session."""
    lf = _langfuse()
    if lf is None:
        yield _Noop()
        return
    from langfuse import propagate_attributes

    with propagate_attributes(
        trace_name=f"sre.{step}",
        session_id=f"incident-run-{incident_run_id}",
        tags=["sre", step, f"project:{project_id}"],
        metadata={"project_id": str(project_id), "incident_run_id": str(incident_run_id)},
    ):
        with lf.start_as_current_observation(name=f"sre.{step}", as_type="span"):
            yield _Step(lf.get_current_trace_id() or "")
    lf.flush()


@contextmanager
def trace_generation(name: str, *, model: str, input):
    lf = _langfuse()
    if lf is None:
        yield _Noop()
        return
    with lf.start_as_current_observation(
        name=name, as_type="generation", model=model, input=input
    ) as generation:
        yield generation
