from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from django.core.management import call_command

from sre.management.commands import backfill_llm_usage
from sre.models import IncidentRun, LLMUsage

pytestmark = pytest.mark.django_db

T0 = datetime(2026, 9, 25, 12, 0)


def generation(trace_name, model, inp, out, minutes):
    return SimpleNamespace(trace_name=trace_name, name="chat", model=model,
                           usage_details={"input": inp, "output": out},
                           start_time=T0 + timedelta(minutes=minutes))


class FakeLangfuse:
    def __init__(self, sessions):
        self.sessions = sessions
        self.api = SimpleNamespace(observations=self)

    def get_many(self, *, session_id, cursor=None, **kwargs):
        # Two pages for the first session, to exercise the cursor.
        data = self.sessions.get(session_id, [])
        if cursor is None and len(data) > 1:
            return SimpleNamespace(data=data[1:], meta=SimpleNamespace(cursor="next"))
        page = data[:1] if cursor == "next" else data
        return SimpleNamespace(data=page, meta=SimpleNamespace(cursor=None))


def test_backfill_writes_rows_once(monkeypatch, make_user, make_project):
    project = make_project(make_user())
    run = IncidentRun.objects.create(project=project, trace_id="t", temporal_workflow_id="w")
    fake = FakeLangfuse({f"incident-run-{run.id}": [
        generation("sre.bug_classification", "gpt-small", 100, 10, 0),
        generation("sre.playbook_execution", "gpt-big", 1000, 200, 5),
    ]})
    monkeypatch.setattr(backfill_llm_usage, "_langfuse", lambda: fake)

    call_command("backfill_llm_usage")
    rows = list(LLMUsage.objects.filter(incident_run=run).order_by("id"))
    assert [(r.step, r.model, r.input_tokens, r.output_tokens, r.provider) for r in rows] == [
        ("bug_classification", "gpt-small", 100, 10, ""),
        ("playbook_execution", "gpt-big", 1000, 200, ""),
    ]

    call_command("backfill_llm_usage")
    assert LLMUsage.objects.filter(incident_run=run).count() == 2


def test_backfill_dry_run_writes_nothing(monkeypatch, make_user, make_project):
    project = make_project(make_user())
    run = IncidentRun.objects.create(project=project, trace_id="t", temporal_workflow_id="w")
    fake = FakeLangfuse({f"incident-run-{run.id}": [generation("sre.x", "m", 1, 1, 0)]})
    monkeypatch.setattr(backfill_llm_usage, "_langfuse", lambda: fake)
    call_command("backfill_llm_usage", "--dry-run")
    assert not LLMUsage.objects.exists()
