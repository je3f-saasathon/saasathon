"""Turning old, specific playbooks into generic playbook + runbook pairs, reversibly
(manage.py generalize_playbooks). Every change is recorded in the playbook's
legacy_snapshot so --revert can put things back exactly."""

import json

from django.db import transaction
from django.db.models import Q

from ..activities import llm_step
from ..llm.clients import client_for
from ..llm.resolve import get_llm_config
from ..models import IncidentRun, PipelineStep, Playbook, PlaybookRun, Runbook
from .context import UNTRUSTED_NOTICE, untrusted
from .knowledge import GenericPlaybookAuthor, playbook_card
from .playbooks import MATCH_CONFIDENCE_THRESHOLD, clean_generic_steps, clean_steps

SNAPSHOT_FIELDS = ["title", "description", "keywords", "steps", "status", "category", "symptoms",
                   "consecutive_failure_count"]


def legacy_playbooks(project_id: int | None = None):
    qs = Playbook.objects.filter(is_generic=False).exclude(origin=Playbook.Origin.BUILTIN)
    if project_id is not None:
        qs = qs.filter(project_id=project_id)
    return qs.select_related("project", "source_incident_run").order_by("id")


def skip_reason(playbook: Playbook) -> str:
    if playbook.project is None:
        return "no project"
    if playbook.source_incident_run is None:
        return "written by a person (no source incident to bill the rewrite to)"
    return ""


class Rewriter:
    """The same generic-playbook prompt the pipeline uses, given the old playbook too."""

    def __init__(self, run: IncidentRun):
        self.run = run
        self.client = client_for(get_llm_config(run.project, PipelineStep.PLAYBOOK_CREATION))

    def rewrite(self, playbook: Playbook) -> dict:
        prompt = (
            "Rewrite this playbook, which was written for one specific bug, as a generic "
            "playbook for its class of bug.\n\n"
            + untrusted("specific_playbook", {"title": playbook.title,
                                              "description": playbook.description,
                                              "steps": playbook.steps})
            + "\n\nThe incident it came from:\n"
            + untrusted("classification", self.run.classification or {})
        )
        return self.client.complete_json(GenericPlaybookAuthor.SYSTEM, prompt,
                                         name="playbook_creation")


def generalize(playbook: Playbook) -> Runbook:
    """Copy the specific playbook into a runbook, then rewrite it in place as a generic
    one (same id, so past runs and incidents still point at it). One transaction."""
    run = playbook.source_incident_run
    with llm_step("playbook_creation", run):
        data = Rewriter(run).rewrite(playbook)
    steps = clean_generic_steps(data.get("steps"))
    if not steps:
        raise ValueError("the rewrite had no generic steps")
    project = playbook.project
    with transaction.atomic():
        snapshot = {f: getattr(playbook, f) for f in SNAPSHOT_FIELDS}
        runbook = Runbook.objects.create(
            project=project, playbook=playbook, title=playbook.title,
            description=playbook.description, keywords=playbook.keywords,
            steps=clean_steps(playbook.steps), status=playbook.status,
            consecutive_failure_count=playbook.consecutive_failure_count,
            origin=Runbook.Origin.MIGRATED,
            repo_owner=project.github_repo_owner, repo_name=project.github_repo_name,
            service_name=str((run.telemetry or {}).get("service_name") or "")[:255],
        )
        snapshot["run_ids"] = list(PlaybookRun.objects.filter(playbook=playbook, runbook__isnull=True)
                                   .values_list("id", flat=True))
        snapshot["incident_ids"] = list(IncidentRun.objects.filter(
            matched_playbook=playbook, matched_runbook__isnull=True).values_list("id", flat=True))
        PlaybookRun.objects.filter(id__in=snapshot["run_ids"]).update(runbook=runbook)
        IncidentRun.objects.filter(id__in=snapshot["incident_ids"]).update(matched_runbook=runbook)

        keywords = [str(k).lower().strip() for k in data.get("keywords") or [] if str(k).strip()]
        playbook.legacy_snapshot = snapshot
        playbook.title = str(data.get("title") or playbook.title)[:255]
        playbook.description = str(data.get("description", ""))[:5000]
        playbook.symptoms = str(data.get("symptoms", ""))[:5000]
        playbook.keywords = keywords[:20]
        playbook.steps = steps
        playbook.category = playbook.category or str((run.classification or {}).get("category") or "")
        playbook.is_generic = True
        # New LLM text: it must earn confirmation again. The runbook keeps the old status.
        playbook.status = Playbook.Status.UNCONFIRMED
        playbook.consecutive_failure_count = 0
        playbook.save()
    return runbook


