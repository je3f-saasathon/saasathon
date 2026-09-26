import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, WorkflowAlreadyStartedError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from .temporal_types import (
        MAX_ATTEMPTS,
        MAX_PARALLEL_REPOS,
        ApprovalDecision,
        AnomalyResult,
        AttemptInput,
        AttemptResult,
        Candidates,
        Classification,
        GraphRefreshInput,
        GraphTarget,
        IncidentInput,
        JudgeInput,
        JudgeResult,
        PlaybookRunInfo,
        PlaybookRunStatus,
        RootCauseResult,
        RunInput,
        ScanFinding,
        ScanInput,
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

# One repo's scan: a read-only agent loop, like a fix attempt but shorter.
SCAN_ATTEMPT = dict(
    start_to_close_timeout=timedelta(minutes=30),
    heartbeat_timeout=timedelta(minutes=10),
    retry_policy=RetryPolicy(maximum_attempts=1),
)

# After a rejection, how long a draft-only run keeps listening for its PR to be reopened
# (or re-opened from the same branch) on GitHub before the workflow ends for good.
REOPEN_WINDOW = timedelta(days=30)


@workflow.defn
class IncidentDiagnosisWorkflow:
    def __init__(self) -> None:
        self._decision: ApprovalDecision | None = None
        self._reopened = False

    @workflow.signal
    def approval_decision(self, decision: ApprovalDecision) -> None:
        self._decision = decision

    @workflow.signal
    def pull_request_reopened(self) -> None:
        self._reopened = True

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
        if workflow.patched("service-mesh-v1"):
            cause: RootCauseResult = await workflow.execute_activity(
                "localize_root_cause", inp, result_type=RootCauseResult, **READ_ONLY
            )
            if cause.child_incident_run_id is not None:
                await self._delegate(cause)
                return "delegated"
        anomaly: AnomalyResult = await workflow.execute_activity(
            "confirm_anomaly", inp, result_type=AnomalyResult, **READ_ONLY
        )
        if not anomaly.is_anomaly:
            return "no_anomaly"

        classification: Classification = await workflow.execute_activity(
            "classify_bug", inp, result_type=Classification, **READ_ONLY
        )
        match = JudgeResult(None, 0.0, "no candidates")
        # Runbooks + playbooks in one search and one judge call. Guarded so workflows that
        # already ran the old two activities still replay (e.g. ones awaiting approval).
        if workflow.patched("runbooks-v1"):
            candidates: Candidates = await workflow.execute_activity(
                "find_candidates",
                SearchInput(inp.project_id, classification.keywords, inp.incident_run_id,
                            classification.category),
                result_type=Candidates,
                **READ_ONLY,
            )
            if candidates.playbook_ids or candidates.runbook_ids:
                match = await workflow.execute_activity(
                    "judge_match",
                    JudgeInput(inp.incident_run_id, candidates.playbook_ids, candidates.runbook_ids),
                    result_type=JudgeResult,
                    **READ_ONLY,
                )
        else:
            candidate_ids: list[int] = await workflow.execute_activity(
                "find_candidate_playbooks",
                SearchInput(inp.project_id, classification.keywords),
                result_type=list[int],
                **READ_ONLY,
            )
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
            RunInput(inp.incident_run_id, match.matched_playbook_id, match.matched_runbook_id),
            result_type=PlaybookRunInfo,
            **IDEMPOTENT_WRITE,
        )

        if info.execution_mode == "advisory_only":
            await workflow.execute_activity("write_diagnosis_report", inp, **READ_ONLY)
            await self._run_status(info, "succeeded")
            return "advisory_complete"

        return await self._execute(inp, info)

    async def _delegate(self, cause: RootCauseResult) -> None:
        """Starts the culprit project's incident. Abandoned, not awaited: it has its own
        approvals and may wait on them for days."""
        try:
            await workflow.start_child_workflow(
                IncidentDiagnosisWorkflow.run,
                IncidentInput(cause.child_incident_run_id, cause.child_project_id),
                id=cause.child_workflow_id,
                parent_close_policy=ParentClosePolicy.ABANDON,
            )
        except WorkflowAlreadyStartedError:
            pass  # a retried parent: the child already runs

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
            if not await self._await_review(inp, info):
                return "rejected"

        await self._run_status(info, "succeeded")
        await workflow.execute_activity(
            "record_playbook_outcome", info.playbook_run_id, **IDEMPOTENT_WRITE
        )
        return "succeeded"

    async def _await_review(self, inp: IncidentInput, info: PlaybookRunInfo) -> bool:
        """Draft-only: wait for the PR to be approved (merged) or rejected (closed). A rejected
        PR that's reopened within REOPEN_WINDOW goes back up for review. True once approved."""
        await self._run_status(info, "pending_approval")
        await self._status(inp, "awaiting_approval")
        while True:
            await workflow.wait_condition(lambda: self._decision is not None)
            decision = self._decision
            if decision.approve:
                if not decision.via_github:
                    await workflow.execute_activity(
                        "open_pull_request", info.playbook_run_id, result_type=str, **IDEMPOTENT_WRITE
                    )
                return True

            if not decision.via_github:
                await workflow.execute_activity(
                    "close_pull_request", info.playbook_run_id, **IDEMPOTENT_WRITE
                )
            await self._run_status(info, "rejected")
            await self._status(inp, "rejected")
            self._reopened = False
            try:
                # A merge can also arrive directly if the reopen delivery was lost.
                await workflow.wait_condition(
                    lambda: self._reopened or (self._decision is not None and self._decision.approve),
                    timeout=REOPEN_WINDOW,
                )
            except asyncio.TimeoutError:
                return False
            if self._decision is not None and self._decision.approve:
                continue  # merged (even if the reopen arrived too): the approve branch handles it
            # Reopened: forget the rejection and wait for a fresh decision.
            self._decision = None
            await self._run_status(info, "pending_approval")
            await self._status(inp, "awaiting_approval")

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


