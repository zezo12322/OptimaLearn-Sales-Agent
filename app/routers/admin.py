"""Admin / internal API.

Consumed by trusted server-side callers — a server action in the Optimatech site,
or an operator with the key. Never a browser: the CRM's *reads* come from Supabase
under RLS, so this API exists for the things a Supabase write cannot do, chiefly
sending a message.

Nothing here is reachable by an end user, which is why a shared secret is enough
— but it is still the surface that can send messages on the company's behalf, so
every write is recorded on the lead timeline with an actor.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import verify_internal_key
from app.core.config import settings
from app.core.db import get_db
from app.models.sales import (
    Lead,
    LeadEvent,
    OutboundMessage,
    SalesChunk,
    SalesDocument,
    SalesMessage,
    SalesSequence,
)
from app.sales import inbound as inbound_module
from app.sales import kb, repository, seed, upsell
from app.sales import outbound as outbound_module
from app.sales.channels.base import KIND_MESSAGE, InboundEvent
from app.sales.enums import (
    Audience,
    Author,
    Channel,
    Direction,
    DocType,
    EventType,
    MessageStatus,
    OutboundStatus,
    Stage,
)
from app.sales.normalize import normalize_email, normalize_phone
from app.sales.qualification import score_lead, signals_from_lead
from app.schemas.sales import (
    DocumentIn,
    DocumentIngestResult,
    DocumentOut,
    EnrollIn,
    EventOut,
    HumanMessageIn,
    LeadDetailOut,
    LeadListOut,
    LeadPatch,
    LeadSummary,
    MessageOut,
    OutboundOut,
    PreviewChatIn,
    PreviewChatOut,
    SeedResult,
    StatsOut,
    UpsellIn,
    UpsellOut,
    UpsellOutcomeIn,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/sales",
    tags=["sales"],
    dependencies=[Depends(verify_internal_key)],
)


def _tenant(explicit: Optional[uuid.UUID]) -> uuid.UUID:
    return explicit or settings.default_tenant_id


def _lead_summary(lead: Lead) -> LeadSummary:
    return LeadSummary(
        id=lead.id,
        full_name=lead.full_name,
        phone_e164=lead.phone_e164,
        email=lead.email,
        segment=lead.segment,
        company_name=lead.company_name,
        stage=lead.stage,
        score=lead.score,
        locale=lead.locale,
        source_channel=lead.source_channel,
        source_campaign=lead.source_campaign,
        owner_user_id=lead.owner_user_id,
        human_takeover=lead.human_takeover,
        marketing_opt_in=lead.marketing_opt_in,
        opted_out=lead.opt_out_at is not None,
        last_inbound_at=lead.last_inbound_at,
        last_outbound_at=lead.last_outbound_at,
        created_at=lead.created_at,
        updated_at=lead.updated_at,
    )


# ----------------------------------------------------------------------
# Knowledge base
# ----------------------------------------------------------------------
@router.post("/knowledge", response_model=DocumentIngestResult)
async def add_document(
    payload: DocumentIn, db: AsyncSession = Depends(get_db)
) -> DocumentIngestResult:
    """Ingest one document. Idempotent on content: same text, same document."""
    try:
        document, chunk_count, created = await kb.ingest_document(
            db,
            tenant_id=_tenant(payload.tenant_id),
            title=payload.title,
            content=payload.content,
            doc_type=payload.doc_type,
            audience=payload.audience,
            locale=payload.locale,
            source_uri=payload.source_uri,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": {"code": "INVALID_DOCUMENT", "message": str(exc)}},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - embedding/provider failure
        logger.exception("Document ingestion failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": {"code": "INGEST_FAILED", "message": str(exc)}},
        ) from exc

    await db.commit()
    return DocumentIngestResult(
        document=DocumentOut(
            id=document.id,
            title=document.title,
            doc_type=document.doc_type,
            audience=document.audience,
            locale=document.locale,
            is_active=document.is_active,
            chunk_count=chunk_count,
            created_at=document.created_at,
            updated_at=document.updated_at,
        ),
        chunk_count=chunk_count,
        created=created,
    )


@router.get("/knowledge", response_model=list[DocumentOut])
async def list_documents(
    tenant_id: Optional[uuid.UUID] = None,
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
) -> list[DocumentOut]:
    chunk_counts = (
        select(SalesChunk.document_id, func.count().label("chunks"))
        .group_by(SalesChunk.document_id)
        .subquery()
    )
    query = (
        select(SalesDocument, chunk_counts.c.chunks)
        .outerjoin(chunk_counts, chunk_counts.c.document_id == SalesDocument.id)
        .where(SalesDocument.tenant_id == _tenant(tenant_id))
        .order_by(SalesDocument.updated_at.desc())
    )
    if not include_inactive:
        query = query.where(SalesDocument.is_active.is_(True))

    rows = (await db.execute(query)).all()
    return [
        DocumentOut(
            id=document.id,
            title=document.title,
            doc_type=document.doc_type,
            audience=document.audience,
            locale=document.locale,
            is_active=document.is_active,
            chunk_count=int(chunks or 0),
            created_at=document.created_at,
            updated_at=document.updated_at,
        )
        for document, chunks in rows
    ]


@router.delete("/knowledge/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def retire_document(
    document_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> None:
    """Retire a document from retrieval. Kept on disk for auditing."""
    if not await kb.deactivate_document(db, document_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "NOT_FOUND", "message": "No such document."}},
        )
    await db.commit()


@router.post("/knowledge/seed", response_model=SeedResult)
async def seed_knowledge(
    tenant_id: Optional[uuid.UUID] = None, db: AsyncSession = Depends(get_db)
) -> SeedResult:
    """Install the starter knowledge base and cadences.

    Safe to call repeatedly: documents de-duplicate on content hash and
    sequences on name, so this converges rather than accumulating.
    """
    tenant = _tenant(tenant_id)
    created = existing = 0
    for entry in seed.STARTER_DOCUMENTS:
        try:
            _document, _chunks, was_created = await kb.ingest_document(
                db,
                tenant_id=tenant,
                title=entry["title"],
                content=entry["content"],
                doc_type=DocType(entry["doc_type"]),
                audience=Audience(entry["audience"]),
                locale=entry["locale"],
                source_uri="seed",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Seeding document %s failed", entry["title"])
            continue
        created += int(was_created)
        existing += int(not was_created)

    seq_created = seq_existing = 0
    for entry in seed.STARTER_SEQUENCES:
        found = await db.execute(
            select(SalesSequence).where(
                SalesSequence.tenant_id == tenant,
                SalesSequence.name == entry["name"],
            )
        )
        if found.scalar_one_or_none() is not None:
            seq_existing += 1
            continue
        db.add(
            SalesSequence(
                tenant_id=tenant,
                name=entry["name"],
                description=entry.get("description"),
                audience=entry["audience"],
                steps=entry["steps"],
                is_active=True,
            )
        )
        seq_created += 1

    await db.commit()
    return SeedResult(
        documents_created=created,
        documents_existing=existing,
        sequences_created=seq_created,
        sequences_existing=seq_existing,
    )


# ----------------------------------------------------------------------
# Leads
# ----------------------------------------------------------------------
@router.get("/leads", response_model=LeadListOut)
async def list_leads(
    tenant_id: Optional[uuid.UUID] = None,
    stage: Optional[Stage] = None,
    channel: Optional[Channel] = None,
    min_score: Optional[int] = Query(default=None, ge=0, le=100),
    owner_user_id: Optional[str] = None,
    awaiting_human: Optional[bool] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> LeadListOut:
    filters = [Lead.tenant_id == _tenant(tenant_id)]
    if stage is not None:
        filters.append(Lead.stage == stage.value)
    if channel is not None:
        filters.append(Lead.source_channel == channel.value)
    if min_score is not None:
        filters.append(Lead.score >= min_score)
    if owner_user_id:
        filters.append(Lead.owner_user_id == owner_user_id)
    if awaiting_human is not None:
        filters.append(Lead.human_takeover.is_(awaiting_human))
    if q:
        pattern = f"%{q.strip()}%"
        filters.append(
            Lead.full_name.ilike(pattern)
            | Lead.phone_e164.ilike(pattern)
            | Lead.email.ilike(pattern)
            | Lead.company_name.ilike(pattern)
        )

    total = int(
        await db.scalar(select(func.count()).select_from(Lead).where(*filters)) or 0
    )
    rows = await db.execute(
        select(Lead)
        .where(*filters)
        # Hottest first: score decides, recency breaks the tie.
        .order_by(Lead.score.desc(), Lead.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return LeadListOut(
        items=[_lead_summary(lead) for lead in rows.scalars().all()],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/leads/{lead_id}", response_model=LeadDetailOut)
async def get_lead(
    lead_id: uuid.UUID,
    message_limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> LeadDetailOut:
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "NOT_FOUND", "message": "No such lead."}},
        )

    messages = (
        await db.execute(
            select(SalesMessage)
            .where(SalesMessage.lead_id == lead_id)
            .order_by(SalesMessage.created_at.asc())
            .limit(message_limit)
        )
    ).scalars().all()

    events = (
        await db.execute(
            select(LeadEvent)
            .where(LeadEvent.lead_id == lead_id)
            .order_by(LeadEvent.created_at.desc())
            .limit(200)
        )
    ).scalars().all()

    outbound_rows = (
        await db.execute(
            select(OutboundMessage)
            .where(OutboundMessage.lead_id == lead_id)
            .order_by(OutboundMessage.created_at.desc())
            .limit(50)
        )
    ).scalars().all()

    inbound_count = await repository.count_inbound_messages(db, lead)
    score = score_lead(signals_from_lead(lead, inbound_message_count=inbound_count))

    return LeadDetailOut(
        lead=_lead_summary(lead),
        qualification=dict(lead.qualification or {}),
        score_breakdown=lead.score_breakdown,
        missing_fields=score.missing_fields,
        messages=[
            MessageOut(
                id=m.id,
                direction=m.direction,
                channel=m.channel,
                author=m.author,
                text=m.text,
                status=m.status,
                template_name=m.template_name,
                citations=m.citations,
                tool_calls=m.tool_calls,
                created_at=m.created_at,
            )
            for m in messages
        ],
        events=[
            EventOut(
                id=e.id,
                event_type=e.event_type,
                actor=e.actor,
                payload=e.payload,
                created_at=e.created_at,
            )
            for e in events
        ],
        outbound=[
            OutboundOut(
                id=o.id,
                channel=o.channel,
                kind=o.kind,
                template_name=o.template_name,
                body=o.body,
                status=o.status,
                skip_reason=o.skip_reason,
                scheduled_at=o.scheduled_at,
                sent_at=o.sent_at,
                attempt_count=o.attempt_count,
            )
            for o in outbound_rows
        ],
        notes=lead.notes,
    )


@router.patch("/leads/{lead_id}", response_model=LeadSummary)
async def patch_lead(
    lead_id: uuid.UUID, payload: LeadPatch, db: AsyncSession = Depends(get_db)
) -> LeadSummary:
    """Apply a human's edits. Score stays derived; opt-out is one-way."""
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "NOT_FOUND", "message": "No such lead."}},
        )

    actor = payload.actor or "human"
    changes: dict[str, Any] = {}

    if payload.stage is not None and payload.stage.value != lead.stage:
        changes["stage"] = {"from": lead.stage, "to": payload.stage.value}
        lead.stage = payload.stage.value
    if payload.segment is not None and payload.segment.value != lead.segment:
        changes["segment"] = {"from": lead.segment, "to": payload.segment.value}
        lead.segment = payload.segment.value
    if payload.owner_user_id is not None:
        lead.owner_user_id = payload.owner_user_id or None
        lead.assigned_at = datetime.now(timezone.utc) if payload.owner_user_id else None
        changes["owner_user_id"] = payload.owner_user_id
    if payload.notes is not None:
        lead.notes = payload.notes
        changes["notes"] = True
    if payload.full_name is not None:
        lead.full_name = payload.full_name or None
        changes["full_name"] = lead.full_name
    if payload.company_name is not None:
        lead.company_name = payload.company_name or None
        changes["company_name"] = lead.company_name
    if payload.company_size is not None:
        lead.company_size = payload.company_size
        changes["company_size"] = payload.company_size

    # A human may correct contact details the agent is not allowed to overwrite.
    if payload.email is not None:
        email = normalize_email(payload.email)
        if payload.email and not email:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": {"code": "INVALID_EMAIL", "message": payload.email}},
            )
        lead.email = email
        changes["email"] = email
    if payload.phone is not None:
        phone = normalize_phone(payload.phone)
        if payload.phone and not phone:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": {"code": "INVALID_PHONE", "message": payload.phone}},
            )
        lead.phone_e164 = phone
        changes["phone"] = phone

    if payload.human_takeover is not None:
        lead.human_takeover = payload.human_takeover
        lead.human_takeover_at = (
            datetime.now(timezone.utc) if payload.human_takeover else None
        )
        changes["human_takeover"] = payload.human_takeover
        if payload.human_takeover:
            await repository.log_event(
                db, lead, EventType.HUMAN_TOOK_OVER, {"actor": actor}, actor=actor
            )

    if payload.opt_out:
        await repository.mark_opted_out(db, lead, source=f"crm:{actor}")
        changes["opt_out"] = True

    if changes:
        await repository.log_event(
            db, lead, EventType.STAGE_CHANGED, {"changes": changes}, actor=actor
        )

    await db.commit()
    await db.refresh(lead)
    return _lead_summary(lead)


