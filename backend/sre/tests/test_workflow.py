import asyncio
import uuid
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from sre.temporal_types import (
    AnomalyResult,
    ApprovalDecision,
    AttemptInput,
    AttemptResult,
    Classification,
    IncidentInput,
    JudgeInput,
    JudgeResult,
    PlaybookRunInfo,
    PlaybookRunStatus,
    RunInput,
    SearchInput,
    StatusUpdate,
)
from sre.workflows import IncidentDiagnosisWorkflow


@dataclass
class Scenario:
    is_anomaly: bool = True
    candidates: list[int] = field(default_factory=lambda: [7])
    match: int | None = 7
    mode: str = "autonomous"
    attempt_outcomes: list[str] = field(default_factory=lambda: ["succeeded"])
    confirm_error: Exception | None = None
    calls: list[str] = field(default_factory=list)
    feedback_seen: list[str] = field(default_factory=list)
    incident_status: list[tuple[str, str]] = field(default_factory=list)
    run_status: list[str] = field(default_factory=list)


def stub_activities(s: Scenario):
    @activity.defn(name="confirm_anomaly")
    async def confirm_anomaly(inp: IncidentInput) -> AnomalyResult:
        s.calls.append("confirm_anomaly")
        if s.confirm_error:
            raise s.confirm_error
        return AnomalyResult(s.is_anomaly, "stub")

    @activity.defn(name="classify_bug")
    async def classify_bug(inp: IncidentInput) -> Classification:
        s.calls.append("classify_bug")
        return Classification("timeout", "high", "stub", keywords=["timeout", "db"])

    @activity.defn(name="find_candidate_playbooks")
    async def find_candidate_playbooks(inp: SearchInput) -> list[int]:
        s.calls.append("find_candidate_playbooks")
        assert inp.keywords == ["timeout", "db"]
        return s.candidates

    @activity.defn(name="judge_playbook_match")
    async def judge_playbook_match(inp: JudgeInput) -> JudgeResult:
        s.calls.append("judge_playbook_match")
        return JudgeResult(s.match, 0.9, "stub")

    @activity.defn(name="create_playbook")
    async def create_playbook(inp: IncidentInput) -> int:
        s.calls.append("create_playbook")
        return 99

    @activity.defn(name="create_playbook_run")
    async def create_playbook_run(inp: RunInput) -> PlaybookRunInfo:
        s.calls.append("create_playbook_run")
        return PlaybookRunInfo(55, s.mode)

    @activity.defn(name="run_playbook_attempt")
    async def run_playbook_attempt(inp: AttemptInput) -> AttemptResult:
        s.calls.append("run_playbook_attempt")
        s.feedback_seen.append(inp.previous_feedback)
        outcome = s.attempt_outcomes[inp.attempt_number - 1]
        error = "" if outcome == "succeeded" else f"attempt {inp.attempt_number} broke"
        return AttemptResult(outcome, error, f"branch-{inp.attempt_number}")

    @activity.defn(name="write_diagnosis_report")
    async def write_diagnosis_report(inp: IncidentInput) -> None:
        s.calls.append("write_diagnosis_report")

    @activity.defn(name="open_pull_request")
    async def open_pull_request(playbook_run_id: int) -> str:
        s.calls.append("open_pull_request")
        return "https://github.com/o/r/pull/1"

    @activity.defn(name="close_pull_request")
    async def close_pull_request(playbook_run_id: int) -> None:
        s.calls.append("close_pull_request")

    @activity.defn(name="set_playbook_run_status")
    async def set_playbook_run_status(inp: PlaybookRunStatus) -> None:
        s.run_status.append(inp.status)

    @activity.defn(name="record_playbook_outcome")
    async def record_playbook_outcome(playbook_run_id: int) -> None:
        s.calls.append("record_playbook_outcome")

    @activity.defn(name="mark_incident_status")
    async def mark_incident_status(inp: StatusUpdate) -> None:
        s.incident_status.append((inp.status, inp.error_message))

    return [confirm_anomaly, classify_bug, find_candidate_playbooks, judge_playbook_match,
            create_playbook, create_playbook_run, run_playbook_attempt, write_diagnosis_report,
            open_pull_request, close_pull_request, set_playbook_run_status, record_playbook_outcome,
            mark_incident_status]


