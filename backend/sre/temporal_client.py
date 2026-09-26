import logging
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.conf import settings
from temporalio.client import (
    Client,
    ScheduleUpdate,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from .temporal_types import ApprovalDecision, GraphRefreshInput, IncidentInput, ScanInput, UptraceSyncInput

logger = logging.getLogger(__name__)

GRAPH_REFRESH_SCHEDULE_ID = "sre-service-graph-refresh"
GRAPH_REFRESH_EVERY = timedelta(minutes=15)


# Connect per call: Django may run each request on a different event loop,
# and a Temporal client is bound to the loop it was created on.
async def _connect() -> Client:
    return await Client.connect(settings.TEMPORAL_ADDRESS, namespace=settings.TEMPORAL_NAMESPACE)


async def _start(workflow_id: str, inp: IncidentInput) -> None:
    client = await _connect()
    try:
        await client.start_workflow(
            "IncidentDiagnosisWorkflow",
            inp,
            id=workflow_id,
            task_queue=settings.TEMPORAL_TASK_QUEUE,
            # Default policy would rerun the whole pipeline if Uptrace retries after completion.
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        pass  # duplicate callback for the same trace: the first workflow already owns it


class WorkflowFinished(Exception):
    """The workflow has already completed, so it can't take the signal."""


async def _signal(workflow_id: str, name: str, *args) -> None:
    client = await _connect()
    try:
        await client.get_workflow_handle(workflow_id).signal(name, *args)
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:  # Temporal's answer for a completed workflow
            raise WorkflowFinished(workflow_id) from exc
        raise


def start_incident_workflow(workflow_id: str, inp: IncidentInput) -> None:
    async_to_sync(_start)(workflow_id, inp)


def signal_approval(workflow_id: str, decision: ApprovalDecision) -> None:
    async_to_sync(_signal)(workflow_id, "approval_decision", decision)


def signal_pull_request_reopened(workflow_id: str) -> None:
    async_to_sync(_signal)(workflow_id, "pull_request_reopened")


async def _start_graph_refresh(organization_id: int, source: str) -> None:
    client = await _connect()
    try:
        await client.start_workflow(
            "ServiceGraphRefreshWorkflow",
            GraphRefreshInput(organization_id, source),
            id=f"sre-service-graph-refresh-{organization_id}",
            task_queue=settings.TEMPORAL_TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        pass  # one is already running for this org; it picks up the latest state


def start_graph_refresh(organization_id: int, source: str = "") -> None:
    async_to_sync(_start_graph_refresh)(organization_id, source)


async def _start_uptrace_sync(workflow_id: str, inp: UptraceSyncInput) -> None:
    client = await _connect()
    await client.start_workflow(
        "UptraceSyncWorkflow", inp, id=workflow_id, task_queue=settings.TEMPORAL_TASK_QUEUE,
        # A newer change replaces a sync still in flight: the sync is idempotent and
        # reads the database when it runs, so the latest one is all that matters.
        id_conflict_policy=WorkflowIDConflictPolicy.TERMINATE_EXISTING,
    )


def start_uptrace_sync(inp: UptraceSyncInput) -> None:
    key = f"project-{inp.project_id}" if inp.project_id else "uptrace-" + "-".join(
        str(i) for i in sorted(inp.uptrace_project_ids))
    async_to_sync(_start_uptrace_sync)(f"sre-uptrace-sync-{key}", inp)


async def ensure_schedules(client: Client) -> None:
    """Worker startup: the service graph refresh schedule when the mesh is on (left in
    place when it's turned off: the workflow then finds nothing to do), and every
    remediation agent's schedule, in case an API-side sync was missed."""
    if settings.SRE_REMEDIATION_AGENTS_ENABLED:
        from asgiref.sync import sync_to_async

        from .models import RemediationAgent

        agents = await sync_to_async(lambda: list(RemediationAgent.objects.all()))()
        for agent in agents:
            await _sync_agent_schedule(client, agent.id, _agent_cron(agent))
    if not settings.SRE_SERVICE_MESH_ENABLED:
        return
    try:
        await client.create_schedule(
            GRAPH_REFRESH_SCHEDULE_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    "ServiceGraphRefreshWorkflow", GraphRefreshInput(),
                    id=GRAPH_REFRESH_SCHEDULE_ID, task_queue=settings.TEMPORAL_TASK_QUEUE,
                ),
                spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=GRAPH_REFRESH_EVERY)]),
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
            ),
        )
        logger.info("created schedule %s", GRAPH_REFRESH_SCHEDULE_ID)
    except ScheduleAlreadyRunningError:
        pass


