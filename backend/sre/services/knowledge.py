"""Matching with generic playbooks and specific runbooks (SRE_RUNBOOKS_ENABLED).

An incident first looks for a runbook of its project that fixes this bug here, then a
playbook for its class of bug. One LLM call judges both lists. When nothing fits, a new
*generic* playbook is written for the org; the runbook comes later, from a successful fix.
"""

import json

from ..llm.clients import JevClient, client_for
from ..llm.resolve import get_llm_config
from ..models import IncidentRun, PipelineStep, Playbook, Project, Runbook
from ..temporal_types import Candidates, Classification, JudgeResult
from .context import UNTRUSTED_NOTICE, incident_context, untrusted
from .playbooks import MATCH_CONFIDENCE_THRESHOLD, clean_generic_steps, visible_playbooks

TOP_K = 3


def _overlap_score(wanted: set[str], keywords, text: str) -> int:
    own = {str(k).lower() for k in keywords or []}
    return 2 * len(wanted & own) + sum(1 for kw in wanted if kw in text)


class KnowledgeSearch:
    """Keyword overlap in Python (works on SQLite and Postgres), no tokens spent."""

    def __init__(self, project: Project):
        self.project = project

    def find(self, keywords: list[str], category: str = "", service_name: str = "") -> Candidates:
        wanted = {kw.lower() for kw in keywords if kw}
        return Candidates(playbook_ids=self._playbooks(wanted, category),
                          runbook_ids=self._runbooks(wanted, service_name))

    def _runbooks(self, wanted: set[str], service_name: str) -> list[int]:
        scored = []
        for runbook in self.project.runbooks.exclude(status=Playbook.Status.FAILING):
            text = f"{runbook.title} {runbook.description} {runbook.area}".lower()
            score = _overlap_score(wanted, runbook.keywords, text)
            if score and service_name and runbook.service_name == service_name:
                score += 2
            if score > 0:
                scored.append((score, runbook.id))
        return [i for _, i in sorted(scored, key=lambda s: (-s[0], -s[1]))[:TOP_K]]

    def _playbooks(self, wanted: set[str], category: str) -> list[int]:
        scored = []
        for playbook in visible_playbooks(self.project).exclude(status=Playbook.Status.FAILING):
            text = f"{playbook.title} {playbook.description} {playbook.symptoms}".lower()
            score = _overlap_score(wanted, playbook.keywords, text)
            if category and playbook.category == category:
                score += 3
            if score > 0:
                scored.append((score, playbook.id))
        return [i for _, i in sorted(scored, key=lambda s: (-s[0], -s[1]))[:TOP_K]]


def playbook_card(playbook: Playbook) -> dict:
    return {"id": playbook.id, "title": playbook.title, "category": playbook.category,
            "description": playbook.description[:600], "symptoms": playbook.symptoms[:600]}


def runbook_card(runbook: Runbook) -> dict:
    # The files it touches (not its instructions): enough to tell "this bug, here" from
    # "a similar bug somewhere else".
    files = [s["path"] for s in runbook.steps if s.get("type") == "edit_file" and s.get("path")]
    return {"id": runbook.id, "title": runbook.title, "area": runbook.area,
            "service": runbook.service_name, "description": runbook.description[:600],
            "files": files[:10]}


