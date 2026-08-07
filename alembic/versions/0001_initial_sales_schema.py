"""initial sales agent schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-07 12:00:00.000000

Creates the twelve ``sales_*`` tables plus the ``vector`` extension the knowledge
base needs.

Status, stage and channel columns are ``VARCHAR`` rather than PostgreSQL ``ENUM``.
A sales pipeline gains stages as the business learns, and paying an ``ALTER TYPE``
migration for each one buys nothing — the vocabulary is enforced in
``app/sales/enums.py``, where it is also readable.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 1536


def upgrade() -> None:
    # pgvector must exist before any column can use the type.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "sales_leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("full_name", sa.String(), nullable=True),
        sa.Column("phone_e164", sa.String(length=20), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("segment", sa.String(length=16), nullable=False),
        sa.Column("company_name", sa.String(), nullable=True),
        sa.Column("company_size", sa.Integer(), nullable=True),
        sa.Column("job_title", sa.String(), nullable=True),
        sa.Column("locale", sa.String(length=8), nullable=True),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("stage", sa.String(length=24), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("score_breakdown", postgresql.JSONB(), nullable=True),
        sa.Column("qualification", postgresql.JSONB(), nullable=False),
        sa.Column("source_channel", sa.String(length=16), nullable=True),
        sa.Column("source_campaign", sa.String(), nullable=True),
        sa.Column("source_detail", sa.String(), nullable=True),
        sa.Column("owner_user_id", sa.String(), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("human_takeover", sa.Boolean(), nullable=False),
        sa.Column("human_takeover_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("marketing_opt_in", sa.Boolean(), nullable=False),
        sa.Column("opt_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opt_in_source", sa.String(), nullable=True),
        sa.Column("opt_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_ref", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sales_leads_tenant_id", "sales_leads", ["tenant_id"])
    op.create_index("ix_sales_leads_client_ref", "sales_leads", ["client_ref"])
    op.create_index(
        "ix_sales_leads_tenant_stage", "sales_leads", ["tenant_id", "stage"]
    )
    op.create_index(
        "ix_sales_leads_tenant_score", "sales_leads", ["tenant_id", "score"]
    )
    # Natural-key de-duplication that tolerates leads with no phone/e-mail yet.
    op.create_index(
        "uq_sales_leads_tenant_phone",
        "sales_leads",
        ["tenant_id", "phone_e164"],
        unique=True,
        postgresql_where=sa.text("phone_e164 IS NOT NULL"),
    )
    op.create_index(
        "uq_sales_leads_tenant_email",
        "sales_leads",
        ["tenant_id", "email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )

    op.create_table(
        "sales_lead_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("extra", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["sales_leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "channel",
            "external_id",
            name="uq_sales_lead_identities_channel_external",
        ),
    )
    op.create_index(
        "ix_sales_lead_identities_tenant_id", "sales_lead_identities", ["tenant_id"]
    )
    op.create_index(
        "ix_sales_lead_identities_lead_id", "sales_lead_identities", ["lead_id"]
    )

    op.create_table(
        "sales_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["sales_leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "lead_id",
            "channel",
            name="uq_sales_conversations_lead_channel",
        ),
    )
    op.create_index(
        "ix_sales_conversations_tenant_id", "sales_conversations", ["tenant_id"]
    )
    op.create_index(
        "ix_sales_conversations_lead_id", "sales_conversations", ["lead_id"]
    )

    op.create_table(
        "sales_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("author", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("template_name", sa.String(), nullable=True),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=True),
        sa.Column("citations", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["sales_conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sales_messages_tenant_id", "sales_messages", ["tenant_id"])
    op.create_index(
        "ix_sales_messages_conversation_id", "sales_messages", ["conversation_id"]
    )
    op.create_index("ix_sales_messages_lead_id", "sales_messages", ["lead_id"])
    op.create_index(
        "ix_sales_messages_lead_created", "sales_messages", ["lead_id", "created_at"]
    )
    # Makes Meta's webhook redelivery a no-op.
    op.create_index(
        "uq_sales_messages_provider_id",
        "sales_messages",
        ["provider_message_id"],
        unique=True,
        postgresql_where=sa.text("provider_message_id IS NOT NULL"),
    )

    op.create_table(
        "sales_lead_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["sales_leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sales_lead_events_tenant_id", "sales_lead_events", ["tenant_id"]
    )
    op.create_index("ix_sales_lead_events_lead_id", "sales_lead_events", ["lead_id"])
    op.create_index(
        "ix_sales_lead_events_lead_created",
        "sales_lead_events",
        ["lead_id", "created_at"],
    )

    op.create_table(
        "sales_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("doc_type", sa.String(length=24), nullable=False),
        sa.Column("audience", sa.String(length=8), nullable=False),
        sa.Column("locale", sa.String(length=8), nullable=False),
        sa.Column("source_uri", sa.String(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "content_hash", name="uq_sales_documents_tenant_hash"
        ),
    )
    op.create_index("ix_sales_documents_tenant_id", "sales_documents", ["tenant_id"])
    op.create_index(
        "ix_sales_documents_content_hash", "sales_documents", ["content_hash"]
    )

    op.create_table(
        "sales_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["sales_documents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "chunk_index", name="uq_sales_chunks_document_index"
        ),
    )
    op.create_index("ix_sales_chunks_tenant_id", "sales_chunks", ["tenant_id"])
    op.create_index("ix_sales_chunks_document_id", "sales_chunks", ["document_id"])

    op.create_table(
        "sales_chunk_embeddings",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("audience", sa.String(length=8), nullable=False),
        sa.Column("locale", sa.String(length=8), nullable=False),
        sa.Column("doc_type", sa.String(length=24), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["chunk_id"], ["sales_chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chunk_id"),
    )
    op.create_index(
        "ix_sales_chunk_embeddings_tenant_id", "sales_chunk_embeddings", ["tenant_id"]
    )
    op.create_index(
        "ix_sales_chunk_embeddings_document_id",
        "sales_chunk_embeddings",
        ["document_id"],
    )
    op.create_index(
        "ix_sales_chunk_embeddings_filters",
        "sales_chunk_embeddings",
        ["tenant_id", "audience", "locale"],
    )
    op.create_index(
        "ix_sales_chunk_embeddings_hnsw",
        "sales_chunk_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "sales_outbound_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("template_name", sa.String(), nullable=True),
        sa.Column("template_language", sa.String(length=8), nullable=True),
        sa.Column("template_params", postgresql.JSONB(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "scheduled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("skip_reason", sa.String(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column(
            "sequence_enrollment_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["lead_id"], ["sales_leads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_sales_outbound_dedupe_key"),
    )
    op.create_index(
        "ix_sales_outbound_messages_tenant_id",
        "sales_outbound_messages",
        ["tenant_id"],
    )
    op.create_index(
        "ix_sales_outbound_messages_lead_id", "sales_outbound_messages", ["lead_id"]
    )
    op.create_index(
        "ix_sales_outbound_messages_sequence_enrollment_id",
        "sales_outbound_messages",
        ["sequence_enrollment_id"],
    )
    op.create_index(
        "ix_sales_outbound_due", "sales_outbound_messages", ["status", "scheduled_at"]
    )

    op.create_table(
        "sales_sequences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("audience", sa.String(length=8), nullable=False),
        sa.Column("steps", postgresql.JSONB(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_sales_sequences_tenant_name"),
    )
    op.create_index("ix_sales_sequences_tenant_id", "sales_sequences", ["tenant_id"])

    op.create_table(
        "sales_sequence_enrollments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("current_step", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["sales_leads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["sequence_id"], ["sales_sequences.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "lead_id",
            "sequence_id",
            name="uq_sales_sequence_enrollments_lead_sequence",
        ),
    )
    op.create_index(
        "ix_sales_sequence_enrollments_tenant_id",
        "sales_sequence_enrollments",
        ["tenant_id"],
    )
    op.create_index(
        "ix_sales_sequence_enrollments_lead_id",
        "sales_sequence_enrollments",
        ["lead_id"],
    )
    op.create_index(
        "ix_sales_sequence_enrollments_sequence_id",
        "sales_sequence_enrollments",
        ["sequence_id"],
    )
    op.create_index(
        "ix_sales_sequence_enrollments_due",
        "sales_sequence_enrollments",
        ["status", "next_run_at"],
    )

    op.create_table(
        "sales_upsell_recommendations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_ref", sa.String(), nullable=False),
        sa.Column("recommended_service_slug", sa.String(), nullable=True),
        sa.Column("recommended_service_title", sa.String(), nullable=True),
        sa.Column("reason_code", sa.String(length=32), nullable=False),
        sa.Column("pitch", sa.Text(), nullable=False),
        sa.Column("locale", sa.String(length=8), nullable=False),
        sa.Column("signals", postgresql.JSONB(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sales_upsell_recommendations_tenant_id",
        "sales_upsell_recommendations",
        ["tenant_id"],
    )
    op.create_index(
        "ix_sales_upsell_client_created",
        "sales_upsell_recommendations",
        ["tenant_id", "client_ref", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("sales_upsell_recommendations")
    op.drop_table("sales_sequence_enrollments")
    op.drop_table("sales_sequences")
    op.drop_table("sales_outbound_messages")
    op.drop_table("sales_chunk_embeddings")
    op.drop_table("sales_chunks")
    op.drop_table("sales_documents")
    op.drop_table("sales_lead_events")
    op.drop_table("sales_messages")
    op.drop_table("sales_conversations")
    op.drop_table("sales_lead_identities")
    op.drop_table("sales_leads")
    # The extension is left in place: other schemas in the same database may be
    # using it, and dropping it would take their indexes with it.
