"""Fill LLMUsage for incident runs from before usage was recorded in the DB, using the
Langfuse traces (session `incident-run-{id}`). Runs that already have usage are skipped,
so it's safe to re-run. The provider is left blank: the project's config today may not
be the one that ran back then."""

from django.core.management.base import BaseCommand
from django.db import transaction

from sre.models import IncidentRun, LLMUsage
from sre.tracing import _langfuse

FIELDS = "core,basic,model,usage,trace_context"


def generations_for(lf, incident_run_id: int) -> list:
    observations, cursor = [], None
    while True:
        page = lf.api.observations.get_many(
            session_id=f"incident-run-{incident_run_id}", type="GENERATION",
            fields=FIELDS, limit=100, cursor=cursor,
        )
        observations.extend(page.data)
        cursor = page.meta.cursor
        if not cursor or not page.data:
            return sorted(observations, key=lambda o: o.start_time)


def usage_rows(run: IncidentRun, observations) -> list[LLMUsage]:
    rows = []
    for obs in observations:
        trace_name = obs.trace_name or ""
        step = trace_name.removeprefix("sre.") if trace_name.startswith("sre.") else (obs.name or "")
        usage = obs.usage_details or {}
        rows.append(LLMUsage(
            incident_run=run, step=step, model=obs.model or "",
            input_tokens=int(usage.get("input") or 0), output_tokens=int(usage.get("output") or 0),
        ))
    return rows


class Command(BaseCommand):
    help = "Backfill per-incident LLM usage (tokens, models) from Langfuse."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, dry_run=False, **options):
        lf = _langfuse()
        if lf is None:
            self.stderr.write(self.style.WARNING("LANGFUSE_PUBLIC_KEY / SECRET_KEY not set; nothing to do"))
            return
        runs = IncidentRun.objects.filter(llm_usage__isnull=True).order_by("id")
        filled = 0
        for run in runs:
            rows = usage_rows(run, generations_for(lf, run.id))
            if not rows:
                continue
            tokens = sum(r.input_tokens + r.output_tokens for r in rows)
            self.stdout.write(f"incident run {run.id}: {len(rows)} calls, {tokens} tokens")
            if not dry_run:
                with transaction.atomic():
                    LLMUsage.objects.bulk_create(rows)
            filled += 1
        verb = "would fill" if dry_run else "filled"
        self.stdout.write(self.style.SUCCESS(f"{verb} {filled} incident run(s)"))