@router.post("/leads/{lead_id}/messages", response_model=MessageOut)
async def send_human_message(
    lead_id: uuid.UUID, payload: HumanMessageIn, db: AsyncSession = Depends(get_db)
) -> MessageOut:
    """Send as a human, and by default take the thread over.

    The takeover is the point: from here the agent stays quiet on this lead, so a
    colleague and a model never answer the same question twice.
    """
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "NOT_FOUND", "message": "No such lead."}},
        )
    if lead.opt_out_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": {
                    "code": "LEAD_OPTED_OUT",
                    "message": "This lead asked us to stop contacting them.",
                }
            },
        )

    channel = payload.channel
    if channel is None:
        try:
            channel = Channel(lead.source_channel or Channel.WHATSAPP.value)
        except ValueError:
            channel = Channel.WHATSAPP

    if payload.take_over and not lead.human_takeover:
        lead.human_takeover = True
        lead.human_takeover_at = datetime.now(timezone.utc)
        await repository.log_event(
            db,
            lead,
            EventType.HUMAN_TOOK_OVER,
            {"actor": payload.actor},
            actor=payload.actor,
        )

    conversation = await repository.get_or_create_conversation(db, lead, channel)
    recipient = await outbound_module.get_identity(db, lead, channel)
    if not recipient:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": {
                    "code": "NO_RECIPIENT_HANDLE",
                    "message": f"No {channel.value} handle on file for this lead.",
                }
            },
        )

    provider_message_id: Optional[str] = None
    send_error: Optional[str] = None
    try:
        if channel is Channel.WHATSAPP:
            from app.sales.channels import whatsapp

            ids = await whatsapp.send_text(recipient, payload.text)
            provider_message_id = ids[0] if ids else None
        elif channel is Channel.MESSENGER:
            from app.sales.channels import messenger

            ids = await messenger.send_text(recipient, payload.text)
            provider_message_id = ids[0] if ids else None
        elif channel is not Channel.WEB:
            raise ValueError(f"no sender for channel {channel.value}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Human message send failed: %s", exc)
        send_error = str(exc)

    message = await repository.record_message(
        db,
        conversation,
        lead,
        direction=Direction.OUTBOUND,
        author=Author.HUMAN,
        text=payload.text,
        status=MessageStatus.FAILED if send_error else MessageStatus.SENT,
        provider_message_id=provider_message_id,
    )
    await db.commit()

    if send_error or message is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": {
                    "code": "SEND_FAILED",
                    "message": send_error or "Message could not be recorded.",
                }
            },
        )

    return MessageOut(
        id=message.id,
        direction=message.direction,
        channel=message.channel,
        author=message.author,
        text=message.text,
        status=message.status,
        template_name=message.template_name,
        citations=message.citations,
        tool_calls=message.tool_calls,
        created_at=message.created_at,
    )


