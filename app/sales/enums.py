"""Vocabulary shared by the sales-agent domain.

These live in their own dependency-free module on purpose: the policy engine,
the qualification scorer and the tests all need the vocabulary without pulling
in settings, the database or the OpenAI client.

Values are persisted as plain ``VARCHAR`` rather than PostgreSQL ``ENUM`` types
(the pattern used by the ingestion tables). A sales pipeline gains stages and
channels as the business learns; ``ALTER TYPE`` for every one of those is a
migration tax we do not want to pay.
"""

from enum import StrEnum


class Channel(StrEnum):
    WHATSAPP = "WHATSAPP"
    MESSENGER = "MESSENGER"
    # Used by the admin preview playground and any future site widget. Having a
    # third channel from day one keeps the adapters honest about staying generic.
    WEB = "WEB"
    EMAIL = "EMAIL"


#: Channels that Meta governs (consent, service windows, template rules).
META_CHANNELS = frozenset({Channel.WHATSAPP, Channel.MESSENGER})


class Segment(StrEnum):
    UNKNOWN = "UNKNOWN"
    B2C = "B2C"
    B2B = "B2B"


class Audience(StrEnum):
    """Which segment a knowledge-base document is written for."""

    ALL = "ALL"
    B2C = "B2C"
    B2B = "B2B"


class Stage(StrEnum):
    NEW = "NEW"
    ENGAGED = "ENGAGED"
    QUALIFYING = "QUALIFYING"
    QUALIFIED = "QUALIFIED"
    DEMO_BOOKED = "DEMO_BOOKED"
    PROPOSAL_SENT = "PROPOSAL_SENT"
    WON = "WON"
    LOST = "LOST"
    UNQUALIFIED = "UNQUALIFIED"
    NURTURE = "NURTURE"


#: Stages a human owns. The agent never auto-replies or auto-sequences a lead
#: parked in one of these.
TERMINAL_STAGES = frozenset({Stage.WON, Stage.LOST, Stage.UNQUALIFIED})


class Direction(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class Author(StrEnum):
    LEAD = "LEAD"
    AGENT = "AGENT"
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"


class MessageStatus(StrEnum):
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"


class OutboundKind(StrEnum):
    #: Plain text. Only legal inside an open service window.
    FREEFORM = "FREEFORM"
    #: Pre-approved Meta template. The only way to reopen a closed window.
    TEMPLATE = "TEMPLATE"


class OutboundStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    #: Policy refused the send (no consent, quiet hours, cap reached...).
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class ConversationStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class DocType(StrEnum):
    PRODUCT = "PRODUCT"
    PRICING = "PRICING"
    FAQ = "FAQ"
    OBJECTION = "OBJECTION"
    CASE_STUDY = "CASE_STUDY"
    POLICY = "POLICY"
    COURSE = "COURSE"


class SequenceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"


class UpsellOutcome(StrEnum):
    SHOWN = "SHOWN"
    CLICKED = "CLICKED"
    CONVERTED = "CONVERTED"
    DISMISSED = "DISMISSED"


class EventType(StrEnum):
    """Timeline entries. Every state change a human might have to explain."""

    LEAD_CREATED = "LEAD_CREATED"
    IDENTITY_LINKED = "IDENTITY_LINKED"
    INBOUND_RECEIVED = "INBOUND_RECEIVED"
    AGENT_REPLIED = "AGENT_REPLIED"
    DETAILS_CAPTURED = "DETAILS_CAPTURED"
    STAGE_CHANGED = "STAGE_CHANGED"
    SCORE_CHANGED = "SCORE_CHANGED"
    DEMO_BOOKED = "DEMO_BOOKED"
    CHECKOUT_LINK_SENT = "CHECKOUT_LINK_SENT"
    HANDOFF_REQUESTED = "HANDOFF_REQUESTED"
    HUMAN_TOOK_OVER = "HUMAN_TOOK_OVER"
    OPTED_IN = "OPTED_IN"
    OPTED_OUT = "OPTED_OUT"
    OUTBOUND_QUEUED = "OUTBOUND_QUEUED"
    OUTBOUND_SENT = "OUTBOUND_SENT"
    OUTBOUND_SKIPPED = "OUTBOUND_SKIPPED"
    OUTBOUND_FAILED = "OUTBOUND_FAILED"
    SEQUENCE_ENROLLED = "SEQUENCE_ENROLLED"
    SEQUENCE_STOPPED = "SEQUENCE_STOPPED"
    UPSELL_RECOMMENDED = "UPSELL_RECOMMENDED"
