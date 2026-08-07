"""Celery tasks.

Every task is idempotent, because ``acks_late`` means any of them can run twice:
inbound writes de-duplicate on the provider message id, sends on the queue row's
``dedupe_key``, and cadence steps on ``seq:<enrollment>:<step>``.

Retries are reserved for infrastructure failures. A policy refusal or a provider
rejection is a *result*, recorded on the row — retrying it would just refuse
again, and a retry storm against Meta is its own reputation problem.
"""

import logging
import uuid
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded

from app.sales import inbound as inbound_module
from app.sales import outbound as outbound_module
from app.sales.channels.base import InboundEvent
from app.worker.celery_app import celery_app
from app.worker.runtime import run_with_session

logger = logging.getLogger(__name__)


@celery_app.task(
    name="sales.handle_inbound_event",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    autoretry_for=(),
)
def handle_inbound_event(
    self: Any, tenant_id: str, event_payload: dict[str, Any]
) -> dict[str, Any]:
    """Process one webhook event: resolve, record, answer."""
    try:
        event = InboundEvent.from_dict(event_payload)
    except (KeyError, ValueError) as exc:
        # A payload we cannot even parse will never parse. Retrying is pointless.
        logger.error("Discarding unparseable inbound event: %s", exc)
        return {"handled": False, "reason": "UNPARSEABLE_EVENT"}

    tenant = uuid.UUID(tenant_id)

    try:
        return run_with_session(
            lambda session: inbound_module.handle_event(session, tenant, event)
        )
    except SoftTimeLimitExceeded:
        logger.error("Inbound handling timed out for %s", event.external_id)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Inbound handling failed for %s", event.external_id)
        # Infrastructure trouble (database, broker, provider): worth another go.
        raise self.retry(exc=exc) from exc


@celery_app.task(name="sales.drain_outbound_queue")
def drain_outbound_queue(limit: int | None = None) -> dict[str, Any]:
    """Send everything that is due, one policy check at a time."""
    from app.core.config import settings

    batch = limit or settings.outbound_batch_size

    async def _work(session: Any) -> dict[str, Any]:
        config = outbound_module.policy_config()
        by_status: dict[str, int] = {}
        for row in await outbound_module.due_outbound(session, limit=batch):
            outcome = await outbound_module.process_outbound(
                session, row, config=config
            )
            by_status[outcome] = by_status.get(outcome, 0) + 1
        return {"processed": sum(by_status.values()), "by_status": by_status}

    result = run_with_session(_work)
    if result["processed"]:
        logger.info("Outbound drain: %s", result)
    return result


@celery_app.task(name="sales.tick_sequences")
def tick_sequences(limit: int | None = None) -> dict[str, Any]:
    """Advance every cadence whose next step is due."""
    from app.core.config import settings

    batch = limit or settings.sequence_batch_size

    async def _work(session: Any) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for enrollment in await outbound_module.due_enrollments(session, limit=batch):
            outcome = await outbound_module.advance_enrollment(session, enrollment)
            by_status[outcome] = by_status.get(outcome, 0) + 1
        return {"advanced": sum(by_status.values()), "by_status": by_status}

    result = run_with_session(_work)
    if result["advanced"]:
        logger.info("Sequence tick: %s", result)
    return result


@celery_app.task(name="sales.enroll_in_sequence")
def enroll_in_sequence(
    lead_id: str, sequence_name: str, start_delay_hours: float | None = None
) -> dict[str, Any]:
    """Enrol a lead in a cadence by name. Used by triggers outside the API."""
    from app.models.sales import Lead

    async def _work(session: Any) -> dict[str, Any]:
        lead = await session.get(Lead, uuid.UUID(lead_id))
        if lead is None:
            return {"enrolled": False, "reason": "LEAD_NOT_FOUND"}
        sequence = await outbound_module.find_sequence(
            session, lead.tenant_id, sequence_name
        )
        if sequence is None:
            return {"enrolled": False, "reason": "SEQUENCE_NOT_FOUND"}
        enrollment = await outbound_module.enroll(
            session, lead, sequence, start_delay_hours=start_delay_hours
        )
        return {
            "enrolled": enrollment is not None,
            "enrollment_id": str(enrollment.id) if enrollment else None,
        }

    return run_with_session(_work)