# ---- remediation agents --------------------------------------------------------------

def agent_schedule_id(agent_id: int) -> str:
    return f"sre-agent-{agent_id}-schedule"


def _agent_cron(agent) -> str:
    """The cron an agent's schedule should have, or "" for none."""
    from .models import AgentTrigger

    on = settings.SRE_REMEDIATION_AGENTS_ENABLED and agent.enabled
    return agent.schedule_cron if on and agent.trigger == AgentTrigger.SCHEDULE else ""


async def _sync_agent_schedule(client: Client, agent_id: int, cron: str) -> None:
    handle = client.get_schedule_handle(agent_schedule_id(agent_id))
    if not cron:
        try:
            await handle.delete()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
        return
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            "ActiveRemediationWorkflow", ScanInput(agent_id=agent_id),
            id=f"sre-agent-{agent_id}-scheduled", task_queue=settings.TEMPORAL_TASK_QUEUE,
        ),
        spec=ScheduleSpec(cron_expressions=[cron]),
        # A scan still running when the next one is due: skip the new one.
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )
    try:
        await client.create_schedule(agent_schedule_id(agent_id), schedule)
    except ScheduleAlreadyRunningError:
        await handle.update(lambda _: ScheduleUpdate(schedule=schedule))


async def _sync_agent_schedule_now(agent_id: int, cron: str) -> None:
    await _sync_agent_schedule(await _connect(), agent_id, cron)


def sync_agent_schedule(agent) -> None:
    """Creates, updates or deletes the agent's Temporal Schedule to match it."""
    async_to_sync(_sync_agent_schedule_now)(agent.id, _agent_cron(agent))


def delete_agent_schedule(agent_id: int) -> None:
    async_to_sync(_sync_agent_schedule_now)(agent_id, "")


async def _start_scan(workflow_id: str, inp: ScanInput) -> None:
    client = await _connect()
    try:
        await client.start_workflow(
            "ActiveRemediationWorkflow", inp, id=workflow_id, task_queue=settings.TEMPORAL_TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        pass


def start_scan(workflow_id: str, scan_run_id: int) -> None:
    async_to_sync(_start_scan)(workflow_id, ScanInput(scan_run_id=scan_run_id))


# ---- project deletion -----------------------------------------------------------------

async def cancel_workflows(client: Client, workflow_ids: list[str], reason: str) -> None:
    """Requests cancellation of each workflow, and terminates it if the cancel request
    itself fails. A workflow that has already finished (or never started) is fine. Any
    other error is raised, so the caller can stop before deleting what the workflows use."""
    for workflow_id in workflow_ids:
        handle = client.get_workflow_handle(workflow_id)
        try:
            await handle.cancel()
            continue
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:  # Temporal's answer for a finished workflow
                continue
            logger.warning("cancelling %s failed (%s); terminating it", workflow_id, exc)
        try:
            await handle.terminate(reason=reason)
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise


async def _stop_project_work(workflow_ids: list[str], agent_ids: list[int], reason: str) -> None:
    client = await _connect()
    await cancel_workflows(client, workflow_ids, reason)
    for agent_id in agent_ids:
        await _sync_agent_schedule(client, agent_id, "")


def stop_project_work(workflow_ids: list[str], agent_ids: list[int], reason: str) -> None:
    """Cancels the workflows and deletes the agents' schedules; raises if Temporal can't be
    reached or refuses."""
    async_to_sync(_stop_project_work)(workflow_ids, agent_ids, reason)
