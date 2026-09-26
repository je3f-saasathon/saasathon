import json

from django.conf import settings
from django.db.models import Q

from ..llm.clients import JevClient, client_for
from ..llm.resolve import get_llm_config
from ..models import IncidentRun, PipelineStep, Playbook, Project
from ..temporal_types import Classification, JudgeResult
from .context import UNTRUSTED_NOTICE, incident_context, untrusted

MATCH_CONFIDENCE_THRESHOLD = 0.6
ALLOWED_STEP_TYPES = {"edit_file", "run_command"}
GENERIC_STEP_TYPES = {"investigate", "change", "verify"}
MAX_PLAYBOOK_STEPS = 20


def clean_steps(raw_steps) -> list[dict]:
    """Keep only known step types with the fields they need; drop anything else
    (notably any attempt to smuggle in a PR/push step)."""
    steps = []
    for step in raw_steps or []:
        if not isinstance(step, dict) or step.get("type") not in ALLOWED_STEP_TYPES:
            continue
        if step["type"] == "edit_file" and step.get("path"):
            steps.append({
                "type": "edit_file",
                "path": str(step["path"])[:500],
                "instructions": str(step.get("instructions", ""))[:4000],
            })
        elif step["type"] == "run_command" and step.get("command"):
            steps.append({"type": "run_command", "command": str(step["command"])[:1000]})
    return steps[:MAX_PLAYBOOK_STEPS]


def clean_generic_steps(raw_steps) -> list[dict]:
    """Generic playbook steps: what to do, never which file (that's a runbook's job)."""
    steps = []
    for step in raw_steps or []:
        if (isinstance(step, dict) and step.get("type") in GENERIC_STEP_TYPES
                and str(step.get("instructions", "")).strip()):
            steps.append({"type": step["type"], "instructions": str(step["instructions"])[:4000]})
    return steps[:MAX_PLAYBOOK_STEPS]


def clean_playbook_steps(raw_steps) -> list[dict]:
    """A playbook may hold generic steps and, for legacy or hand-written ones, the
    specific step types too. Anything else (e.g. a push/PR step) is dropped."""
    steps = []
    for step in raw_steps or []:
        kind = step.get("type") if isinstance(step, dict) else None
        cleaned = (clean_generic_steps([step]) if kind in GENERIC_STEP_TYPES
                   else clean_steps([step]))
        steps.extend(cleaned)
    return steps[:MAX_PLAYBOOK_STEPS]


def visible_playbooks(project: Project):
    """What a project can match and list. Runbooks off: its own playbooks, as always.
    On: the built-ins, its org's generic playbooks, and its own legacy ones."""
    if not settings.SRE_RUNBOOKS_ENABLED:
        return Playbook.objects.filter(project=project)
    return Playbook.objects.filter(
        Q(organization__isnull=True, origin=Playbook.Origin.BUILTIN)
        | Q(organization_id=project.organization_id, is_generic=True,
            organization__isnull=False)
        | Q(project=project, is_generic=False)
    )


def _playbook_card(playbook: Playbook) -> dict:
    return {
        "id": playbook.id,
        "title": playbook.title,
        "description": playbook.description,
        "keywords": playbook.keywords,
        "steps": playbook.steps,
        "status": playbook.status,
    }


class PlaybookSearch:
    """Keyword-overlap ranking in Python so it works on SQLite (native dev) and Postgres."""

    def __init__(self, project: Project):
        self.project = project

    def top_k(self, keywords: list[str], k: int = 5) -> list[int]:
        wanted = {kw.lower() for kw in keywords}
        if not wanted:
            return []
        scored = []
        for playbook in self.project.playbooks.exclude(status=Playbook.Status.FAILING):
            own = {kw.lower() for kw in playbook.keywords}
            haystack = f"{playbook.title} {playbook.description}".lower()
            score = 2 * len(wanted & own) + sum(1 for kw in wanted if kw in haystack)
            if score > 0:
                scored.append((score, playbook.id))
        scored.sort(key=lambda s: (-s[0], -s[1]))
        return [playbook_id for _, playbook_id in scored[:k]]