@workflow.defn
class ServiceGraphRefreshWorkflow:
    """Copies Uptrace's service graph into every (org, Uptrace project) graph, one at a
    time. Run every 15 minutes by a Temporal Schedule (see worker.py) and on demand."""

    @workflow.run
    async def run(self, inp: GraphRefreshInput) -> int:
        targets: list[GraphTarget] = await workflow.execute_activity(
            "list_graph_targets", inp, result_type=list[GraphTarget], **READ_ONLY
        )
        refreshed = 0
        for target in targets:
            try:
                if await workflow.execute_activity(
                    "refresh_service_graph", target, result_type=bool, **READ_ONLY
                ):
                    refreshed += 1
            except ActivityError:
                workflow.logger.warning("service graph refresh failed for %s", target)
        return refreshed


@workflow.defn
class ActiveRemediationWorkflow:
    """A remediation agent's scan run: scans each repo (at most MAX_PARALLEL_REPOS at once)
    and starts the incident pipeline for every new finding. Findings' pipelines are
    abandoned children: they can wait days on a draft PR's review."""

    @workflow.run
    async def run(self, inp: ScanInput) -> str:
        scan_run_id = inp.scan_run_id
        if not scan_run_id:
            scan_run_id = await workflow.execute_activity(
                "create_scheduled_scan", inp, result_type=int, **IDEMPOTENT_WRITE
            )
            if not scan_run_id:
                return "skipped"
        repo_ids: list[int] = await workflow.execute_activity(
            "list_scan_repos", scan_run_id, result_type=list[int], **READ_ONLY
        )
        slots = asyncio.Semaphore(MAX_PARALLEL_REPOS)

        async def scan(repo_id: int) -> None:
            async with slots:
                try:
                    findings: list[ScanFinding] = await workflow.execute_activity(
                        "scan_repository", repo_id, result_type=list[ScanFinding], **SCAN_ATTEMPT
                    )
                except ActivityError:
                    return  # the activity marked the repo failed; finish_scan counts it
            for finding in findings:
                try:
                    await workflow.start_child_workflow(
                        IncidentDiagnosisWorkflow.run,
                        IncidentInput(finding.incident_run_id, finding.project_id),
                        id=finding.workflow_id,
                        parent_close_policy=ParentClosePolicy.ABANDON,
                    )
                except WorkflowAlreadyStartedError:
                    pass

        await asyncio.gather(*(scan(repo_id) for repo_id in repo_ids))
        return await workflow.execute_activity(
            "finish_scan", scan_run_id, result_type=str, **IDEMPOTENT_WRITE
        )