def run_workflow(scenario: Scenario, decision: ApprovalDecision | None = None) -> str:
    async def go():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            queue = f"test-{uuid.uuid4()}"
            async with Worker(env.client, task_queue=queue,
                              workflows=[IncidentDiagnosisWorkflow],
                              activities=stub_activities(scenario)):
                handle = await env.client.start_workflow(
                    IncidentDiagnosisWorkflow.run, IncidentInput(1, 1),
                    id=f"wf-{uuid.uuid4()}", task_queue=queue,
                )
                if decision is not None:
                    while ("awaiting_approval", "") not in scenario.incident_status:
                        await asyncio.sleep(0.05)
                    await handle.signal(IncidentDiagnosisWorkflow.approval_decision, decision)
                return await handle.result()

    return asyncio.run(go())


def test_no_anomaly_stops_early():
    s = Scenario(is_anomaly=False)
    assert run_workflow(s) == "no_anomaly"
    assert s.calls == ["confirm_anomaly"]
    assert s.incident_status == [("no_anomaly", "")]


def test_no_candidates_creates_unconfirmed_playbook_and_stops():
    s = Scenario(candidates=[])
    assert run_workflow(s) == "new_playbook_created"
    assert "judge_playbook_match" not in s.calls
    assert "create_playbook" in s.calls
    assert "run_playbook_attempt" not in s.calls


def test_judge_rejects_candidates_creates_playbook():
    s = Scenario(match=None)
    assert run_workflow(s) == "new_playbook_created"
    assert "create_playbook" in s.calls
    assert "create_playbook_run" not in s.calls


def test_autonomous_success_records_outcome_without_approval():
    s = Scenario(mode="autonomous")
    assert run_workflow(s) == "succeeded"
    assert s.calls.count("run_playbook_attempt") == 1
    assert "open_pull_request" not in s.calls  # the attempt opens it itself
    assert s.run_status == ["succeeded"]
    assert "record_playbook_outcome" in s.calls


def test_three_failures_feed_errors_forward_then_fail():
    s = Scenario(attempt_outcomes=["failed", "failed", "failed"])
    assert run_workflow(s) == "failed"
    assert s.feedback_seen == ["", "attempt 1 broke", "attempt 2 broke"]
    assert s.run_status == ["failed"]
    assert "record_playbook_outcome" in s.calls
    assert s.incident_status[-1] == ("failed", "")


def test_retry_succeeds_on_second_attempt():
    s = Scenario(attempt_outcomes=["failed", "succeeded"])
    assert run_workflow(s) == "succeeded"
    assert s.calls.count("run_playbook_attempt") == 2


def test_draft_only_waits_for_approval_then_opens_pr():
    s = Scenario(mode="draft_only")
    assert run_workflow(s, ApprovalDecision(approve=True, user_id=3)) == "succeeded"
    assert s.run_status == ["pending_approval", "succeeded"]
    assert "open_pull_request" in s.calls


def test_draft_only_rejection_opens_no_pr():
    s = Scenario(mode="draft_only")
    assert run_workflow(s, ApprovalDecision(approve=False, user_id=3)) == "failed"
    assert s.run_status == ["pending_approval", "rejected"]
    assert "open_pull_request" not in s.calls
    assert "close_pull_request" in s.calls
    assert "record_playbook_outcome" not in s.calls


def test_draft_only_merged_on_github_touches_no_pr():
    s = Scenario(mode="draft_only")
    assert run_workflow(s, ApprovalDecision(approve=True, user_id=0, via_github=True)) == "succeeded"
    assert s.run_status == ["pending_approval", "succeeded"]
    assert "open_pull_request" not in s.calls and "close_pull_request" not in s.calls
    assert "record_playbook_outcome" in s.calls


def test_draft_only_closed_on_github_is_rejected_without_closing_again():
    s = Scenario(mode="draft_only")
    assert run_workflow(s, ApprovalDecision(approve=False, user_id=0, via_github=True)) == "failed"
    assert s.run_status == ["pending_approval", "rejected"]
    assert "close_pull_request" not in s.calls and "open_pull_request" not in s.calls


def test_advisory_writes_report_and_never_runs_agent():
    s = Scenario(mode="advisory_only")
    assert run_workflow(s) == "advisory_complete"
    assert "write_diagnosis_report" in s.calls
    assert "run_playbook_attempt" not in s.calls


def test_non_retryable_error_marks_incident_failed_with_message():
    s = Scenario(confirm_error=ApplicationError("no LLM config", non_retryable=True))
    assert run_workflow(s) == "failed"
    assert s.calls == ["confirm_anomaly"]  # not retried
    assert s.incident_status == [("failed", "no LLM config")]