def revert(playbook: Playbook) -> None:
    """Undo generalize, after undoing any merge into or out of this playbook."""
    for moved in list(playbook.legacy_snapshot.get("merged_from", [])):
        source = Playbook.objects.filter(id=moved["playbook_id"]).first()
        if source is not None:
            unmerge(source)
    playbook.refresh_from_db()
    unmerge(playbook)
    playbook.refresh_from_db()
    snapshot = playbook.legacy_snapshot
    with transaction.atomic():
        runbooks = Runbook.objects.filter(playbook=playbook, origin=Runbook.Origin.MIGRATED)
        PlaybookRun.objects.filter(id__in=snapshot.get("run_ids", [])).update(runbook=None)
        IncidentRun.objects.filter(id__in=snapshot.get("incident_ids", [])).update(matched_runbook=None)
        runbooks.delete()
        for field in SNAPSHOT_FIELDS:
            if field in snapshot:
                setattr(playbook, field, snapshot[field])
        playbook.is_generic = False
        playbook.legacy_snapshot = {}
        playbook.save()


class DuplicateJudge:
    SYSTEM = (
        "You are an SRE deduplicating generic remediation playbooks. Say whether the new "
        "playbook covers the same class of bug as one of the existing ones, so that one of "
        "them is enough. " + UNTRUSTED_NOTICE +
        ' Reply with JSON only: {"duplicate_of": <id or null>, "confidence": <0.0-1.0>, '
        '"reasoning": "<one sentence>"}'
    )

    def __init__(self, run: IncidentRun):
        self.run = run
        self.client = client_for(get_llm_config(run.project, PipelineStep.PLAYBOOK_SIMILARITY_JUDGE))

    def duplicate_of(self, playbook: Playbook, others: list[Playbook]) -> int | None:
        prompt = ("New playbook:\n" + untrusted("new_playbook", playbook_card(playbook))
                  + "\n\nExisting playbooks:\n"
                  + untrusted("existing_playbooks", [playbook_card(p) for p in others]))
        with llm_step("playbook_similarity_judge", self.run):
            data = self.client.complete_json(self.SYSTEM, prompt, name="playbook_dedupe")
        target = data.get("duplicate_of")
        try:
            confidence = float(data.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        ids = {p.id for p in others}
        return target if target in ids and confidence >= MATCH_CONFIDENCE_THRESHOLD else None


def merge(playbook: Playbook) -> Playbook | None:
    """If a built-in or another generic playbook in the org already covers this one, move
    its runbooks there and archive this one (status failing; nothing is deleted)."""
    others = list(
        Playbook.objects.filter(is_generic=True, category=playbook.category)
        .filter(Q(organization__isnull=True, origin=Playbook.Origin.BUILTIN)
                | Q(organization_id=playbook.organization_id))
        .exclude(id=playbook.id).exclude(legacy_snapshot__has_key="merged_into")
    ) if playbook.category else []
    if not others:
        return None
    target_id = DuplicateJudge(playbook.source_incident_run).duplicate_of(playbook, others)
    if target_id is None:
        return None
    target = Playbook.objects.get(id=target_id)
    with transaction.atomic():
        runbook_ids = list(playbook.runbooks.values_list("id", flat=True))
        Runbook.objects.filter(id__in=runbook_ids).update(playbook=target)
        if target.origin != Playbook.Origin.BUILTIN:  # built-ins stay untouched
            target.legacy_snapshot = {**target.legacy_snapshot, "merged_from": [
                *target.legacy_snapshot.get("merged_from", []),
                {"playbook_id": playbook.id, "runbook_ids": runbook_ids}]}
            target.save(update_fields=["legacy_snapshot", "updated_at"])
        playbook.legacy_snapshot = {**playbook.legacy_snapshot, "merged_into": target.id,
                                    "merged_runbook_ids": runbook_ids,
                                    "status_before_merge": playbook.status}
        playbook.status = Playbook.Status.FAILING  # archived: excluded from matching
        playbook.save(update_fields=["legacy_snapshot", "status", "updated_at"])
    return target


def unmerge(playbook: Playbook) -> None:
    """Undo merge() for a playbook that was merged into another (built-ins included)."""
    snapshot = playbook.legacy_snapshot
    if "merged_into" not in snapshot:
        return
    with transaction.atomic():
        Runbook.objects.filter(id__in=snapshot.get("merged_runbook_ids", [])).update(playbook=playbook)
        target = Playbook.objects.filter(id=snapshot["merged_into"]).first()
        if target is not None and target.legacy_snapshot.get("merged_from"):
            target.legacy_snapshot = {**target.legacy_snapshot, "merged_from": [
                m for m in target.legacy_snapshot["merged_from"] if m["playbook_id"] != playbook.id]}
            target.save(update_fields=["legacy_snapshot", "updated_at"])
        playbook.status = snapshot.get("status_before_merge", playbook.status)
        playbook.legacy_snapshot = {k: v for k, v in snapshot.items()
                                    if k not in ("merged_into", "merged_runbook_ids",
                                                 "status_before_merge")}
        playbook.save(update_fields=["legacy_snapshot", "status", "updated_at"])


def describe(playbook: Playbook) -> str:
    return json.dumps({"id": playbook.id, "project": playbook.project.name if playbook.project else None,
                       "title": playbook.title, "status": playbook.status,
                       "steps": len(playbook.steps)})
