"""Run with: uv run python -m sre.worker"""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.conf import settings  # noqa: E402
from temporalio.client import Client  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from sre.activities import ALL_ACTIVITIES  # noqa: E402
from sre.temporal_client import ensure_schedules  # noqa: E402
from sre.workflows import (  # noqa: E402
    ActiveRemediationWorkflow,
    IncidentDiagnosisWorkflow,
    ServiceGraphRefreshWorkflow,
)

MAX_CONCURRENT_ACTIVITIES = 8


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    client = await Client.connect(settings.TEMPORAL_ADDRESS, namespace=settings.TEMPORAL_NAMESPACE)
    await ensure_schedules(client)
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_ACTIVITIES) as executor:
        worker = Worker(
            client,
            task_queue=settings.TEMPORAL_TASK_QUEUE,
            workflows=[IncidentDiagnosisWorkflow, ServiceGraphRefreshWorkflow,
                       ActiveRemediationWorkflow],
            activities=ALL_ACTIVITIES,
            activity_executor=executor,
            max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES,
        )
        logging.info("SRE worker polling task queue %s on %s",
                     settings.TEMPORAL_TASK_QUEUE, settings.TEMPORAL_ADDRESS)
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