@router.post("/leads/{lead_id}/sequences", status_code=status.HTTP_202_ACCEPTED)
async def enroll_lead(
    lead_id: uuid.UUID, payload: EnrollIn, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "NOT_FOUND", "message": "No such lead."}},
        )
    sequence = await outbound_module.find_sequence(
        db, lead.tenant_id, payload.sequence_name
    )
    if sequence is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "SEQUENCE_NOT_FOUND",
                    "message": f"No active sequence named '{payload.sequence_name}'.",
                }
            },
        )

    enrollment = await outbound_module.enroll(
        db, lead, sequence, start_delay_hours=payload.start_delay_hours
    )
    await db.commit()
    if enrollment is None:
        return {
            "enrolled": False,
            "reason": "Lead is opted out, owned by a human, already enrolled, "
            "or past the point where a cadence helps.",
        }
    return {
        "enrolled": True,
        "enrollment_id": str(enrollment.id),
        "next_run_at": enrollment.next_run_at.isoformat()
        if enrollment.next_run_at
        else None,
    }


@router.delete("/leads/{lead_id}/sequences")
async def stop_lead_sequences(
    lead_id: uuid.UUID,
    reason: str = Query(default="STOPPED_BY_HUMAN", max_length=100),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": {"code": "NOT_FOUND", "message": "No such lead."}},
        )
    stopped = await repository.stop_active_sequences(db, lead, reason=reason)
    await db.commit()
    return {"stopped": stopped}


