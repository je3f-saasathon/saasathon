import asyncio
import uuid
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from sre.temporal_types import (
    UptraceSyncInput,
    AnomalyResult,
    ApprovalDecision,
    AttemptInput,
    AttemptResult,
    Candidates,
    Classification,
    IncidentInput,
    JudgeInput,
    JudgeResult,
    PlaybookRunInfo,
    PlaybookRunStatus,
    RootCauseResult,
    RunInput,
    GraphRefreshInput,
    GraphTarget,
    ScanFinding,
    ScanInput,
    SearchInput,
    StatusUpdate,
)
from sre.workflows import (
    ActiveRemediationWorkflow, IncidentDiagnosisWorkflow, ServiceGraphRefreshWorkflow, UptraceSyncWorkflow,
)


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
    telemetry_fetches: int = 0
    runbook_candidates: list[int] = field(default_factory=list)
    runbook_match: int | None = None
    judged_runbooks: list[int] = field(default_factory=list)
    run_inputs: list = field(default_factory=list)
    # Service mesh: the linked child incident localize_root_cause hands back, if any.
    delegate_to: int | None = None
    localized: list[int] = field(default_factory=list)
    child_workflow_ids: list[str] = field(default_factory=list)
    # The fix attempt runs until it's cancelled (project deletion tests).
    block_attempt: bool = False


