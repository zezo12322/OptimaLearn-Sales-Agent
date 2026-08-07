"""Persistence for the AI sales agent.

Everything here is tenant-scoped and hangs off ``sales_leads``. Three clusters:

* **CRM** — ``sales_leads`` and its identities, conversations, messages and
  event timeline. One lead can be reached on several channels (the same person
  writes on Messenger today and WhatsApp tomorrow), so channel handles live in
  ``sales_lead_identities`` rather than on the lead itself.
* **Knowledge base** — ``sales_documents`` chunked and embedded into
  ``sales_chunk_embeddings``. Deliberately separate from ``transcript_chunks``:
  transcripts are scoped to a video, sales content is scoped to an audience.
* **Outbound** — a durable queue (``sales_outbound_messages``) plus cadences
  (``sales_sequences`` / ``sales_sequence_enrollments``). Nothing is ever sent
  straight from a request handler; the queue row is the audit record and the
  place the messaging-policy verdict is written down.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime

from app.models.base import EMBEDDING_DIM, Base
from app.sales.enums import (
    Audience,
    ConversationStatus,
    DocType,
    OutboundKind,
    OutboundStatus,
    Segment,
    SequenceStatus,
    Stage,
)


class Lead(Base):
    """A prospect. The single row a salesperson opens to see the whole story."""

    __tablename__ = "sales_leads"
    __table_args__ = (
        # Natural-key de-duplication, scoped per tenant and skipping NULLs so a
        # lead reachable only on Messenger (no phone yet) never collides.
        Index(
            "uq_sales_leads_tenant_phone",
            "tenant_id",
            "phone_e164",
            unique=True,
            postgresql_where=text("phone_e164 IS NOT NULL"),
        ),
        Index(
            "uq_sales_leads_tenant_email",
            "tenant_id",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        Index("ix_sales_leads_tenant_stage", "tenant_id", "stage"),
        Index("ix_sales_leads_tenant_score", "tenant_id", "score"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)

    full_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    phone_e164: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    segment: Mapped[str] = mapped_column(
        String(16), default=Segment.UNKNOWN.value, nullable=False
    )
    company_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    company_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    job_title: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    #: BCP-47-ish short code ("ar" / "en"), detected from the first message and
    #: then honoured for every reply and template on this lead.
    locale: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    #: IANA zone used for quiet-hours checks; falls back to the tenant default.
    timezone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    stage: Mapped[str] = mapped_column(
        String(24), default=Stage.NEW.value, nullable=False
    )
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Per-signal contribution, so a rep can see *why* a lead scores 78.
    score_breakdown: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    #: Captured qualification answers (need, budget, authority, timeline, seats,
    #: interests, objections). Free-form by design — the agent adds keys as the
    #: playbook evolves and the scorer reads only the ones it knows.
    qualification: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )

    source_channel: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    #: Campaign / ad set name when the lead arrived from a paid placement.
    source_campaign: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    #: Post id, ad id, lead-gen form id — whatever identifies the exact origin.
    source_detail: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    owner_user_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    assigned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: While true the agent stays silent on this lead: a human is in the thread.
    human_takeover: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    human_takeover_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Lawful basis for proactive outreach. Never inferred — only set when the
    #: lead messages us first, submits a form, or says yes to follow-up.
    marketing_opt_in: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    opt_in_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    opt_in_source: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    #: Set once and never cleared by automation. Hard stop for all outbound.
    opt_out_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Drives the Meta service window. Updated on every inbound message.
    last_inbound_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_outbound_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Set once the prospect becomes a paying client — the customer e-mail on
    #: their order, or another stable reference. This is the join that makes
    #: closed-loop reporting (and the cross-sell path) possible.
    client_ref: Mapped[Optional[str]] = mapped_column(
        String, nullable=True, index=True
    )

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    identities: Mapped[list["LeadIdentity"]] = relationship(
        "LeadIdentity", back_populates="lead", cascade="all, delete-orphan"
    )
    conversations: Mapped[list["SalesConversation"]] = relationship(
        "SalesConversation", back_populates="lead", cascade="all, delete-orphan"
    )


class LeadIdentity(Base):
    """A channel handle (WhatsApp ``wa_id``, Messenger PSID, email) for a lead."""

    __tablename__ = "sales_lead_identities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel",
            "external_id",
            name="uq_sales_lead_identities_channel_external",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sales_leads.id", ondelete="CASCADE"),
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    #: Profile name and any other provider-supplied context.
    extra: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    lead: Mapped["Lead"] = relationship("Lead", back_populates="identities")


class SalesConversation(Base):
    """One thread with a lead on one channel."""

    __tablename__ = "sales_conversations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "lead_id",
            "channel",
            name="uq_sales_conversations_lead_channel",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sales_leads.id", ondelete="CASCADE"),
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=ConversationStatus.OPEN.value, nullable=False
    )
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    lead: Mapped["Lead"] = relationship("Lead", back_populates="conversations")
    messages: Mapped[list["SalesMessage"]] = relationship(
        "SalesMessage", back_populates="conversation", cascade="all, delete-orphan"
    )


class SalesMessage(Base):
    """A single message, whichever direction and whoever wrote it."""

    __tablename__ = "sales_messages"
    __table_args__ = (
        # Provider ids are unique per provider; this is what makes webhook
        # redelivery (which Meta does freely) a no-op.
        Index(
            "uq_sales_messages_provider_id",
            "provider_message_id",
            unique=True,
            postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
        Index("ix_sales_messages_lead_created", "lead_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sales_conversations.id", ondelete="CASCADE"),
        index=True,
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)

    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    author: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    provider_message_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    #: Set when the message went out as a Meta template.
    template_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    #: Which tools the agent invoked to produce this reply, and with what args.
    #: The reason a rep can audit an answer without re-running the model.
    tool_calls: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(
        JSONB, nullable=True
    )
    #: Knowledge-base passages the answer was grounded in.
    citations: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(
        JSONB, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped["SalesConversation"] = relationship(
        "SalesConversation", back_populates="messages"
    )


class LeadEvent(Base):
    """Append-only timeline. Nothing in here is ever updated or deleted."""

    __tablename__ = "sales_lead_events"
    __table_args__ = (
        Index("ix_sales_lead_events_lead_created", "lead_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sales_leads.id", ondelete="CASCADE"),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Who caused it: "agent", "system", or a colleague's identifier for human
    #: actions.
    actor: Mapped[str] = mapped_column(String, default="agent", nullable=False)
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SalesDocument(Base):
    """A knowledge-base source: pricing sheet, FAQ, objection card, case study."""

    __tablename__ = "sales_documents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "content_hash", name="uq_sales_documents_tenant_hash"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)

    title: Mapped[str] = mapped_column(String, nullable=False)
    doc_type: Mapped[str] = mapped_column(
        String(24), default=DocType.PRODUCT.value, nullable=False
    )
    audience: Mapped[str] = mapped_column(
        String(8), default=Audience.ALL.value, nullable=False
    )
    locale: Mapped[str] = mapped_column(String(8), default="ar", nullable=False)
    source_uri: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: Bumped on every re-ingest so an answer can be traced to a revision.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    chunks: Mapped[list["SalesChunk"]] = relationship(
        "SalesChunk", back_populates="document", cascade="all, delete-orphan"
    )


class SalesChunk(Base):
    __tablename__ = "sales_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_sales_chunks_document_index"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sales_documents.id", ondelete="CASCADE"),
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document: Mapped["SalesDocument"] = relationship(
        "SalesDocument", back_populates="chunks"
    )
    embedding: Mapped[Optional["SalesChunkEmbedding"]] = relationship(
        "SalesChunkEmbedding",
        back_populates="chunk",
        cascade="all, delete-orphan",
        uselist=False,
    )


class SalesChunkEmbedding(Base):
    """Vector index for the sales knowledge base.

    ``audience``/``locale``/``doc_type`` are denormalised from the document so
    retrieval can filter on them inside the vector scan instead of joining back
    — the same trade-off ``chunk_embeddings`` makes for transcripts.
    """

    __tablename__ = "sales_chunk_embeddings"
    __table_args__ = (
        Index(
            "ix_sales_chunk_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_sales_chunk_embeddings_filters", "tenant_id", "audience", "locale"),
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sales_chunks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)

    audience: Mapped[str] = mapped_column(String(8), nullable=False)
    locale: Mapped[str] = mapped_column(String(8), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(24), nullable=False)

    embedding: Mapped[Vector] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    chunk: Mapped["SalesChunk"] = relationship("SalesChunk", back_populates="embedding")


class OutboundMessage(Base):
    """A queued proactive message plus the policy verdict that let it through.

    Rows are never deleted: a ``SKIPPED`` row with a ``skip_reason`` is the
    evidence that the agent declined to message someone, which matters as much
    as the sends when a platform review asks how consent is enforced.
    """

    __tablename__ = "sales_outbound_messages"
    __table_args__ = (
        # Idempotency for sequence steps and webhook-triggered first touches:
        # the same logical send can be enqueued twice and still go out once.
        UniqueConstraint("dedupe_key", name="uq_sales_outbound_dedupe_key"),
        Index("ix_sales_outbound_due", "status", "scheduled_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sales_leads.id", ondelete="CASCADE"),
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(16), nullable=False)

    kind: Mapped[str] = mapped_column(
        String(16), default=OutboundKind.TEMPLATE.value, nullable=False
    )
    template_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    template_language: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    template_params: Mapped[Optional[list[str]]] = mapped_column(JSONB, nullable=True)
    #: Text for FREEFORM sends, or a human-readable rendering of the template
    #: so the CRM timeline reads naturally either way.
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default=OutboundStatus.PENDING.value, nullable=False
    )
    skip_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    dedupe_key: Mapped[str] = mapped_column(String, nullable=False)
    sequence_enrollment_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SalesSequence(Base):
    """A follow-up cadence.

    Steps are JSONB because a cadence is edited as a whole (reorder, retime,
    swap a template) and is only ever read whole. A steps table would buy
    referential integrity we do not need and cost a join on every tick.
    Each step: ``{delay_hours, channel, template_name, template_language,
    template_params, body, goal}``.
    """

    __tablename__ = "sales_sequences"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_sales_sequences_tenant_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    audience: Mapped[str] = mapped_column(
        String(8), default=Audience.ALL.value, nullable=False
    )
    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SequenceEnrollment(Base):
    __tablename__ = "sales_sequence_enrollments"
    __table_args__ = (
        UniqueConstraint(
            "lead_id",
            "sequence_id",
            name="uq_sales_sequence_enrollments_lead_sequence",
        ),
        Index("ix_sales_sequence_enrollments_due", "status", "next_run_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sales_leads.id", ondelete="CASCADE"),
        index=True,
    )
    sequence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sales_sequences.id", ondelete="CASCADE"),
        index=True,
    )
    current_step: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=SequenceStatus.ACTIVE.value, nullable=False
    )
    next_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    stop_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UpsellRecommendation(Base):
    """An in-product upgrade suggestion and, later, what came of it."""

    __tablename__ = "sales_upsell_recommendations"
    __table_args__ = (
        Index(
            "ix_sales_upsell_client_created", "tenant_id", "client_ref", "created_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    client_ref: Mapped[str] = mapped_column(String, nullable=False)

    #: The package suggested — a slug from `app.sales.offerings`, and its title in
    #: the client's locale at the time (kept so an old recommendation still reads
    #: correctly after the catalogue is renamed).
    recommended_service_slug: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    recommended_service_title: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    #: Machine-readable trigger ("HIT_FREE_LIMIT", "HIGH_COMPLETION", ...).
    reason_code: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The copy shown to the user, in their locale.
    pitch: Mapped[str] = mapped_column(Text, nullable=False)
    locale: Mapped[str] = mapped_column(String(8), default="ar", nullable=False)
    signals: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    outcome: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    outcome_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "Lead",
    "LeadIdentity",
    "SalesConversation",
    "SalesMessage",
    "LeadEvent",
    "SalesDocument",
    "SalesChunk",
    "SalesChunkEmbedding",
    "OutboundMessage",
    "SalesSequence",
    "SequenceEnrollment",
    "UpsellRecommendation",
]
