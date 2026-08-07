"""Inbound orchestration: one normalised event in, one handled outcome out.

Everything the webhooks receive funnels through :func:`handle_event`, which is
deliberately the only place that knows the whole sequence: resolve the person,
record what they said, check whether we are allowed to answer, answer, and write
down what the answer did.

Replies to a live message are sent directly rather than queued. The outbound
queue exists to govern *proactive* contact — running a live answer through the
daily cap would mean politely refusing to reply to a customer who just asked a
question.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.sales import Lead, SalesMessage
from app.sales import agent as agent_module
from app.sales import outbound as outbound_module
from app.sales import repository
from app.sales.channels import messenger, whatsapp
from app.sales.channels.base import (
    KIND_COMMENT,
    KIND_LEAD_FORM,
    KIND_MESSAGE,
    KIND_STATUS,
    InboundEvent,
    format_for_channel,
)
from app.sales.enums import (
    Author,
    Channel,
    Direction,
    EventType,
    MessageStatus,
    OutboundKind,
    Stage,
)
from app.sales.normalize import detect_locale
from app.sales.policy import (
    Decision,
    detect_opt_in,
    detect_opt_out,
    evaluate_private_reply,
    may_auto_reply,
)
from app.sales.prompts import OPT_IN_ACK, OPT_OUT_ACK, localized

logger = logging.getLogger(__name__)

#: Provider status strings mapped onto our message states.
_STATUS_MAP = {
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "failed": MessageStatus.FAILED,
    "delivery": MessageStatus.DELIVERED,
}


async def handle_event(
    db: AsyncSession, tenant_id: uuid.UUID, event: InboundEvent
) -> dict[str, Any]:
    """Process one inbound event. Never raises; always reports what happened."""
    if not settings.sales_agent_enabled:
        return {"handled": False, "reason": "AGENT_DISABLED"}

    if event.kind == KIND_STATUS:
        return await _handle_status(db, event)
    if event.kind == KIND_LEAD_FORM:
        return await _handle_lead_form(db, tenant_id, event)
    if event.kind == KIND_COMMENT:
        return await _handle_comment(db, tenant_id, event)
    if event.kind == KIND_MESSAGE:
        return await _handle_message(db, tenant_id, event)

    return {"handled": False, "reason": f"UNSUPPORTED_KIND:{event.kind}"}


async def _handle_status(db: AsyncSession, event: InboundEvent) -> dict[str, Any]:
    """Fold a delivery/read receipt onto the message it refers to."""
    if not event.provider_message_id:
        return {"handled": False, "reason": "NO_PROVIDER_ID"}

    status = _STATUS_MAP.get((event.text or "").lower())
    if status is None:
        return {"handled": False, "reason": f"UNKNOWN_STATUS:{event.text}"}

    result = await db.execute(
        select(SalesMessage)
        .where(SalesMessage.provider_message_id == event.provider_message_id)
        .limit(1)
    )
    message = result.scalar_one_or_none()
    if message is None:
        return {"handled": False, "reason": "MESSAGE_NOT_FOUND"}

    # Receipts can arrive out of order; never walk a message backwards from
    # READ to DELIVERED.
    ranking = {
        MessageStatus.SENT.value: 1,
        MessageStatus.DELIVERED.value: 2,
        MessageStatus.READ.value: 3,
    }
    if status is MessageStatus.FAILED or ranking.get(status.value, 0) > ranking.get(
        message.status, 0
    ):
        message.status = status.value
    return {"handled": True, "status": message.status}


async def _prepare_lead(
    db: AsyncSession, tenant_id: uuid.UUID, event: InboundEvent
) -> tuple[Lead, bool]:
    locale = detect_locale(event.text) if event.text else None
    lead, created = await repository.resolve_lead(
        db,
        tenant_id=tenant_id,
        channel=event.channel,
        external_id=event.external_id,
        profile_name=event.profile_name,
        phone=event.phone,
        email=event.email,
        locale=locale,
        source_campaign=event.source_campaign,
        source_detail=event.source_detail,
    )

    # Language is set from the first message that has letters in it and then left
    # alone: a prospect who drops one English word mid-thread has not switched
    # languages, and flip-flopping replies read as broken.
    if locale and not lead.locale:
        lead.locale = locale
    # Attribution is first-touch: the ad that started the conversation is the one
    # that earned it, so a later referral never overwrites it.
    if event.source_campaign and not lead.source_campaign:
        lead.source_campaign = event.source_campaign
    if event.source_detail and not lead.source_detail:
        lead.source_detail = event.source_detail
    return lead, created


async def _handle_message(
    db: AsyncSession, tenant_id: uuid.UUID, event: InboundEvent
) -> dict[str, Any]:
    lead, _created = await _prepare_lead(db, tenant_id, event)
    conversation = await repository.get_or_create_conversation(
        db, lead, event.channel
    )

    recorded = await repository.record_message(
        db,
        conversation,
        lead,
        direction=Direction.INBOUND,
        author=Author.LEAD,
        text=event.text or "[empty]",
        status=MessageStatus.RECEIVED,
        provider_message_id=event.provider_message_id,
    )
    if recorded is None:
        return {"handled": True, "duplicate": True, "lead_id": str(lead.id)}

    await repository.log_event(
        db,
        lead,
        EventType.INBOUND_RECEIVED,
        {"channel": event.channel.value, "chars": len(event.text or "")},
        actor="lead",
    )

    # A reply is the outcome a cadence was chasing; stop nudging immediately.
    await outbound_module.stop_sequences_on_reply(db, lead)

    if detect_opt_out(event.text or ""):
        await repository.mark_opted_out(db, lead, source=f"keyword:{event.channel.value}")
        ack = localized(OPT_OUT_ACK, lead.locale)
        await _send_and_record(db, lead, conversation, event.channel, ack)
        return {"handled": True, "lead_id": str(lead.id), "action": "OPT_OUT"}

    if detect_opt_in(event.text or "") and lead.opt_out_at is not None:
        await repository.mark_opted_in(db, lead, source=f"keyword:{event.channel.value}")
        ack = localized(OPT_IN_ACK, lead.locale)
        await _send_and_record(db, lead, conversation, event.channel, ack)
        return {"handled": True, "lead_id": str(lead.id), "action": "OPT_IN"}

    state = await repository.messaging_state(
        db, lead, event.channel, outbound_module.policy_config(), include_counts=False
    )
    verdict = may_auto_reply(state, agent_enabled=settings.sales_agent_enabled)
    if verdict.decision is Decision.DENY:
        # Recorded but unanswered — a human sees it in the CRM inbox.
        await repository.refresh_score_and_stage(db, lead)
        return {
            "handled": True,
            "lead_id": str(lead.id),
            "action": "NO_AUTO_REPLY",
            "reason": verdict.reason,
        }

    inbound_count = await repository.count_inbound_messages(db, lead)
    history = await repository.load_history(
        db, conversation, limit=settings.sales_history_messages
    )
    # ``history`` already ends with the message we just recorded; hand the model
    # the earlier turns plus the current one explicitly.
    prior_history = history[:-1] if history else []

    reply = await agent_module.generate_reply(
        db=db,
        lead=lead,
        conversation=conversation,
        channel=event.channel,
        message=event.text or "",
        history=prior_history,
        inbound_count=inbound_count,
    )

    await _apply_reply_side_effects(db, lead, reply)
    await repository.refresh_score_and_stage(db, lead, inbound_count=inbound_count)

    sent = await _send_and_record(
        db,
        lead,
        conversation,
        event.channel,
        reply.text,
        tool_calls=reply.tool_calls or None,
        citations=reply.citations or None,
    )

    return {
        "handled": True,
        "lead_id": str(lead.id),
        "action": "REPLIED" if sent else "REPLY_SEND_FAILED",
        "stage": lead.stage,
        "score": lead.score,
        "handoff": reply.handoff,
        "agent_ok": reply.ok,
        # Returned so the admin preview can show the answer without a provider,
        # and so a failed send still leaves the text somewhere useful.
        "reply": reply.text,
        "citations": reply.citations,
        "tool_calls": reply.tool_calls,
    }


async def _handle_comment(
    db: AsyncSession, tenant_id: uuid.UUID, event: InboundEvent
) -> dict[str, Any]:
    """Answer a public comment with the one private reply Meta allows."""
    if not event.reply_to:
        return {"handled": False, "reason": "NO_COMMENT_ID"}

    lead, _created = await _prepare_lead(db, tenant_id, event)
    conversation = await repository.get_or_create_conversation(
        db, lead, Channel.MESSENGER
    )

    recorded = await repository.record_message(
        db,
        conversation,
        lead,
        direction=Direction.INBOUND,
        author=Author.LEAD,
        text=event.text or "[comment]",
        status=MessageStatus.RECEIVED,
        provider_message_id=event.provider_message_id,
    )
    if recorded is None:
        return {"handled": True, "duplicate": True, "lead_id": str(lead.id)}

    state = await repository.messaging_state(
        db,
        lead,
        Channel.MESSENGER,
        outbound_module.policy_config(),
        include_counts=False,
    )
    verdict = evaluate_private_reply(state, agent_enabled=settings.sales_agent_enabled)
    if verdict.decision is Decision.DENY:
        return {
            "handled": True,
            "lead_id": str(lead.id),
            "action": "NO_PRIVATE_REPLY",
            "reason": verdict.reason,
        }

    inbound_count = await repository.count_inbound_messages(db, lead)
    reply = await agent_module.generate_reply(
        db=db,
        lead=lead,
        conversation=conversation,
        channel=Channel.MESSENGER,
        message=event.text or "",
        history=[],
        inbound_count=inbound_count,
    )
    await _apply_reply_side_effects(db, lead, reply)
    await repository.refresh_score_and_stage(db, lead, inbound_count=inbound_count)

    try:
        message_id, psid = await messenger.send_private_reply(
            event.reply_to, reply.text
        )
    except messenger.MessengerError as exc:
        logger.warning("Private reply to comment %s failed: %s", event.reply_to, exc)
        await repository.record_message(
            db,
            conversation,
            lead,
            direction=Direction.OUTBOUND,
            author=Author.AGENT,
            text=reply.text,
            status=MessageStatus.FAILED,
        )
        return {
            "handled": True,
            "lead_id": str(lead.id),
            "action": "PRIVATE_REPLY_FAILED",
            "error": str(exc),
        }

    if psid:
        # The payoff: the private reply mints a PSID, which is what makes this
        # person addressable from here on.
        await repository.attach_identity(
            db, lead, Channel.MESSENGER, psid, event.profile_name, lead.tenant_id
        )

    await repository.record_message(
        db,
        conversation,
        lead,
        direction=Direction.OUTBOUND,
        author=Author.AGENT,
        text=format_for_channel(reply.text, Channel.MESSENGER),
        status=MessageStatus.SENT,
        provider_message_id=message_id,
        tool_calls=reply.tool_calls or None,
        citations=reply.citations or None,
    )
    await repository.log_event(
        db,
        lead,
        EventType.AGENT_REPLIED,
        {"channel": Channel.MESSENGER.value, "via": "comment_private_reply"},
    )
    return {
        "handled": True,
        "lead_id": str(lead.id),
        "action": "PRIVATE_REPLIED",
        "psid_captured": bool(psid),
    }


async def _handle_lead_form(
    db: AsyncSession, tenant_id: uuid.UUID, event: InboundEvent
) -> dict[str, Any]:
    """Turn a submitted Facebook lead form into a lead and a first touch."""
    leadgen_id = event.external_id.split(":", 1)[-1]
    try:
        form = await messenger.fetch_lead_form_data(leadgen_id)
    except messenger.MessengerError as exc:
        logger.warning("Lead form %s could not be fetched: %s", leadgen_id, exc)
        return {"handled": False, "reason": "LEAD_FORM_FETCH_FAILED", "error": str(exc)}

    lead, created = await repository.resolve_lead(
        db,
        tenant_id=tenant_id,
        channel=Channel.WHATSAPP if form.get("phone") else Channel.MESSENGER,
        external_id=f"leadgen:{leadgen_id}",
        profile_name=form.get("full_name"),
        phone=form.get("phone"),
        email=form.get("email"),
        source_campaign=form.get("campaign_name") or "facebook_lead_ad",
        source_detail=form.get("ad_id") or form.get("form_id"),
    )

    if form.get("company_name") and not lead.company_name:
        lead.company_name = form["company_name"]
    if form.get("job_title") and not lead.job_title:
        lead.job_title = form["job_title"]

    # Submitting a lead form with our consent language *is* an opt-in. It is the
    # one place we set this without the prospect typing "yes".
    if not lead.marketing_opt_in:
        lead.marketing_opt_in = True
        lead.opt_in_at = datetime.now(timezone.utc)
        lead.opt_in_source = f"facebook_lead_form:{form.get('form_id') or leadgen_id}"
        await repository.log_event(
            db,
            lead,
            EventType.OPTED_IN,
            {"source": lead.opt_in_source},
            actor="system",
        )

    await repository.refresh_score_and_stage(db, lead)

    queued = False
    if lead.phone_e164 and settings.sales_lead_form_template_name:
        # No inbound message exists yet, so the window is closed by definition
        # and only an approved template can open it.
        row = await outbound_module.enqueue_outbound(
            db,
            lead,
            Channel.WHATSAPP,
            dedupe_key=f"leadform:{leadgen_id}",
            kind=OutboundKind.TEMPLATE,
            template_name=settings.sales_lead_form_template_name,
            template_language=lead.locale or settings.whatsapp_template_language,
            template_params=[
                (lead.full_name or "").split(" ")[0] or "حضرتك",
                settings.sales_company_name,
            ],
            body=f"[template:{settings.sales_lead_form_template_name}]",
        )
        queued = row is not None

    return {
        "handled": True,
        "lead_id": str(lead.id),
        "created": created,
        "action": "LEAD_FORM_CAPTURED",
        "first_touch_queued": queued,
        "reason": None
        if queued
        else "No phone on the form or SALES_LEAD_FORM_TEMPLATE_NAME is unset",
    }


async def _apply_reply_side_effects(
    db: AsyncSession, lead: Lead, reply: agent_module.AgentReply
) -> None:
    """Persist the consequences of the tools the agent used this turn."""
    if reply.captured:
        await repository.log_event(
            db, lead, EventType.DETAILS_CAPTURED, {"fields": reply.captured}
        )

    if reply.booking:
        # A booked call is a real commitment on both sides, so the stage moves
        # even though scoring would otherwise still call this "qualifying".
        await repository.log_event(db, lead, EventType.DEMO_BOOKED, reply.booking)
        lead.stage = Stage.DEMO_BOOKED.value

    if reply.handoff:
        await repository.log_event(
            db, lead, EventType.HANDOFF_REQUESTED, reply.handoff
        )
        # The agent stays live: a handoff means a colleague should join, not that
        # the prospect gets silence until someone notices. ``human_takeover`` is
        # set only when a human actually writes through the CRM.


async def _send_and_record(
    db: AsyncSession,
    lead: Lead,
    conversation: Any,
    channel: Channel,
    text: str,
    tool_calls: Optional[list[dict[str, Any]]] = None,
    citations: Optional[list[dict[str, Any]]] = None,
) -> bool:
    """Send a live reply and record it. Returns whether the provider accepted it."""
    recipient = await outbound_module.get_identity(db, lead, channel)
    if not recipient:
        await repository.record_message(
            db,
            conversation,
            lead,
            direction=Direction.OUTBOUND,
            author=Author.AGENT,
            text=text,
            status=MessageStatus.FAILED,
            tool_calls=tool_calls,
            citations=citations,
        )
        logger.warning("No %s handle for lead %s", channel.value, lead.id)
        return False

    provider_message_id: Optional[str] = None
    try:
        if channel is Channel.WHATSAPP:
            ids = await whatsapp.send_text(recipient, text)
            provider_message_id = ids[0] if ids else None
        elif channel is Channel.MESSENGER:
            ids = await messenger.send_text(recipient, text)
            provider_message_id = ids[0] if ids else None
        elif channel is Channel.WEB:
            # The preview/playground caller returns the text itself.
            provider_message_id = None
        else:
            raise ValueError(f"no sender for channel {channel.value}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sending reply on %s failed: %s", channel.value, exc)
        await repository.record_message(
            db,
            conversation,
            lead,
            direction=Direction.OUTBOUND,
            author=Author.AGENT,
            text=text,
            status=MessageStatus.FAILED,
            tool_calls=tool_calls,
            citations=citations,
        )
        return False

    await repository.record_message(
        db,
        conversation,
        lead,
        direction=Direction.OUTBOUND,
        author=Author.AGENT,
        text=format_for_channel(text, channel),
        status=MessageStatus.SENT,
        provider_message_id=provider_message_id,
        tool_calls=tool_calls,
        citations=citations,
    )
    await repository.log_event(
        db, lead, EventType.AGENT_REPLIED, {"channel": channel.value}
    )
    return True
