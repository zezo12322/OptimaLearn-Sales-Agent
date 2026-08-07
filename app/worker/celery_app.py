"""Celery application and beat schedule.

Two periodic jobs keep the outbound side alive:

* ``sales.drain_outbound_queue`` — sends what is due. Runs often, because a
  deferred message (quiet hours, daily cap) becomes sendable at an arbitrary
  moment and a follow-up that lands three hours late has lost its point.
* ``sales.tick_sequences`` — advances cadences. Runs less often; cadence delays
  are measured in hours, so minute-level precision buys nothing.

``acks_late`` with ``reject_on_worker_lost`` is deliberate: a task that dies
mid-flight is retried. Every send is idempotent through ``dedupe_key`` and every
inbound write through ``provider_message_id``, so at-least-once delivery is safe
here and losing a customer's message is not.
"""

from celery import Celery
from celery.schedules import schedule

from app.core.config import settings

celery_app = Celery(
    "optimalearn_sales_agent",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_max_tasks_per_child=200,
    # A model call plus a provider round-trip; generous but not unbounded.
    task_soft_time_limit=120,
    task_time_limit=180,
    beat_schedule={
        "drain-outbound-queue": {
            "task": "sales.drain_outbound_queue",
            "schedule": schedule(run_every=settings.outbound_tick_seconds),
        },
        "tick-sequences": {
            "task": "sales.tick_sequences",
            "schedule": schedule(run_every=settings.sequence_tick_seconds),
        },
    },
)

celery_app.autodiscover_tasks(["app.worker"])