# ----------------------------------------------------------------------
# Preview
# ----------------------------------------------------------------------
@router.post("/preview/chat", response_model=PreviewChatOut)
async def preview_chat(
    payload: PreviewChatIn, db: AsyncSession = Depends(get_db)
) -> PreviewChatOut:
    """Talk to the agent from the admin UI, with no Meta connection needed.

    Runs the real inbound path on the ``WEB`` channel rather than a special
    code path, so what is tested here is what customers get.
    """
    event = InboundEvent(
        channel=Channel.WEB,
        kind=KIND_MESSAGE,
        external_id=f"preview:{payload.session_id}",
        text=payload.message,
        profile_name=payload.display_name,
    )
    result = await inbound_module.handle_event(db, _tenant(payload.tenant_id), event)
    await db.commit()

    lead_id = result.get("lead_id")
    if not lead_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": {
                    "code": "AGENT_UNAVAILABLE",
                    "message": str(result.get("reason") or "Agent did not run."),
                }
            },
        )

    return PreviewChatOut(
        lead_id=uuid.UUID(lead_id),
        reply=str(result.get("reply") or ""),
        stage=str(result.get("stage") or Stage.NEW.value),
        score=int(result.get("score") or 0),
        citations=list(result.get("citations") or []),
        tool_calls=list(result.get("tool_calls") or []),
        handoff=result.get("handoff"),
        agent_ok=bool(result.get("agent_ok", True)),
    )


