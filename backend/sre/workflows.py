from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from .temporal_types import (
        MAX_ATTEMPTS,
        ApprovalDecision,
        AnomalyResult,
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

# Temporal's default is to retry forever; every activity gets an explicit policy.
READ_ONLY = dict(
    start_to_close_timeout=timedelta(minutes=2),
    retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5)),
)
# Row-creating activities are idempotent, so retrying them is safe.
IDEMPOTENT_WRITE = READ_ONLY
# The re-plan loop is the retry; Temporal must not rerun an attempt on its own.
AGENT_ATTEMPT = dict(
    start_to_close_timeout=timedelta(minutes=45),
    heartbeat_timeout=timedelta(minutes=10),  # > the sandbox's 5-minute command timeout
    retry_policy=RetryPolicy(maximum_attempts=1),
)


@workflow.defn
class IncidentDiagnosisWorkflow:
    def __init__(self) -> None:
        self._decision: ApprovalDecision | None = None

    @workflow.signal
    def approval_decision(self, decision: ApprovalDecision) -> None:
        self._decision = decision

    @workflow.run
    async def run(self, inp: IncidentInput) -> str:
        try:
            status = await self._run(inp)
        except ActivityError as exc:
            cause = exc.cause
            message = cause.message if isinstance(cause, ApplicationError) else str(cause or exc)
            await self._status(inp, "failed", message)
            return "failed"
        await self._status(inp, status)
        return status

    async def _run(self, inp: IncidentInput) -> str:
        # Guarded so workflows started before this activity existed still replay.
        if workflow.patched("uptrace-telemetry-v1"):
            await workflow.execute_activity("fetch_incident_telemetry", inp, **READ_ONLY)
        anomaly: AnomalyResult = await workflow.execute_activity(
            "confirm_anomaly", inp, result_type=AnomalyResult, **READ_ONLY
        )
        if not anomaly.is_anomaly:
            return "no_anomaly"

        classification: Classification = await workflow.execute_activity(
            "classify_bug", inp, result_type=Classification, **READ_ONLY
        )
        candidate_ids: list[int] = await workflow.execute_activity(
            "find_candidate_playbooks",
            SearchInput(inp.project_id, classification.keywords),
            result_type=list[int],
            **READ_ONLY,
        )
        match = JudgeResult(None, 0.0, "no candidates")
        if candidate_ids:
            match = await workflow.execute_activity(
                "judge_playbook_match",
                JudgeInput(inp.incident_run_id, candidate_ids),
                result_type=JudgeResult,
                **READ_ONLY,
            )

        if match.matched_playbook_id is None:
            # New playbooks are only run on a later incident that matches them.
            await workflow.execute_activity("create_playbook", inp, result_type=int, **IDEMPOTENT_WRITE)
            return "new_playbook_created"

        info: PlaybookRunInfo = await workflow.execute_activity(
            "create_playbook_run",
            RunInput(inp.incident_run_id, match.matched_playbook_id),
            result_type=PlaybookRunInfo,
            **IDEMPOTENT_WRITE,
        )

        if info.execution_mode == "advisory_only":
            await workflow.execute_activity("write_diagnosis_report", inp, **READ_ONLY)
            await self._run_status(info, "succeeded")
            return "advisory_complete"

        return await self._execute(inp, info)

    async def _execute(self, inp: IncidentInput, info: PlaybookRunInfo) -> str:
        feedback = ""
        for attempt_number in range(1, MAX_ATTEMPTS + 1):
            result: AttemptResult = await workflow.execute_activity(
                "run_playbook_attempt",
                AttemptInput(info.playbook_run_id, attempt_number, feedback),
                result_type=AttemptResult,
                **AGENT_ATTEMPT,
            )
            if result.outcome == "succeeded":
                break
            feedback = result.error_output
        else:
            await self._run_status(info, "failed")
            await workflow.execute_activity(
                "record_playbook_outcome", info.playbook_run_id, **IDEMPOTENT_WRITE
            )
            return "failed"

        if info.execution_mode == "draft_only":
            await self._run_status(info, "pending_approval")
            await self._status(inp, "awaiting_approval")
            await workflow.wait_condition(lambda: self._decision is not None)
            if not self._decision.approve:
                if not self._decision.via_github:
                    await workflow.execute_activity(
                        "close_pull_request", info.playbook_run_id, **IDEMPOTENT_WRITE
                    )
                await self._run_status(info, "rejected")
                return "failed"
            if not self._decision.via_github:
                await workflow.execute_activity(
                    "open_pull_request", info.playbook_run_id, result_type=str, **IDEMPOTENT_WRITE
                )

        await self._run_status(info, "succeeded")
        await workflow.execute_activity(
            "record_playbook_outcome", info.playbook_run_id, **IDEMPOTENT_WRITE
        )
        return "succeeded"

    async def _status(self, inp: IncidentInput, status: str, error: str = "") -> None:
        await workflow.execute_activity(
            "mark_incident_status", StatusUpdate(inp.incident_run_id, status, error), **IDEMPOTENT_WRITE
        )

    async def _run_status(self, info: PlaybookRunInfo, status: str) -> None:
        await workflow.execute_activity(
            "set_playbook_run_status",
            PlaybookRunStatus(info.playbook_run_id, status),
            **IDEMPOTENT_WRITE,
        )