def stub_activities(s: Scenario):
    @activity.defn(name="fetch_incident_telemetry")
    async def fetch_incident_telemetry(inp: IncidentInput) -> None:
        # Not in s.calls, so the call-order assertions stay about the pipeline itself.
        s.telemetry_fetches += 1

    @activity.defn(name="localize_root_cause")
    async def localize_root_cause(inp: IncidentInput) -> RootCauseResult:
        s.localized.append(inp.incident_run_id)
        if s.delegate_to is None or inp.incident_run_id == s.delegate_to:
            return RootCauseResult()  # the child finds the culprit is its own service
        s.child_workflow_ids.append(f"child-{uuid.uuid4()}")
        return RootCauseResult(s.delegate_to, 2, s.child_workflow_ids[-1])

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

    @activity.defn(name="find_candidates")
    async def find_candidates(inp: SearchInput) -> Candidates:
        s.calls.append("find_candidates")
        assert inp.keywords == ["timeout", "db"] and inp.category == "timeout"
        return Candidates(playbook_ids=s.candidates, runbook_ids=s.runbook_candidates)

    @activity.defn(name="judge_match")
    async def judge_match(inp: JudgeInput) -> JudgeResult:
        s.calls.append("judge_match")
        s.judged_runbooks = inp.runbook_ids
        return JudgeResult(s.match, 0.9, "stub", matched_runbook_id=s.runbook_match)

    @activity.defn(name="create_playbook")
    async def create_playbook(inp: IncidentInput) -> int:
        s.calls.append("create_playbook")
        return 99

    @activity.defn(name="create_playbook_run")
    async def create_playbook_run(inp: RunInput) -> PlaybookRunInfo:
        s.calls.append("create_playbook_run")
        s.run_inputs.append(inp)
        return PlaybookRunInfo(55, s.mode)

    @activity.defn(name="run_playbook_attempt")
    async def run_playbook_attempt(inp: AttemptInput) -> AttemptResult:
        s.calls.append("run_playbook_attempt")
        s.feedback_seen.append(inp.previous_feedback)
        while s.block_attempt:
            activity.heartbeat()
            await asyncio.sleep(0.05)
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

    return [fetch_incident_telemetry, localize_root_cause, confirm_anomaly, classify_bug, find_candidates, judge_match,
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


def run_review(scenario: Scenario, steps: list[tuple[str, str | None]]) -> str:
    """Draft-only run driven by GitHub events. Each step is (event, state): the event
    ("merge", "close" or "reopen") is sent once the run's latest status is `state`, or
    straight away if `state` is None."""
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
                for event, state in steps:
                    while state and (not scenario.run_status or scenario.run_status[-1] != state):
                        await asyncio.sleep(0.05)
                    if event == "reopen":
                        await handle.signal(IncidentDiagnosisWorkflow.pull_request_reopened)
                    else:
                        await handle.signal(IncidentDiagnosisWorkflow.approval_decision,
                                            ApprovalDecision(event == "merge", 0, via_github=True))
                return await handle.result()

    return asyncio.run(go())


PENDING, REJECTED = "pending_approval", "rejected"


def test_no_anomaly_stops_early():
    s = Scenario(is_anomaly=False)
    assert run_workflow(s) == "no_anomaly"
    assert s.calls == ["confirm_anomaly"]
    assert s.incident_status == [("no_anomaly", "")]
    assert s.telemetry_fetches == 1  # before triage, even when triage stops early


def test_no_candidates_creates_unconfirmed_playbook_and_stops():
    s = Scenario(candidates=[])
    assert run_workflow(s) == "new_playbook_created"
    assert "judge_match" not in s.calls
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
    assert run_workflow(s, ApprovalDecision(approve=False, user_id=3)) == "rejected"
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
    assert run_workflow(s, ApprovalDecision(approve=False, user_id=0, via_github=True)) == "rejected"
    assert s.run_status == ["pending_approval", "rejected"]
    assert "close_pull_request" not in s.calls and "open_pull_request" not in s.calls


def test_closed_then_reopened_then_merged_succeeds():
    s = Scenario(mode="draft_only")
    steps = [("close", PENDING), ("reopen", REJECTED), ("merge", PENDING)]
    assert run_review(s, steps) == "succeeded"
    assert s.run_status == [PENDING, REJECTED, PENDING, "succeeded"]
    statuses = [status for status, _ in s.incident_status]
    assert statuses == ["awaiting_approval", "rejected", "awaiting_approval", "succeeded"]
    assert s.calls.count("record_playbook_outcome") == 1
    assert "open_pull_request" not in s.calls and "close_pull_request" not in s.calls


def test_reopened_pr_can_be_rejected_again():
    s = Scenario(mode="draft_only")
    steps = [("close", PENDING), ("reopen", REJECTED), ("close", PENDING)]
    assert run_review(s, steps) == "rejected"
    assert s.run_status == [PENDING, REJECTED, PENDING, REJECTED]
    assert "record_playbook_outcome" not in s.calls


def test_rejected_pr_never_reopened_ends_after_the_window():
    s = Scenario(mode="draft_only")
    assert run_review(s, [("close", PENDING)]) == "rejected"  # time-skips past REOPEN_WINDOW
    assert s.run_status == [PENDING, REJECTED]
    # Rejected, not failed: the agent did produce a fix.
    assert "failed" not in [status for status, _ in s.incident_status]


def test_merge_after_rejection_counts_even_if_reopen_was_missed():
    s = Scenario(mode="draft_only")
    assert run_review(s, [("close", PENDING), ("merge", REJECTED)]) == "succeeded"
    assert s.run_status == [PENDING, REJECTED, "succeeded"]
    assert s.calls.count("record_playbook_outcome") == 1


def test_reopen_and_merge_arriving_together_approve():
    s = Scenario(mode="draft_only")
    assert run_review(s, [("close", PENDING), ("reopen", REJECTED), ("merge", None)]) == "succeeded"
    assert s.run_status[-1] == "succeeded"
    assert s.calls.count("record_playbook_outcome") == 1


def test_duplicate_close_while_rejected_does_not_block_reopen():
    s = Scenario(mode="draft_only")
    steps = [("close", PENDING), ("close", REJECTED), ("reopen", REJECTED), ("merge", PENDING)]
    assert run_review(s, steps) == "succeeded"


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


def test_runbook_candidates_reach_the_judge_and_the_run():
    s = Scenario(candidates=[7], runbook_candidates=[3], match=7, runbook_match=3)
    assert run_workflow(s) == "succeeded"
    assert s.judged_runbooks == [3]
    assert s.run_inputs[0].playbook_id == 7 and s.run_inputs[0].runbook_id == 3


def test_only_runbook_candidates_still_get_judged():
    s = Scenario(candidates=[], runbook_candidates=[3], match=7, runbook_match=3)
    assert run_workflow(s) == "succeeded"
    assert "judge_match" in s.calls


@pytest.mark.parametrize("fixture,workflow_id", [
    # Main before this milestone, and main after PR #8 (reopen after reject): what prod ran.
    ("workflow_history_pre_runbooks.json", "pre-runbooks-awaiting-approval"),
    ("workflow_history_pr8_awaiting_approval.json", "main-pr8-awaiting-approval"),
])
def test_workflow_from_before_runbooks_still_replays(fixture, workflow_id):
    """Recorded with the pre-runbooks workflow: a draft_only run paused awaiting approval,
    which a deploy must not break. The new activities sit behind workflow.patched(), so
    replaying it must not raise."""
    from pathlib import Path

    from temporalio.client import WorkflowHistory
    from temporalio.worker import Replayer

    raw = (Path(__file__).parent / "fixtures" / fixture).read_text()
    history = WorkflowHistory.from_json(workflow_id, raw)
    asyncio.run(Replayer(workflows=[IncidentDiagnosisWorkflow]).replay_workflow(history))


def test_culprit_in_another_project_delegates_to_a_linked_child():
    s = Scenario(delegate_to=8)

    async def go():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            queue = f"test-{uuid.uuid4()}"
            async with Worker(env.client, task_queue=queue, workflows=[IncidentDiagnosisWorkflow],
                              activities=stub_activities(s)):
                parent = await env.client.execute_workflow(
                    IncidentDiagnosisWorkflow.run, IncidentInput(1, 1), id=f"wf-{uuid.uuid4()}",
                    task_queue=queue,
                )
                child = await env.client.get_workflow_handle(s.child_workflow_ids[0]).result()
                return parent, child

    assert asyncio.run(go()) == ("delegated", "succeeded")
    # The parent stops before triage; only the child (run 8) runs the pipeline.
    assert s.localized == [1, 8]
    assert s.calls.count("confirm_anomaly") == 1
    assert ("delegated", "") in s.incident_status and ("succeeded", "") in s.incident_status


def test_graph_refresh_counts_successes_and_skips_failures():
    refreshed: list[str] = []

    @activity.defn(name="list_graph_targets")
    async def list_graph_targets(inp: GraphRefreshInput) -> list[GraphTarget]:
        return [GraphTarget(1, "u.example/1"), GraphTarget(1, "u.example/2"), GraphTarget(2, "u.example/1")]

    @activity.defn(name="refresh_service_graph")
    async def refresh_service_graph(target: GraphTarget) -> bool:
        if target.source.endswith("/2"):
            raise ApplicationError("boom", non_retryable=True)
        refreshed.append(f"{target.organization_id}:{target.source}")
        return target.organization_id == 1

    async def go():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            queue = f"test-{uuid.uuid4()}"
            async with Worker(env.client, task_queue=queue, workflows=[ServiceGraphRefreshWorkflow],
                              activities=[list_graph_targets, refresh_service_graph]):
                return await env.client.execute_workflow(
                    ServiceGraphRefreshWorkflow.run, GraphRefreshInput(), id=f"wf-{uuid.uuid4()}",
                    task_queue=queue,
                )

    assert asyncio.run(go()) == 1
    assert refreshed == ["1:u.example/1", "2:u.example/1"]


def run_scan(inp: ScanInput, repo_ids: list[int], findings: dict[int, list[int]], fail: set[int] = frozenset()):
    """ActiveRemediationWorkflow with stub activities. Each finding's incident pipeline runs
    too (the incident stubs), so the test can count them."""
    s = Scenario()
    log = {"in_flight": 0, "max_in_flight": 0, "created": [], "finished": []}

    @activity.defn(name="create_scheduled_scan")
    async def create_scheduled_scan(inp: ScanInput) -> int:
        log["created"].append(inp.agent_id)
        return 0 if inp.agent_id == 404 else 77

    @activity.defn(name="list_scan_repos")
    async def list_scan_repos(scan_run_id: int) -> list[int]:
        return repo_ids

    @activity.defn(name="scan_repository")
    async def scan_repository(repo_id: int) -> list[ScanFinding]:
        log["in_flight"] += 1
        log["max_in_flight"] = max(log["max_in_flight"], log["in_flight"])
        await asyncio.sleep(0.05)
        log["in_flight"] -= 1
        if repo_id in fail:
            raise ApplicationError("sandbox broke", non_retryable=True)
        return [ScanFinding(i, 1, f"incident-{i}-{uuid.uuid4()}") for i in findings.get(repo_id, [])]

    @activity.defn(name="finish_scan")
    async def finish_scan(scan_run_id: int) -> str:
        log["finished"].append(scan_run_id)
        return "partial" if fail else "succeeded"

    async def go():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            queue = f"test-{uuid.uuid4()}"
            async with Worker(env.client, task_queue=queue,
                              workflows=[ActiveRemediationWorkflow, IncidentDiagnosisWorkflow],
                              activities=[create_scheduled_scan, list_scan_repos, scan_repository,
                                          finish_scan, *stub_activities(s)]):
                result = await env.client.execute_workflow(
                    ActiveRemediationWorkflow.run, inp, id=f"wf-{uuid.uuid4()}", task_queue=queue)
                # Findings' pipelines are abandoned children; let them finish before counting.
                expected = sum(len(v) for k, v in findings.items() if k not in fail)
                for _ in range(200):
                    if len([x for x in s.incident_status if x[0] == "succeeded"]) >= expected:
                        break
                    await asyncio.sleep(0.05)
                return result

    return asyncio.run(go()), log, s


def test_scan_runs_at_most_two_repos_at_once_and_starts_each_findings_pipeline():
    result, log, s = run_scan(ScanInput(scan_run_id=5), [1, 2, 3, 4], {1: [11], 3: [31, 32]})
    assert result == "succeeded" and log["finished"] == [5]
    assert log["max_in_flight"] == 2
    assert sorted(s.localized) == [11, 31, 32]  # one incident pipeline per finding


def test_a_failed_repo_does_not_stop_the_others():
    result, log, s = run_scan(ScanInput(scan_run_id=5), [1, 2], {2: [21]}, fail={1})
    assert result == "partial"
    assert s.localized == [21]


def test_a_scheduled_run_creates_its_scan_run_and_skips_a_gone_agent():
    result, log, _ = run_scan(ScanInput(agent_id=9), [], {})
    assert (result, log["created"], log["finished"]) == ("succeeded", [9], [77])
    result, log, _ = run_scan(ScanInput(agent_id=404), [], {})
    assert (result, log["finished"]) == ("skipped", [])


def run_uptrace_sync(inp: UptraceSyncInput, provisioned: int, flaky: int = 0):
    """UptraceSyncWorkflow with stub activities; sync fails `flaky` times before working."""
    log = {"provision": [], "sync": []}

    @activity.defn(name="provision_managed_uptrace")
    async def provision_managed_uptrace(i: UptraceSyncInput) -> int:
        log["provision"].append(i.project_id)
        return provisioned

    @activity.defn(name="sync_managed_uptrace")
    async def sync_managed_uptrace(i: UptraceSyncInput) -> None:
        log["sync"].append((i.base_url, list(i.uptrace_project_ids)))
        if len(log["sync"]) <= flaky:
            raise ApplicationError("Uptrace is unreachable")

    async def go():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            queue = f"test-{uuid.uuid4()}"
            async with Worker(env.client, task_queue=queue, workflows=[UptraceSyncWorkflow],
                              activities=[provision_managed_uptrace, sync_managed_uptrace]):
                await env.client.execute_workflow(UptraceSyncWorkflow.run, inp,
                                                  id=f"wf-{uuid.uuid4()}", task_queue=queue)

    asyncio.run(go())
    return log


def test_uptrace_sync_provisions_then_syncs_old_and_new_uptrace_projects():
    log = run_uptrace_sync(UptraceSyncInput("https://api.example", project_id=5,
                                            uptrace_project_ids=[3]), provisioned=9)
    assert log["provision"] == [5]
    assert log["sync"] == [("https://api.example", [3, 9])]


def test_uptrace_sync_without_a_project_only_syncs_and_retries_uptrace_outages():
    log = run_uptrace_sync(UptraceSyncInput("https://api.example", uptrace_project_ids=[4]),
                           provisioned=0, flaky=2)
    assert log["provision"] == []
    assert log["sync"] == [("https://api.example", [4])] * 3


def test_uptrace_sync_skips_a_project_that_is_no_longer_managed():
    log = run_uptrace_sync(UptraceSyncInput("https://api.example", project_id=5), provisioned=0)
    assert log["provision"] == [5] and log["sync"] == []


# ---- cancellation (project deletion) --------------------------------------------------

def run_and_cancel(scenario: Scenario, ready):
    """Starts an incident workflow, cancels it through temporal_client.cancel_workflows once
    ready(scenario) holds, and returns the cause of its failure. Then cancels it again (now
    finished) along with a workflow that never started: neither may raise."""
    from sre.temporal_client import cancel_workflows

    async def go():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            queue = f"test-{uuid.uuid4()}"
            async with Worker(env.client, task_queue=queue, workflows=[IncidentDiagnosisWorkflow],
                              activities=stub_activities(scenario)):
                wid = f"wf-{uuid.uuid4()}"
                handle = await env.client.start_workflow(
                    IncidentDiagnosisWorkflow.run, IncidentInput(1, 1), id=wid, task_queue=queue)
                while not ready(scenario):
                    await asyncio.sleep(0.05)
                await cancel_workflows(env.client, [wid], "test")
                with pytest.raises(WorkflowFailureError) as failure:
                    await handle.result()
                await cancel_workflows(env.client, [wid, f"missing-{uuid.uuid4()}"], "test")
                return failure.value.cause

    return asyncio.run(go())


def test_cancelling_a_run_awaiting_review_ends_it_without_marking_it_failed():
    s = Scenario(mode="draft_only")
    cause = run_and_cancel(s, lambda s: ("awaiting_approval", "") in s.incident_status)
    assert isinstance(cause, CancelledError)
    assert [status for status, _ in s.incident_status] == ["awaiting_approval"]


def test_cancelling_during_a_fix_attempt_does_not_mark_it_failed():
    s = Scenario(block_attempt=True)
    cause = run_and_cancel(s, lambda s: "run_playbook_attempt" in s.calls)
    assert isinstance(cause, CancelledError)
    assert s.incident_status == [] and s.run_status == []


def test_cancelling_a_scan_run_skips_finish_scan():
    from sre.temporal_client import cancel_workflows

    log = []

    @activity.defn(name="list_scan_repos")
    async def list_scan_repos(scan_run_id: int) -> list[int]:
        return [1]

    @activity.defn(name="scan_repository")
    async def scan_repository(repo_id: int) -> list[ScanFinding]:
        log.append("scanning")
        while True:
            activity.heartbeat()
            await asyncio.sleep(0.05)

    @activity.defn(name="finish_scan")
    async def finish_scan(scan_run_id: int) -> str:
        log.append("finish_scan")
        return "succeeded"

    async def go():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            queue = f"test-{uuid.uuid4()}"
            async with Worker(env.client, task_queue=queue, workflows=[ActiveRemediationWorkflow],
                              activities=[list_scan_repos, scan_repository, finish_scan]):
                wid = f"wf-{uuid.uuid4()}"
                handle = await env.client.start_workflow(
                    ActiveRemediationWorkflow.run, ScanInput(scan_run_id=5), id=wid, task_queue=queue)
                while not log:
                    await asyncio.sleep(0.05)
                await cancel_workflows(env.client, [wid], "test")
                with pytest.raises(WorkflowFailureError) as failure:
                    await handle.result()
                return failure.value.cause

    assert isinstance(asyncio.run(go()), CancelledError)
    assert log == ["scanning"]