# ----------------------------------------------------------------------
# Upsell
# ----------------------------------------------------------------------
@router.post("/upsell/recommend", response_model=UpsellOut)
async def recommend_upsell(
    payload: UpsellIn, db: AsyncSession = Depends(get_db)
) -> UpsellOut:
    signals = upsell.ClientSignals(
        client_ref=payload.client_ref,
        locale=payload.locale,
        purchased_slugs=list(payload.purchased_slugs),
        latest_delivered=payload.latest_delivered,
        days_since_last_purchase=payload.days_since_last_purchase,
        asked_about_slug=payload.asked_about_slug,
    )
    recommendation = await upsell.recommend(db, _tenant(payload.tenant_id), signals)
    await db.commit()
    if recommendation is None:
        return UpsellOut(recommendation=None)
    return UpsellOut(recommendation=upsell.to_payload(recommendation))


@router.post("/upsell/{recommendation_id}/outcome")
async def record_upsell_outcome(
    recommendation_id: uuid.UUID,
    payload: UpsellOutcomeIn,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    updated = await upsell.record_outcome(
        db, recommendation_id, payload.outcome.value
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {"code": "NOT_FOUND", "message": "No such recommendation."}
            },
        )
    await db.commit()
    return {"outcome": updated.outcome or ""}


