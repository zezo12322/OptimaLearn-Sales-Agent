"""Persistence helpers for leads, conversations and their timeline.

Two things here are worth more than they look.

**Identity resolution.** A prospect is one person even when they appear as a
Messenger PSID today and a WhatsApp number tomorrow. ``resolve_lead`` links a
new channel handle onto an existing lead whenever the phone or e-mail matches,
so the agent picks up the thread instead of starting over — and so nobody gets
counted twice in the pipeline.

**Idempotency.** Meta redelivers webhooks. Every inbound write goes through the
partial unique index on ``provider_message_id`` inside a savepoint, so a replay
is a cheap no-op rather than a duplicate message and a second reply.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sales import (
    Lead,
    LeadEvent,
    LeadIdentity,
    OutboundMessage,
    SalesConversation,
    SalesMessage,
)
from app.sales.enums import (
    Author,
    Channel,
    ConversationStatus,
    Direction,
    EventType,
    MessageStatus,
    OutboundStatus,
    Segment,
    Stage,
)
from app.sales.policy import LeadMessagingState, MessagingPolicyConfig
from app.sales.qualification import derive_stage, score_lead, signals_from_lead

logger = logging.getLogger(__name__)


async def log_event(
    db: AsyncSession,
    lead: Lead,
    event_type: EventType,
    payload: Optional[dict[str, Any]] = None,
    actor: str = "agent",
) -> LeadEvent:
    event = LeadEvent(
        tenant_id=lead.tenant_id,
        lead_id=lead.id,
        event_type=event_type.value,
        actor=actor,
        payload=payload,
    )
    db.add(event)
    return event


async def find_lead_by_identity(
    db: AsyncSession, tenant_id: uuid.UUID, channel: Channel, external_id: str
) -> Optional[Lead]:
    result = await db.execute(
        select(Lead)
        .join(LeadIdentity, LeadIdentity.lead_id == Lead.id)
        .where(
            LeadIdentity.tenant_id == tenant_id,
            LeadIdentity.channel == channel.value,
            LeadIdentity.external_id == external_id,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _find_lead_by_contact(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    phone: Optional[str],
    email: Optional[str],
) -> Optional[Lead]:
    for column, value in ((Lead.phone_e164, phone), (Lead.email, email)):
        if not value:
            continue
        result = await db.execute(
            select(Lead).where(Lead.tenant_id == tenant_id, column == value).limit(1)
        )
        lead = result.scalar_one_or_none()
        if lead:
            return lead
    return None


async def resolve_lead(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    channel: Channel,
    external_id: str,
    profile_name: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    locale: Optional[str] = None,
    source_campaign: Optional[str] = None,
    source_detail: Optional[str] = None,
) -> tuple[Lead, bool]:
    """Find or create the lead behind a channel handle.

    Returns ``(lead, created)``. Safe to call concurrently: the unique index on
    ``(tenant, channel, external_id)`` is the arbiter, and a losing racer simply
    re-reads the row the winner inserted.
    """
    lead = await find_lead_by_identity(db, tenant_id, channel, external_id)
    if lead is not None:
        return lead, False

    existing = await _find_lead_by_contact(db, tenant_id, phone, email)
    if existing is not None:
        await attach_identity(
            db, existing, channel, external_id, profile_name, tenant_id
        )
        await log_event(
            db,
            existing,
            EventType.IDENTITY_LINKED,
            {"channel": channel.value, "external_id": external_id},
            actor="system",
        )
        return existing, False

    lead = Lead(
        tenant_id=tenant_id,
        full_name=profile_name,
        phone_e164=phone,
        email=email,
        locale=locale,
        segment=Segment.UNKNOWN.value,
        stage=Stage.NEW.value,
        qualification={},
        source_channel=channel.value,
        source_campaign=source_campaign,
        source_detail=source_detail,
    )
    db.add(lead)

    try:
        async with db.begin_nested():
            await db.flush()
            db.add(
                LeadIdentity(
                    tenant_id=tenant_id,
                    lead_id=lead.id,
                    channel=channel.value,
                    external_id=external_id,
                    extra={"profile_name": profile_name} if profile_name else None,
                )
            )
            await db.flush()
    except IntegrityError:
        # Another worker created this lead (or the phone/e-mail) first.
        db.expunge(lead)
        winner = await find_lead_by_identity(db, tenant_id, channel, external_id)
        if winner is None:
            winner = await _find_lead_by_contact(db, tenant_id, phone, email)
        if winner is None:
            raise
        return winner, False

    await log_event(
        db,
        lead,
        EventType.LEAD_CREATED,
        {
            "channel": channel.value,
            "campaign": source_campaign,
            "detail": source_detail,
        },
        actor="system",
    )
    return lead, True


async def attach_identity(
    db: AsyncSession,
    lead: Lead,
    channel: Channel,
    external_id: str,
    profile_name: Optional[str],
    tenant_id: uuid.UUID,
) -> None:
    try:
        async with db.begin_nested():
            db.add(
                LeadIdentity(
                    tenant_id=tenant_id,
                    lead_id=lead.id,
                    channel=channel.value,
                    external_id=external_id,
                    extra={"profile_name": profile_name} if profile_name else None,
                )
            )
            await db.flush()
    except IntegrityError:
        logger.debug("Identity %s/%s already linked", channel.value, external_id)


async def get_or_create_conversation(
    db: AsyncSession, lead: Lead, channel: Channel
) -> SalesConversation:
    result = await db.execute(
        select(SalesConversation)
        .where(
            SalesConversation.tenant_id == lead.tenant_id,
            SalesConversation.lead_id == lead.id,
            SalesConversation.channel == channel.value,
        )
        .limit(1)
    )
    conversation = result.scalar_one_or_none()
    if conversation is not None:
        return conversation

    conversation = SalesConversation(
        tenant_id=lead.tenant_id,
        lead_id=lead.id,
        channel=channel.value,
        status=ConversationStatus.OPEN.value,
    )
    db.add(conversation)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        db.expunge(conversation)
        result = await db.execute(
            select(SalesConversation)
            .where(
                SalesConversation.tenant_id == lead.tenant_id,
                SalesConversation.lead_id == lead.id,
                SalesConversation.channel == channel.value,
            )
            .limit(1)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            raise
        return existing
    return conversation


async def record_message(
    db: AsyncSession,
    conversation: SalesConversation,
    lead: Lead,
    direction: Direction,
    author: Author,
    text: str,
    status: MessageStatus,
    provider_message_id: Optional[str] = None,
    template_name: Optional[str] = None,
    tool_calls: Optional[list[dict[str, Any]]] = None,
    citations: Optional[list[dict[str, Any]]] = None,
) -> Optional[SalesMessage]:
    """Append a message. Returns ``None`` when it is a provider-level duplicate."""
    message = SalesMessage(
        tenant_id=lead.tenant_id,
        conversation_id=conversation.id,
        lead_id=lead.id,
        direction=direction.value,
        channel=conversation.channel,
        author=author.value,
        text=text,
        status=status.value,
        provider_message_id=provider_message_id,
        template_name=template_name,
        tool_calls=tool_calls,
        citations=citations,
    )
    db.add(message)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        db.expunge(message)
        logger.info(
            "Duplicate provider message %s ignored", provider_message_id
        )
        return None

    now = datetime.now(timezone.utc)
    conversation.message_count += 1
    conversation.last_message_at = now
    if direction is Direction.INBOUND:
        lead.last_inbound_at = now
    else:
        lead.last_outbound_at = now
    return message


async def load_history(
    db: AsyncSession, conversation: SalesConversation, limit: int = 12
) -> list[dict[str, str]]:
    """Recent turns as OpenAI chat messages, oldest first.

    Human replies are folded in as ``assistant`` turns: from the prospect's side
    the thread is one voice, and hiding a colleague's answer would make the agent
    contradict it.
    """
    result = await db.execute(
        select(SalesMessage)
        .where(SalesMessage.conversation_id == conversation.id)
        .order_by(SalesMessage.created_at.desc(), SalesMessage.id.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    rows.reverse()

    history: list[dict[str, str]] = []
    for row in rows:
        role = "user" if row.direction == Direction.INBOUND.value else "assistant"
        history.append({"role": role, "content": row.text})
    return history


async def count_inbound_messages(db: AsyncSession, lead: Lead) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(SalesMessage)
        .where(
            SalesMessage.lead_id == lead.id,
            SalesMessage.direction == Direction.INBOUND.value,
        )
    )
    return int(result.scalar() or 0)


def _zone(name: Optional[str], fallback: str) -> ZoneInfo:
    for candidate in (name, fallback, "UTC"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


async def outbound_counts(
    db: AsyncSession, lead: Lead, default_timezone: str
) -> tuple[int, int]:
    """``(sent_today, sent_total)`` proactive messages, in the lead's local day."""
    zone = _zone(lead.timezone, default_timezone)
    local_now = datetime.now(timezone.utc).astimezone(zone)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = local_midnight.astimezone(timezone.utc)

    total_result = await db.execute(
        select(func.count())
        .select_from(OutboundMessage)
        .where(
            OutboundMessage.lead_id == lead.id,
            OutboundMessage.status == OutboundStatus.SENT.value,
        )
    )
    today_result = await db.execute(
        select(func.count())
        .select_from(OutboundMessage)
        .where(
            OutboundMessage.lead_id == lead.id,
            OutboundMessage.status == OutboundStatus.SENT.value,
            OutboundMessage.sent_at >= day_start,
        )
    )
    return int(today_result.scalar() or 0), int(total_result.scalar() or 0)


