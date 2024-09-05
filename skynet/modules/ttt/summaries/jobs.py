import asyncio
import time
import uuid

from skynet.auth.openai import CredentialsType, get_credentials

from skynet.env import job_timeout, modules, redis_exp_seconds, summary_minimum_payload_length
from skynet.logs import get_logger
from skynet.modules.monitoring import (
    SUMMARY_DURATION_METRIC,
    SUMMARY_ERROR_COUNTER,
    SUMMARY_INPUT_LENGTH_METRIC,
    SUMMARY_QUEUE_SIZE_METRIC,
    SUMMARY_TIME_IN_QUEUE_METRIC,
)
from skynet.modules.ttt.openai_api.app import restart as restart_openai_api

from .persistence import db
from .processor import process, process_azure, process_open_ai
from .v1.models import DocumentMetadata, DocumentPayload, Job, JobId, JobStatus, JobType, Processors

log = get_logger(__name__)

TIME_BETWEEN_JOBS_CHECK = 2
TIME_BETWEEN_JOBS_CHECK_ON_ERROR = 10

PENDING_JOBS_KEY = "jobs:pending"
RUNNING_JOBS_KEY = "jobs:running"
ERROR_JOBS_KEY = "jobs:error"

background_task = None
current_task = None


def can_run_next_job() -> bool:
    return 'summaries:executor' in modules and (current_task is None or current_task.done())


def get_job_processor(customer_id: str) -> str:
    options = get_credentials(customer_id)
    secret = options.get('secret')
    api_type = options.get('type')

    if secret:
        if api_type == CredentialsType.OPENAI.value:
            return Processors.OPENAI
        elif api_type == CredentialsType.AZURE_OPENAI.value:
            return Processors.AZURE

    return Processors.LOCAL


async def update_summary_queue_metric() -> None:
    """Update the queue size metric."""

    queue_size = await db.llen(PENDING_JOBS_KEY)
    SUMMARY_QUEUE_SIZE_METRIC.set(queue_size)


async def restore_stale_jobs() -> list[Job]:
    """Check if any jobs were running on disconnected workers and requeue them."""

    running_jobs_keys = await db.lrange(RUNNING_JOBS_KEY, 0, -1)
    running_jobs = await db.mget(running_jobs_keys)
    connected_clients = await db.client_list()
    stale_jobs = []

    for job_json in running_jobs:
        job = Job.model_validate_json(job_json)

        if str(job.worker_id) not in [client['id'] for client in connected_clients]:
            stale_jobs.append(job)

    if stale_jobs:
        ids = [job.id for job in stale_jobs]
        log.info(f"Restoring stale job(s): {ids}")
        await db.lpush(PENDING_JOBS_KEY, *[job.id for job in stale_jobs])
        await update_summary_queue_metric()


async def create_job(job_type: JobType, payload: DocumentPayload, metadata: DocumentMetadata) -> JobId:
    """Create a job and add it to the db queue if it can't be started immediately."""

    job_id = str(uuid.uuid4())

    job = Job(
        id=job_id, payload=payload, type=job_type, metadata=metadata, processor=get_job_processor(metadata.customer_id)
    )
    await db.set(job_id, Job.model_dump_json(job))

    log.info(f"Created job {job.id}.")

    await db.rpush(PENDING_JOBS_KEY, job_id)
    await update_summary_queue_metric()

    return JobId(id=job_id)


async def get_job(job_id: str) -> Job:
    job_json = await db.get(job_id)
    job = Job.model_validate_json(job_json) if job_json else None

    return job


async def update_job(job_id: str, expires: int = None, **kwargs) -> Job:
    """Update a job in the db."""
    job_json = await db.get(job_id)

    # deserialize and merge
    job = Job(**(Job.model_validate_json(job_json).model_dump() | kwargs))

    # serialize changes and save to db
    await db.set(job_id, Job.model_dump_json(job), expires)

    return job