# ----------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------
@router.get("/stats", response_model=StatsOut)
async def stats(
    tenant_id: Optional[uuid.UUID] = None, db: AsyncSession = Depends(get_db)
) -> StatsOut:
    tenant = _tenant(tenant_id)
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)

    total = int(
        await db.scalar(
            select(func.count()).select_from(Lead).where(Lead.tenant_id == tenant)
        )
        or 0
    )

    by_stage = {
        str(stage): int(count)
        for stage, count in (
            await db.execute(
                select(Lead.stage, func.count())
                .where(Lead.tenant_id == tenant)
                .group_by(Lead.stage)
            )
        ).all()
    }
    by_channel = {
        str(channel or "UNKNOWN"): int(count)
        for channel, count in (
            await db.execute(
                select(Lead.source_channel, func.count())
                .where(Lead.tenant_id == tenant)
                .group_by(Lead.source_channel)
            )
        ).all()
    }

    goal_stages = [
        Stage.QUALIFIED.value,
        Stage.DEMO_BOOKED.value,
        Stage.PROPOSAL_SENT.value,
        Stage.WON.value,
    ]
    qualified = sum(by_stage.get(stage, 0) for stage in goal_stages)

    opted_out = int(
        await db.scalar(
            select(func.count())
            .select_from(Lead)
            .where(Lead.tenant_id == tenant, Lead.opt_out_at.isnot(None))
        )
        or 0
    )
    awaiting_human = int(
        await db.scalar(
            select(func.count())
            .select_from(Lead)
            .where(Lead.tenant_id == tenant, Lead.human_takeover.is_(True))
        )
        or 0
    )

    async def _message_count(direction: Direction) -> int:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(SalesMessage)
                .where(
                    SalesMessage.tenant_id == tenant,
                    SalesMessage.direction == direction.value,
                    SalesMessage.created_at >= week_ago,
                )
            )
            or 0
        )

    pending = int(
        await db.scalar(
            select(func.count())
            .select_from(OutboundMessage)
            .where(
                OutboundMessage.tenant_id == tenant,
                OutboundMessage.status == OutboundStatus.PENDING.value,
            )
        )
        or 0
    )
    skipped = int(
        await db.scalar(
            select(func.count())
            .select_from(OutboundMessage)
            .where(
                OutboundMessage.tenant_id == tenant,
                OutboundMessage.status == OutboundStatus.SKIPPED.value,
                OutboundMessage.created_at >= week_ago,
            )
        )
        or 0
    )

    # Which creative actually produces conversations, not just clicks.
    campaigns = [
        {"campaign": str(name), "leads": int(count)}
        for name, count in (
            await db.execute(
                select(Lead.source_campaign, func.count())
                .where(Lead.tenant_id == tenant, Lead.source_campaign.isnot(None))
                .group_by(Lead.source_campaign)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()
    ]

    return StatsOut(
        leads_total=total,
        leads_by_stage=by_stage,
        leads_by_channel=by_channel,
        qualified_or_better=qualified,
        opted_out=opted_out,
        awaiting_human=awaiting_human,
        messages_in_last_7d=await _message_count(Direction.INBOUND),
        messages_out_last_7d=await _message_count(Direction.OUTBOUND),
        outbound_pending=pending,
        outbound_skipped_last_7d=skipped,
        top_campaigns=campaigns,
    )


@router.get("/sequences")
async def list_sequences(
    tenant_id: Optional[uuid.UUID] = None, db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = await db.execute(
        select(SalesSequence)
        .where(SalesSequence.tenant_id == _tenant(tenant_id))
        .order_by(SalesSequence.name.asc())
    )
    return [
        {
            "id": str(sequence.id),
            "name": sequence.name,
            "description": sequence.description,
            "audience": sequence.audience,
            "is_active": sequence.is_active,
            "step_count": len(sequence.steps or []),
            "steps": sequence.steps,
        }
        for sequence in rows.scalars().all()
    ]


@router.post("/outbound/drain", status_code=status.HTTP_202_ACCEPTED)
async def drain_outbound(
    limit: int = Body(default=50, ge=1, le=200, embed=True),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Process the outbound queue now, instead of waiting for the next tick.

    Exists for operators: after fixing a template name you want to see the
    backlog move without restarting anything.
    """
    config = outbound_module.policy_config()
    results: dict[str, int] = {}
    for row in await outbound_module.due_outbound(db, limit=limit):
        outcome = await outbound_module.process_outbound(db, row, config=config)
        results[outcome] = results.get(outcome, 0) + 1
    await db.commit()
    return {"processed": sum(results.values()), "by_status": results}
