import logging
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.conf import settings
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from .temporal_types import ApprovalDecision, GraphRefreshInput, IncidentInput

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


async def ensure_schedules(client: Client) -> None:
    """Creates the service graph refresh schedule when the mesh is on (idempotent). It's
    left in place when the mesh is turned off: the workflow then finds nothing to do."""
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