async def run_job(job: Job) -> None:
    exit_task = asyncio.create_task(restart_on_timeout(job))

    try:
        await _run_job(job)
    finally:
        exit_task.cancel()


async def _run_job(job: Job) -> None:
    has_failed = False
    result = None
    worker_id = await db.db.client_id()
    start = time.time()

    SUMMARY_TIME_IN_QUEUE_METRIC.observe(start - job.created)

    log.info(f"Running job {job.id}. Queue time: {round(start - job.created, 3)} seconds")

    await update_job(job_id=job.id, start=start, status=JobStatus.RUNNING, worker_id=worker_id)

    # add to running jobs list if not already there (which may occur on multiple worker disconnects while running the same job)
    if job.id not in await db.lrange(RUNNING_JOBS_KEY, 0, -1):
        await db.rpush(RUNNING_JOBS_KEY, job.id)

    if len(job.payload.text) < summary_minimum_payload_length:
        log.info(f"Summarisation for {job.id} did not run because payload is too short: \"{job.payload.text}\"")

        result = job.payload.text
    else:
        try:
            customer_id = job.metadata.customer_id
            options = get_credentials(customer_id)
            secret = options.get('secret')
            processor = get_job_processor(customer_id)  # may have changed since job was created

            if processor == Processors.OPENAI:
                log.info(f"Forwarding inference to OpenAI for customer {customer_id}")

                # needed for backwards compatibility
                model = options.get('model') or options.get('metadata').get('model')
                result = await process_open_ai(job.payload, job.type, secret, model)
            elif processor == Processors.AZURE:
                log.info(f"Forwarding inference to Azure openai for customer {customer_id}")

                metadata = options.get('metadata')
                result = await process_azure(
                    job.payload, job.type, secret, metadata.get('endpoint'), metadata.get('deploymentName')
                )
            else:
                if customer_id:
                    log.info(f'Customer {customer_id} has no API key configured, falling back to local processing')

                result = await process(job.payload, job.type)
        except Exception as e:
            log.warning(f"Job {job.id} failed: {e}")

            has_failed = True
            result = str(e)

    updated_job = await update_job(
        expires=redis_exp_seconds if not has_failed else None,
        job_id=job.id,
        end=time.time(),
        status=JobStatus.ERROR if has_failed else JobStatus.SUCCESS,
        result=result,
    )

    if has_failed:
        await db.rpush(ERROR_JOBS_KEY, job.id)
        SUMMARY_ERROR_COUNTER.inc()

    await db.lrem(RUNNING_JOBS_KEY, 0, job.id)

    SUMMARY_DURATION_METRIC.observe(updated_job.computed_duration)
    SUMMARY_INPUT_LENGTH_METRIC.observe(len(updated_job.payload.text))

    log.info(f"Job {updated_job.id} duration: {updated_job.computed_duration} seconds")


def create_run_job_task(job: Job) -> asyncio.Task:
    global current_task
    current_task = asyncio.create_task(run_job(job))


async def maybe_run_next_job() -> None:
    if not can_run_next_job():
        return

    await restore_stale_jobs()

    next_job_id = await db.lpop(PENDING_JOBS_KEY)

    await update_summary_queue_metric()

    if next_job_id:
        log.info(f"Next job id: {next_job_id}")

        next_job = await get_job(next_job_id)
        create_run_job_task(next_job)


async def monitor_candidate_jobs() -> None:
    while True:
        try:
            await maybe_run_next_job()
            await asyncio.sleep(TIME_BETWEEN_JOBS_CHECK)
        except Exception as e:
            log.error(f"Error in job monitoring: {e}")
            await asyncio.sleep(TIME_BETWEEN_JOBS_CHECK_ON_ERROR)


async def restart_on_timeout(job: Job) -> None:
    await asyncio.sleep(job_timeout)

    log.warning(f"Job {job.id} timed out after {job_timeout} seconds, restarting...")

    restart_openai_api()


def start_monitoring_jobs() -> None:
    global background_task
    background_task = asyncio.create_task(monitor_candidate_jobs())