async def messaging_state(
    db: AsyncSession,
    lead: Lead,
    channel: Channel,
    config: MessagingPolicyConfig,
    include_counts: bool = True,
) -> LeadMessagingState:
    """Assemble the policy input for a lead on a channel."""
    sent_today = sent_total = 0
    if include_counts:
        sent_today, sent_total = await outbound_counts(
            db, lead, config.default_timezone
        )

    try:
        stage = Stage(lead.stage)
    except ValueError:
        stage = Stage.NEW

    return LeadMessagingState(
        channel=channel,
        stage=stage,
        opted_out=lead.opt_out_at is not None,
        marketing_opt_in=bool(lead.marketing_opt_in),
        human_takeover=bool(lead.human_takeover),
        last_inbound_at=lead.last_inbound_at,
        last_outbound_at=lead.last_outbound_at,
        opt_in_at=lead.opt_in_at,
        lead_timezone=lead.timezone,
        sent_today=sent_today,
        sent_total=sent_total,
    )


async def refresh_score_and_stage(
    db: AsyncSession, lead: Lead, inbound_count: Optional[int] = None
) -> None:
    """Recompute score and stage, logging only real changes.

    Writing an event on every recompute would bury the timeline in noise, so a
    no-op recompute stays silent.
    """
    if inbound_count is None:
        inbound_count = await count_inbound_messages(db, lead)

    signals = signals_from_lead(lead, inbound_message_count=inbound_count)
    result = score_lead(signals)

    try:
        current_stage = Stage(lead.stage)
    except ValueError:
        current_stage = Stage.NEW

    new_stage = derive_stage(current_stage, result, signals)

    if result.score != lead.score:
        previous = lead.score
        lead.score = result.score
        lead.score_breakdown = result.breakdown
        await log_event(
            db,
            lead,
            EventType.SCORE_CHANGED,
            {"from": previous, "to": result.score, "breakdown": result.breakdown},
            actor="system",
        )
    else:
        lead.score_breakdown = result.breakdown

    if new_stage is not current_stage:
        lead.stage = new_stage.value
        await log_event(
            db,
            lead,
            EventType.STAGE_CHANGED,
            {"from": current_stage.value, "to": new_stage.value},
            actor="system",
        )


