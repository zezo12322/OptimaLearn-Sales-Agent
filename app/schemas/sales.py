"""Request and response bodies for the admin/internal API.

Field names are snake_case on the wire; the LMS proxy maps them for the web app.
Validation is strict where a bad value would be expensive — stage and outcome
are enums, message bodies are length-bounded — and permissive where the caller
is a trusted service.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.sales.enums import (
    Audience,
    Channel,
    DocType,
    Segment,
    Stage,
    UpsellOutcome,
)


# ----------------------------------------------------------------------
# Knowledge base
# ----------------------------------------------------------------------
class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=200_000)
    doc_type: DocType = DocType.PRODUCT
    audience: Audience = Audience.ALL
    locale: str = Field(default="ar", max_length=8)
    source_uri: Optional[str] = Field(default=None, max_length=1000)
    tenant_id: Optional[uuid.UUID] = None


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    doc_type: str
    audience: str
    locale: str
    is_active: bool
    chunk_count: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DocumentIngestResult(BaseModel):
    document: DocumentOut
    chunk_count: int
    created: bool


class SeedResult(BaseModel):
    documents_created: int
    documents_existing: int
    sequences_created: int
    sequences_existing: int


# ----------------------------------------------------------------------
# Leads
# ----------------------------------------------------------------------
class LeadSummary(BaseModel):
    id: uuid.UUID
    full_name: Optional[str]
    phone_e164: Optional[str]
    email: Optional[str]
    segment: str
    company_name: Optional[str]
    stage: str
    score: int
    locale: Optional[str]
    source_channel: Optional[str]
    source_campaign: Optional[str]
    owner_user_id: Optional[str]
    human_takeover: bool
    marketing_opt_in: bool
    opted_out: bool
    last_inbound_at: Optional[datetime]
    last_outbound_at: Optional[datetime]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


class LeadListOut(BaseModel):
    items: list[LeadSummary]
    total: int
    limit: int
    offset: int


class MessageOut(BaseModel):
    id: uuid.UUID
    direction: str
    channel: str
    author: str
    text: str
    status: str
    template_name: Optional[str]
    citations: Optional[list[dict[str, Any]]]
    tool_calls: Optional[list[dict[str, Any]]]
    created_at: Optional[datetime]


class EventOut(BaseModel):
    id: uuid.UUID
    event_type: str
    actor: str
    payload: Optional[dict[str, Any]]
    created_at: Optional[datetime]


class OutboundOut(BaseModel):
    id: uuid.UUID
    channel: str
    kind: str
    template_name: Optional[str]
    body: Optional[str]
    status: str
    skip_reason: Optional[str]
    scheduled_at: Optional[datetime]
    sent_at: Optional[datetime]
    attempt_count: int


class LeadDetailOut(BaseModel):
    lead: LeadSummary
    qualification: dict[str, Any]
    score_breakdown: Optional[dict[str, Any]]
    missing_fields: list[str]
    messages: list[MessageOut]
    events: list[EventOut]
    outbound: list[OutboundOut]
    notes: Optional[str]


class LeadPatch(BaseModel):
    """Human edits. Deliberately does not accept ``score`` — it is derived."""

    stage: Optional[Stage] = None
    segment: Optional[Segment] = None
    owner_user_id: Optional[str] = Field(default=None, max_length=200)
    notes: Optional[str] = Field(default=None, max_length=5000)
    human_takeover: Optional[bool] = None
    full_name: Optional[str] = Field(default=None, max_length=200)
    email: Optional[str] = Field(default=None, max_length=320)
    phone: Optional[str] = Field(default=None, max_length=40)
    company_name: Optional[str] = Field(default=None, max_length=200)
    company_size: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    #: Set to true to record an opt-out on the lead's behalf (they asked a rep).
    opt_out: Optional[bool] = None
    actor: Optional[str] = Field(default=None, max_length=200)


class HumanMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    channel: Optional[Channel] = None
    #: LMS uid of the person sending, recorded on the timeline.
    actor: str = Field(default="human", max_length=200)
    #: Sending as a human takes the thread over by default, so the agent stops
    #: talking over the colleague who just joined.
    take_over: bool = True


class EnrollIn(BaseModel):
    sequence_name: str = Field(min_length=1, max_length=200)
    start_delay_hours: Optional[float] = Field(default=None, ge=0, le=8760)


# ----------------------------------------------------------------------
# Preview / playground
# ----------------------------------------------------------------------
class PreviewChatIn(BaseModel):
    """Drive the agent from the admin UI without a Meta connection."""

    session_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=2000)
    tenant_id: Optional[uuid.UUID] = None
    display_name: Optional[str] = Field(default=None, max_length=200)


class PreviewChatOut(BaseModel):
    lead_id: uuid.UUID
    reply: str
    stage: str
    score: int
    citations: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    handoff: Optional[dict[str, Any]] = None
    agent_ok: bool = True


# ----------------------------------------------------------------------
# Upsell
# ----------------------------------------------------------------------
class UpsellIn(BaseModel):
    lms_user_id: str = Field(min_length=1, max_length=200)
    locale: str = Field(default="ar", max_length=8)
    current_plan: Optional[str] = Field(default=None, max_length=200)
    enrolled_courses: int = Field(default=0, ge=0)
    completed_courses: int = Field(default=0, ge=0)
    completed_lectures: int = Field(default=0, ge=0)
    quiz_pass_rate: Optional[float] = Field(default=None, ge=0, le=1)
    days_active: int = Field(default=0, ge=0)
    hit_limit: bool = False
    tenant_id: Optional[uuid.UUID] = None


class UpsellOut(BaseModel):
    #: ``None`` means "say nothing" — the honest answer most of the time.
    recommendation: Optional[dict[str, Any]] = None


class UpsellOutcomeIn(BaseModel):
    outcome: UpsellOutcome


# ----------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------
class StatsOut(BaseModel):
    leads_total: int
    leads_by_stage: dict[str, int]
    leads_by_channel: dict[str, int]
    qualified_or_better: int
    opted_out: int
    awaiting_human: int
    messages_in_last_7d: int
    messages_out_last_7d: int
    outbound_pending: int
    outbound_skipped_last_7d: int
    top_campaigns: list[dict[str, Any]]
