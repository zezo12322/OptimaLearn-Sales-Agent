"""Outbound engine: a durable queue plus cadences on top of it.

Nothing is sent directly. A caller enqueues a row; a worker picks it up, asks the
policy engine, and only then talks to a provider. That indirection buys three
things worth having: a send survives a crash, a duplicate enqueue is a no-op
thanks to ``dedupe_key``, and every refusal is written down next to the sends it
was refused alongside.

Free-form is preferred whenever the service window is open, and a template is
used only when the window is closed and Meta requires one. That ordering is not
just compliance — a template send costs money and reads like a template.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.sales import (
    Lead,
    LeadIdentity,
    OutboundMessage,
    SalesSequence,
    SequenceEnrollment,
)
from app.sales import repository
from app.sales.channels import messenger, whatsapp
from app.sales.channels.base import CHANNEL_TEXT_LIMIT, format_for_channel
from app.sales.enums import (
    Author,
    Channel,
    Direction,
    EventType,
    MessageStatus,
    OutboundKind,
    OutboundStatus,
    Segment,
    SequenceStatus,
    Stage,
)
from app.sales.policy import Decision, MessagingPolicyConfig, evaluate_outbound

logger = logging.getLogger(__name__)

#: How long to wait before retrying a send that failed on a provider error.
RETRY_BACKOFF = (timedelta(minutes=10), timedelta(hours=1), timedelta(hours=6))
MAX_SEND_ATTEMPTS = len(RETRY_BACKOFF) + 1

#: Reasons a cadence stops early. Recorded on the enrollment.
STOP_REPLIED = "LEAD_REPLIED"
STOP_GOAL_MET = "GOAL_MET"
STOP_OPTED_OUT = "OPTED_OUT"
STOP_HUMAN = "HUMAN_TAKEOVER"

#: Stages that mean a cadence has done its job.
GOAL_STAGES = frozenset(
    {Stage.QUALIFIED, Stage.DEMO_BOOKED, Stage.PROPOSAL_SENT, Stage.WON}
)


def policy_config() -> MessagingPolicyConfig:
    """Build the policy config from settings, in one place."""
    return MessagingPolicyConfig(
        service_window_hours=settings.sales_service_window_hours,
        quiet_hours_start=settings.sales_quiet_hours_start,
        quiet_hours_end=settings.sales_quiet_hours_end,
        default_timezone=settings.sales_default_timezone,
        max_per_lead_per_day=settings.sales_max_outbound_per_lead_per_day,
        max_per_lead_total=settings.sales_max_outbound_per_lead_total,
    )


async def get_identity(
    db: AsyncSession, lead: Lead, channel: Channel
) -> Optional[str]:
    """The addressable handle for a lead on a channel, if we have one."""
    result = await db.execute(
        select(LeadIdentity.external_id)
        .where(
            LeadIdentity.lead_id == lead.id,
            LeadIdentity.channel == channel.value,
        )
        .order_by(LeadIdentity.created_at.desc())
        .limit(1)
    )
    external_id = result.scalar_one_or_none()
    if external_id and not str(external_id).startswith(("comment_author:", "leadgen:")):
        return str(external_id)

    # WhatsApp can be addressed by phone number even with no prior thread — which
    # is how a lead-form submission gets its first touch.
    if channel is Channel.WHATSAPP and lead.phone_e164:
        return lead.phone_e164.lstrip("+")
    return None


async def enqueue_outbound(
    db: AsyncSession,
    lead: Lead,
    channel: Channel,
    dedupe_key: str,
    body: Optional[str] = None,
    kind: OutboundKind = OutboundKind.FREEFORM,
    template_name: Optional[str] = None,
    template_language: Optional[str] = None,
    template_params: Optional[list[str]] = None,
    scheduled_at: Optional[datetime] = None,
    sequence_enrollment_id: Optional[uuid.UUID] = None,
) -> Optional[OutboundMessage]:
    """Queue a proactive message. Returns ``None`` if it was already queued.

    ``dedupe_key`` is the whole idempotency story: a retried Celery task, a
    redelivered webhook and a double-clicked button all collapse to one send.
    """
    row = OutboundMessage(
        tenant_id=lead.tenant_id,
        lead_id=lead.id,
        channel=channel.value,
        kind=kind.value,
        template_name=template_name,
        template_language=template_language,
        template_params=template_params,
        body=body,
        scheduled_at=scheduled_at or datetime.now(timezone.utc),
        status=OutboundStatus.PENDING.value,
        dedupe_key=dedupe_key,
        sequence_enrollment_id=sequence_enrollment_id,
    )
    db.add(row)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        db.expunge(row)
        logger.info("Outbound %s already queued", dedupe_key)
        return None

    await repository.log_event(
        db,
        lead,
        EventType.OUTBOUND_QUEUED,
        {
            "channel": channel.value,
            "kind": kind.value,
            "template": template_name,
            "scheduled_at": row.scheduled_at.isoformat()
            if row.scheduled_at
            else None,
        },
        actor="system",
    )
    return row


async def due_outbound(db: AsyncSession, limit: int = 50) -> list[OutboundMessage]:
    """Pending rows whose time has come, oldest first."""
    result = await db.execute(
        select(OutboundMessage)
        .where(
            OutboundMessage.status == OutboundStatus.PENDING.value,
            OutboundMessage.scheduled_at <= datetime.now(timezone.utc),
        )
        .order_by(OutboundMessage.scheduled_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def _send(
    row: OutboundMessage, channel: Channel, recipient: str, use_template: bool
) -> Optional[str]:
    """Hand the message to the provider. Returns the provider message id."""
    if use_template:
        if channel is not Channel.WHATSAPP or not row.template_name:
            raise ValueError("template send requested but not possible")
        return await whatsapp.send_template(
            recipient,
            row.template_name,
            language=row.template_language,
            body_params=list(row.template_params or []),
        )

    text = row.body or ""
    if channel is Channel.WHATSAPP:
        ids = await whatsapp.send_text(recipient, text)
    elif channel is Channel.MESSENGER:
        ids = await messenger.send_text(recipient, text)
    else:
        raise ValueError(f"channel {channel.value} has no outbound sender")
    return ids[0] if ids else None


async def process_outbound(
    db: AsyncSession, row: OutboundMessage, config: Optional[MessagingPolicyConfig] = None
) -> str:
    """Evaluate and attempt one queued message. Returns the resulting status."""
    config = config or policy_config()

    lead = await db.get(Lead, row.lead_id)
    if lead is None:
        row.status = OutboundStatus.CANCELLED.value
        row.skip_reason = "LEAD_MISSING"
        return row.status

    try:
        channel = Channel(row.channel)
    except ValueError:
        row.status = OutboundStatus.SKIPPED.value
        row.skip_reason = "UNKNOWN_CHANNEL"
        return row.status

    if not settings.sales_agent_enabled:
        row.status = OutboundStatus.SKIPPED.value
        row.skip_reason = "AGENT_DISABLED"
        return row.status

    state = await repository.messaging_state(db, lead, channel, config)
    verdict = evaluate_outbound(state, config)

    if verdict.decision is Decision.DENY:
        row.status = OutboundStatus.SKIPPED.value
        row.skip_reason = verdict.reason
        await repository.log_event(
            db,
            lead,
            EventType.OUTBOUND_SKIPPED,
            {"reason": verdict.reason, "detail": verdict.detail},
            actor="system",
        )
        return row.status

    if verdict.decision is Decision.DEFER:
        # Rescheduled, not logged: a nightly quiet-hours deferral is normal
        # operation and would otherwise bury the lead timeline in noise.
        row.scheduled_at = verdict.retry_at or (
            datetime.now(timezone.utc) + timedelta(hours=1)
        )
        row.skip_reason = verdict.reason
        return row.status

    recipient = await get_identity(db, lead, channel)
    if not recipient:
        row.status = OutboundStatus.SKIPPED.value
        row.skip_reason = "NO_RECIPIENT_HANDLE"
        return row.status

    use_template = verdict.requires_template
    if use_template and not (channel is Channel.WHATSAPP and row.template_name):
        row.status = OutboundStatus.SKIPPED.value
        row.skip_reason = "TEMPLATE_REQUIRED_BUT_MISSING"
        await repository.log_event(
            db,
            lead,
            EventType.OUTBOUND_SKIPPED,
            {"reason": row.skip_reason, "detail": verdict.detail},
            actor="system",
        )
        return row.status
    if not use_template and not (row.body or "").strip():
        row.status = OutboundStatus.SKIPPED.value
        row.skip_reason = "EMPTY_BODY"
        return row.status

    row.attempt_count += 1
    try:
        provider_message_id = await _send(row, channel, recipient, use_template)
    except Exception as exc:  # noqa: BLE001 - provider errors are expected
        logger.warning(
            "Outbound %s attempt %s failed: %s", row.id, row.attempt_count, exc
        )
        row.error_message = str(exc)[:1000]
        if row.attempt_count >= MAX_SEND_ATTEMPTS:
            row.status = OutboundStatus.FAILED.value
            await repository.log_event(
                db,
                lead,
                EventType.OUTBOUND_FAILED,
                {"error": row.error_message, "attempts": row.attempt_count},
                actor="system",
            )
        else:
            backoff = RETRY_BACKOFF[min(row.attempt_count - 1, len(RETRY_BACKOFF) - 1)]
            row.scheduled_at = datetime.now(timezone.utc) + backoff
        return row.status

    row.status = OutboundStatus.SENT.value
    row.provider_message_id = provider_message_id
    row.sent_at = datetime.now(timezone.utc)
    row.skip_reason = None
    row.error_message = None

    conversation = await repository.get_or_create_conversation(db, lead, channel)
    body_for_log = row.body or f"[template:{row.template_name}]"
    await repository.record_message(
        db,
        conversation,
        lead,
        direction=Direction.OUTBOUND,
        author=Author.AGENT,
        text=format_for_channel(body_for_log, channel)[
            : CHANNEL_TEXT_LIMIT.get(channel, 2000)
        ],
        status=MessageStatus.SENT,
        provider_message_id=provider_message_id,
        template_name=row.template_name,
    )
    await repository.log_event(
        db,
        lead,
        EventType.OUTBOUND_SENT,
        {
            "channel": channel.value,
            "kind": OutboundKind.TEMPLATE.value
            if use_template
            else OutboundKind.FREEFORM.value,
            "template": row.template_name if use_template else None,
        },
        actor="system",
    )
    return row.status


# ----------------------------------------------------------------------
# Cadences
# ----------------------------------------------------------------------


async def find_sequence(
    db: AsyncSession, tenant_id: uuid.UUID, name: str
) -> Optional[SalesSequence]:
    result = await db.execute(
        select(SalesSequence)
        .where(
            SalesSequence.tenant_id == tenant_id,
            SalesSequence.name == name,
            SalesSequence.is_active.is_(True),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def enroll(
    db: AsyncSession,
    lead: Lead,
    sequence: SalesSequence,
    start_delay_hours: Optional[float] = None,
) -> Optional[SequenceEnrollment]:
    """Enrol a lead in a cadence, if eligible and not already enrolled.

    Eligibility is checked here rather than at send time as well, so an
    ineligible lead never even appears in a cadence report.
    """
    if lead.opt_out_at is not None or lead.human_takeover:
        return None
    if not sequence.steps:
        return None

    try:
        stage = Stage(lead.stage)
    except ValueError:
        stage = Stage.NEW
    if stage in GOAL_STAGES:
        return None

    first_delay = (
        start_delay_hours
        if start_delay_hours is not None
        else float(sequence.steps[0].get("delay_hours", 0) or 0)
    )

    enrollment = SequenceEnrollment(
        tenant_id=lead.tenant_id,
        lead_id=lead.id,
        sequence_id=sequence.id,
        current_step=0,
        status=SequenceStatus.ACTIVE.value,
        next_run_at=repository.next_run_after(first_delay),
    )
    db.add(enrollment)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        db.expunge(enrollment)
        return None

    await repository.log_event(
        db,
        lead,
        EventType.SEQUENCE_ENROLLED,
        {"sequence": sequence.name, "steps": len(sequence.steps)},
        actor="system",
    )
    return enrollment


async def due_enrollments(
    db: AsyncSession, limit: int = 100
) -> list[SequenceEnrollment]:
    result = await db.execute(
        select(SequenceEnrollment)
        .where(
            SequenceEnrollment.status == SequenceStatus.ACTIVE.value,
            SequenceEnrollment.next_run_at.isnot(None),
            SequenceEnrollment.next_run_at <= datetime.now(timezone.utc),
        )
        .order_by(SequenceEnrollment.next_run_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


def _render(template: str, lead: Lead) -> str:
    """Fill the small set of placeholders a cadence step may use.

    Missing values become a neutral word rather than an empty gap, so a step
    never sends "اهلا  " with a hole where a name should be.
    """
    first_name = (lead.full_name or "").split(" ")[0] if lead.full_name else ""
    replacements = {
        "{{name}}": first_name or ("حضرتك" if (lead.locale or "ar") == "ar" else "there"),
        "{{company}}": lead.company_name or "",
        "{{product}}": settings.sales_company_name,
        "{{agent}}": settings.sales_agent_display_name,
    }
    rendered = template
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    return " ".join(rendered.split())


async def advance_enrollment(
    db: AsyncSession, enrollment: SequenceEnrollment
) -> str:
    """Queue the current step and point the enrollment at the next one."""
    lead = await db.get(Lead, enrollment.lead_id)
    sequence = await db.get(SalesSequence, enrollment.sequence_id)
    if lead is None or sequence is None:
        enrollment.status = SequenceStatus.STOPPED.value
        enrollment.stop_reason = "MISSING_LEAD_OR_SEQUENCE"
        enrollment.next_run_at = None
        return enrollment.status

    if lead.opt_out_at is not None:
        enrollment.status = SequenceStatus.STOPPED.value
        enrollment.stop_reason = STOP_OPTED_OUT
        enrollment.next_run_at = None
        return enrollment.status
    if lead.human_takeover:
        enrollment.status = SequenceStatus.STOPPED.value
        enrollment.stop_reason = STOP_HUMAN
        enrollment.next_run_at = None
        return enrollment.status

    try:
        stage = Stage(lead.stage)
    except ValueError:
        stage = Stage.NEW
    if stage in GOAL_STAGES:
        enrollment.status = SequenceStatus.COMPLETED.value
        enrollment.stop_reason = STOP_GOAL_MET
        enrollment.next_run_at = None
        return enrollment.status

    steps: list[dict[str, Any]] = list(sequence.steps or [])
    if enrollment.current_step >= len(steps):
        enrollment.status = SequenceStatus.COMPLETED.value
        enrollment.next_run_at = None
        return enrollment.status

    step = steps[enrollment.current_step] or {}
    try:
        channel = Channel(str(step.get("channel") or Channel.WHATSAPP.value))
    except ValueError:
        channel = Channel.WHATSAPP

    body = step.get("body")
    # A step carries free-form copy; the approved re-engagement template is the
    # fallback the sender reaches for when the window has closed. Cadences
    # therefore work inside the window immediately, and outside it as soon as the
    # team has a template approved and configured — with no seed data to edit.
    template_name = step.get("template_name") or settings.sales_reengage_template_name
    template_params = step.get("template_params") or ["{{name}}", "{{product}}"]

    await enqueue_outbound(
        db,
        lead,
        channel,
        dedupe_key=f"seq:{enrollment.id}:{enrollment.current_step}",
        body=_render(str(body), lead) if body else None,
        kind=OutboundKind.TEMPLATE if template_name else OutboundKind.FREEFORM,
        template_name=template_name,
        template_language=step.get("template_language")
        or (lead.locale or settings.whatsapp_template_language),
        template_params=[_render(str(param), lead) for param in template_params],
        sequence_enrollment_id=enrollment.id,
    )

    enrollment.current_step += 1
    if enrollment.current_step >= len(steps):
        enrollment.status = SequenceStatus.COMPLETED.value
        enrollment.next_run_at = None
    else:
        next_delay = float(steps[enrollment.current_step].get("delay_hours", 24) or 24)
        enrollment.next_run_at = repository.next_run_after(next_delay)
    return enrollment.status


async def stop_sequences_on_reply(db: AsyncSession, lead: Lead) -> int:
    """A reply means the cadence worked; stop nudging.

    Called from the inbound path. Without it, a prospect who answers step 1
    keeps getting steps 2 and 3, which is the fastest way to look like a robot.
    """
    return await repository.stop_active_sequences(db, lead, reason=STOP_REPLIED)


def default_sequence_for(lead: Lead) -> str:
    """Which seeded cadence fits this lead."""
    if lead.segment == Segment.B2B.value:
        return "b2b-followup"
    return "b2c-followup"