async def mark_opted_out(db: AsyncSession, lead: Lead, source: str) -> None:
    """Record an opt-out. Idempotent, and never reversed by automation."""
    if lead.opt_out_at is not None:
        return
    lead.opt_out_at = datetime.now(timezone.utc)
    lead.marketing_opt_in = False
    await log_event(
        db, lead, EventType.OPTED_OUT, {"source": source}, actor="system"
    )
    await stop_active_sequences(db, lead, reason="OPTED_OUT")


async def mark_opted_in(db: AsyncSession, lead: Lead, source: str) -> None:
    """Re-subscribe a lead. Only ever called from an explicit lead request."""
    lead.opt_out_at = None
    lead.marketing_opt_in = True
    lead.opt_in_at = datetime.now(timezone.utc)
    lead.opt_in_source = source
    await log_event(db, lead, EventType.OPTED_IN, {"source": source}, actor="system")


async def stop_active_sequences(db: AsyncSession, lead: Lead, reason: str) -> int:
    """Halt every running cadence for a lead and cancel its pending sends."""
    from app.models.sales import SequenceEnrollment  # local: avoids import cycle
    from app.sales.enums import SequenceStatus

    result = await db.execute(
        select(SequenceEnrollment).where(
            SequenceEnrollment.lead_id == lead.id,
            SequenceEnrollment.status == SequenceStatus.ACTIVE.value,
        )
    )
    enrollments = list(result.scalars().all())
    for enrollment in enrollments:
        enrollment.status = SequenceStatus.STOPPED.value
        enrollment.stop_reason = reason
        enrollment.next_run_at = None

    pending = await db.execute(
        select(OutboundMessage).where(
            OutboundMessage.lead_id == lead.id,
            OutboundMessage.status == OutboundStatus.PENDING.value,
        )
    )
    for message in pending.scalars().all():
        message.status = OutboundStatus.CANCELLED.value
        message.skip_reason = reason

    if enrollments:
        await log_event(
            db,
            lead,
            EventType.SEQUENCE_STOPPED,
            {"reason": reason, "count": len(enrollments)},
            actor="system",
        )
    return len(enrollments)


def next_run_after(hours: float, now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + timedelta(hours=hours)