class KnowledgeJudge:
    SYSTEM = (
        "You are an SRE matching a new incident to existing remediation knowledge. "
        "Runbooks are specific: they fixed a bug in THIS repository, and list the files they "
        "touched. Playbooks are generic: how to handle a class of bug. Pick a runbook only if "
        "following it would plausibly fix this very bug; otherwise pick the playbook whose "
        "class of bug this is; otherwise none. " + UNTRUSTED_NOTICE +
        ' Reply with JSON only: {"kind": "runbook" | "playbook" | null, "id": <id or null>, '
        '"confidence": <0.0-1.0>, "reasoning": "<one paragraph>"}'
    )

    def __init__(self, run: IncidentRun):
        self.run = run
        self.client = client_for(
            get_llm_config(run.project, PipelineStep.PLAYBOOK_SIMILARITY_JUDGE)
        )

    def judge(self, playbook_ids: list[int], runbook_ids: list[int]) -> JudgeResult:
        playbooks = {p.id: p for p in visible_playbooks(self.run.project).filter(id__in=playbook_ids)}
        runbooks = {r.id: r for r in self.run.project.runbooks.filter(id__in=runbook_ids)}
        if not playbooks and not runbooks:
            return JudgeResult(None, 0.0, "no candidates")
        context = incident_context(self.run) + "\n\nClassification:\n" + json.dumps(
            self.run.classification
        )
        if isinstance(self.client, JevClient):
            kind, item_id, confidence, reasoning = self._jev(context, playbooks, runbooks)
        else:
            kind, item_id, confidence, reasoning = self._chat(context, playbooks, runbooks)

        if kind == "runbook" and item_id in runbooks:
            if confidence < MATCH_CONFIDENCE_THRESHOLD:
                return JudgeResult(None, confidence, f"below threshold: {reasoning}")
            runbook = runbooks[item_id]
            return JudgeResult(runbook.playbook_id, confidence, reasoning, matched_runbook_id=item_id)
        if kind == "playbook" and item_id in playbooks:
            if confidence < MATCH_CONFIDENCE_THRESHOLD:
                return JudgeResult(None, confidence, f"below threshold: {reasoning}")
            return JudgeResult(item_id, confidence, reasoning)
        return JudgeResult(None, confidence if kind else 0.0, reasoning)

    def _jev(self, context, playbooks, runbooks):
        options = {f"rb_{r.id}": f"Runbook: {r.title} ({r.area})"[:300] for r in runbooks.values()}
        options |= {f"pb_{p.id}": f"Playbook: {p.title}: {p.description}"[:300]
                    for p in playbooks.values()}
        options["none"] = "None of these would fix this bug"
        answer = self.client.choose(context, "Which runbook or playbook would fix this bug?",
                                    options, name="playbook_similarity_judge")
        reasoning = f"Jev answered '{answer.choice}' (p={answer.confidence:.2f})"
        prefix, _, raw_id = answer.choice.partition("_")
        kind = {"rb": "runbook", "pb": "playbook"}.get(prefix)
        return kind, int(raw_id) if raw_id.isdigit() else None, answer.confidence, reasoning

    def _chat(self, context, playbooks, runbooks):
        # Agent-written runbooks and playbooks came from telemetry, so they're untrusted too.
        prompt = (context
                  + "\n\nCandidate runbooks (specific to this repo):\n"
                  + untrusted("candidate_runbooks", [runbook_card(r) for r in runbooks.values()])
                  + "\n\nCandidate playbooks (generic):\n"
                  + untrusted("candidate_playbooks", [playbook_card(p) for p in playbooks.values()]))
        data = self.client.complete_json(self.SYSTEM, prompt, name="playbook_similarity_judge")
        try:
            confidence = float(data.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        item_id = data.get("id")
        return (data.get("kind"), item_id if isinstance(item_id, int) else None, confidence,
                str(data.get("reasoning", "")))


class GenericPlaybookAuthor:
    """No playbook fits: write a generic one for the org, using this incident as one example.
    It names no files; the runbook for this repo is saved later, from a successful fix."""

    SYSTEM = (
        "You are an SRE writing a GENERIC remediation playbook for a class of bug, using one "
        "incident only as an example. It will be reused across many repositories, so never name "
        "files, functions, variables, hostnames or values from this incident: describe how to "
        "recognize the class of bug and how to find and fix its cause. Steps have a type: "
        "investigate (how to find the cause), change (what kind of fix), verify (how to prove "
        "it). No steps that commit, push, deploy or open pull requests. " + UNTRUSTED_NOTICE +
        ' Reply with JSON only: {"title": "...", "description": "...", "symptoms": "...", '
        '"keywords": ["lowercase generic search terms, e.g. exception types"], "steps": '
        '[{"type": "investigate" | "change" | "verify", "instructions": "..."}]}'
    )

    def __init__(self, run: IncidentRun):
        self.run = run
        self.client = client_for(get_llm_config(run.project, PipelineStep.PLAYBOOK_CREATION))

    def create(self, classification: Classification) -> Playbook:
        existing = Playbook.objects.filter(source_incident_run=self.run).first()
        if existing is not None:  # activity retry: don't create a second one
            return existing
        prompt = incident_context(self.run) + "\n\n" + untrusted(
            "classification", {"category": classification.category, "summary": classification.summary}
        )
        data = self.client.complete_json(self.SYSTEM, prompt, name="playbook_creation")
        keywords = [str(k).lower().strip() for k in data.get("keywords") or [] if str(k).strip()]
        project = self.run.project
        return Playbook.objects.create(
            organization_id=project.organization_id,
            project=project,
            is_generic=True,
            origin=Playbook.Origin.AGENT,
            source_incident_run=self.run,
            category=classification.category,
            title=str(data.get("title") or classification.summary or "Untitled playbook")[:255],
            description=str(data.get("description", ""))[:5000],
            symptoms=str(data.get("symptoms", ""))[:5000],
            keywords=keywords[:20],
            steps=clean_generic_steps(data.get("steps")),
            status=Playbook.Status.UNCONFIRMED,
        )