class PlaybookJudge:
    SYSTEM = (
        "You are an SRE deciding whether an existing remediation playbook fits a new "
        "incident. Only pick a playbook if following its steps would plausibly fix THIS "
        "bug; a merely related topic is not a match. " + UNTRUSTED_NOTICE +
        ' Reply with JSON only: {"matched_playbook_id": <id or null>, '
        '"confidence": <0.0-1.0>, "reasoning": "<one paragraph>"}'
    )

    def __init__(self, run: IncidentRun):
        self.run = run
        self.client = client_for(
            get_llm_config(run.project, PipelineStep.PLAYBOOK_SIMILARITY_JUDGE)
        )

    def judge(self, candidate_ids: list[int]) -> JudgeResult:
        candidates = list(Playbook.objects.filter(project=self.run.project, id__in=candidate_ids))
        if not candidates:
            return JudgeResult(None, 0.0, "no candidates")
        by_id = {p.id: p for p in candidates}
        context = incident_context(self.run) + "\n\nClassification:\n" + json.dumps(
            self.run.classification
        )

        if isinstance(self.client, JevClient):
            options = {f"pb_{p.id}": f"{p.title}: {p.description}"[:300] for p in candidates}
            options["none"] = "None of these playbooks would fix this bug"
            answer = self.client.choose(
                context, "Which playbook would fix this bug?", options,
                name="playbook_similarity_judge",
            )
            choice, confidence = answer.choice, answer.confidence
            reasoning = f"Jev answered '{choice}' (p={confidence:.2f})"
            playbook_id = int(choice[3:]) if choice.startswith("pb_") and choice[3:].isdigit() else None
            if playbook_id not in by_id:
                return JudgeResult(None, 0.0, reasoning)
            if confidence < MATCH_CONFIDENCE_THRESHOLD:
                return JudgeResult(None, confidence, f"below threshold: {reasoning}")
            return JudgeResult(playbook_id, confidence, reasoning)

        # Unconfirmed playbooks were LLM-written from telemetry, so they're untrusted too.
        prompt = context + "\n\nCandidate playbooks:\n" + untrusted(
            "candidate_playbooks", [_playbook_card(p) for p in candidates]
        )
        data = self.client.complete_json(self.SYSTEM, prompt, name="playbook_similarity_judge")
        playbook_id = data.get("matched_playbook_id")
        try:
            confidence = float(data.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        reasoning = str(data.get("reasoning", ""))
        if not isinstance(playbook_id, int) or playbook_id not in by_id:
            return JudgeResult(None, confidence, reasoning)
        if confidence < MATCH_CONFIDENCE_THRESHOLD:
            return JudgeResult(None, confidence, f"below threshold: {reasoning}")
        return JudgeResult(playbook_id, confidence, reasoning)


class PlaybookAuthor:
    SYSTEM = (
        "You are an SRE writing a reusable remediation playbook for a class of bug, based "
        "on one incident. Steps must be generic enough to apply to future occurrences. "
        "Allowed step types: edit_file (path + instructions for what to change) and "
        "run_command (e.g. running the test suite). Do not include steps that commit, push, "
        "deploy or open pull requests. " + UNTRUSTED_NOTICE +
        ' Reply with JSON only: {"title": "...", "description": "...", '
        '"keywords": ["lowercase search terms"], "steps": [{"type": "edit_file", '
        '"path": "...", "instructions": "..."}, {"type": "run_command", "command": "..."}]}'
    )

    def __init__(self, run: IncidentRun):
        self.run = run
        self.client = client_for(get_llm_config(run.project, PipelineStep.PLAYBOOK_CREATION))

    def create(self, classification: Classification) -> Playbook:
        existing = Playbook.objects.filter(source_incident_run=self.run).first()
        if existing is not None:  # activity retry: don't create a second one
            return existing
        prompt = incident_context(self.run) + "\n\n" + untrusted(
            "classification", classification.__dict__
        )
        data = self.client.complete_json(self.SYSTEM, prompt, name="playbook_creation")
        keywords = [str(k).lower().strip() for k in data.get("keywords") or [] if str(k).strip()]
        return Playbook.objects.create(
            project=self.run.project,
            organization_id=self.run.project.organization_id,
            source_incident_run=self.run,
            title=str(data.get("title") or classification.summary or "Untitled playbook")[:255],
            description=str(data.get("description", ""))[:5000],
            keywords=(keywords or classification.keywords)[:20],
            steps=clean_steps(data.get("steps")),
            status=Playbook.Status.UNCONFIRMED,
        )


class DiagnosisReporter:
    """ADVISORY_ONLY output: a written diagnosis, no repo access at all."""

    SYSTEM = (
        "You are an SRE writing an incident diagnosis for the on-call engineer. Use "
        "Markdown with sections: Summary, Likely cause, Suggested fix (following the "
        "playbook), Verification. " + UNTRUSTED_NOTICE
    )

    def __init__(self, run: IncidentRun, playbook: Playbook):
        self.run = run
        self.playbook = playbook
        self.client = client_for(get_llm_config(run.project, PipelineStep.PLAYBOOK_EXECUTION))

    def write(self) -> str:
        prompt = (
            incident_context(self.run)
            + "\n\nClassification:\n" + json.dumps(self.run.classification)
            + "\n\nMatched playbook:\n" + json.dumps(_playbook_card(self.playbook), indent=2)
        )
        return self.client.chat(
            self.SYSTEM, [{"role": "user", "content": prompt}], name="diagnosis_report"
        )
