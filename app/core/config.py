"""Configuration for the sales agent service.

Everything is env-driven with sane defaults except the five things that have no
safe default — the internal API key, the database URLs, Redis, and the Azure
OpenAI credentials. Those are required, so a misconfigured deployment fails at
startup instead of at the first customer message.

Channel credentials are optional on purpose: the service must boot and serve its
health check and admin API before anyone has finished a Meta app review. What it
will not do is send anything without them.
"""

import uuid

from pydantic_settings import BaseSettings, SettingsConfigDict

#: Tenant used when a caller does not supply one. A single-tenant deployment
#: needs no configuration at all; the LMS defaults to the same value.
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class Settings(BaseSettings):
    # ------------------------------------------------------------------
    # Required infrastructure
    # ------------------------------------------------------------------
    #: Shared secret every admin/internal endpoint requires. Webhooks use Meta
    #: signatures instead — they are called by Meta, not by us.
    internal_api_key: str
    #: asyncpg URL used by the API.
    database_url: str
    #: psycopg2 URL used by Alembic and the Celery worker.
    sync_database_url: str
    redis_url: str

    azure_openai_api_key: str
    azure_openai_endpoint: str
    azure_openai_api_version: str

    azure_deployment_embeddings: str = "text-embedding-3-small"
    azure_deployment_chat: str = "gpt-5-mini"

    # ------------------------------------------------------------------
    # Agent identity and behaviour
    # ------------------------------------------------------------------
    #: Master switch. When false, webhooks still return 200 (Meta requires it or
    #: it disables the subscription) but nothing is processed and nothing is sent.
    sales_agent_enabled: bool = True
    default_tenant_id: uuid.UUID = DEFAULT_TENANT_ID

    sales_company_name: str = "OptimaLearn"
    sales_agent_display_name: str = "Nour"
    #: Booking page the agent links to for demos.
    sales_booking_url: str | None = None
    #: Where handoff notifications go. Empty disables notification.
    sales_handoff_notify_email: str | None = None

    #: Cap on tool-calling rounds per reply. Without it, a model that keeps
    #: re-searching turns one WhatsApp message into an unbounded bill.
    sales_max_tool_rounds: int = 4
    #: Turns of history handed to the model (each side counts as one).
    sales_history_messages: int = 12
    sales_kb_top_k: int = 6
    sales_kb_min_similarity: float = 0.20
    #: Days before the same learner may be shown another upgrade suggestion.
    sales_upsell_cooldown_days: int = 7
    #: Standing volume discounts for team quotes, as ``{min_seats: fraction}``
    #: e.g. ``{"25": 0.1, "100": 0.2}``. Empty means list price only — anything
    #: beyond a published band is escalated to a human, never improvised.
    sales_volume_discount_bands: dict[int, float] = {}

    # ------------------------------------------------------------------
    # LMS lookups (live catalogue and pricing)
    # ------------------------------------------------------------------
    #: NestJS API root, including the version prefix, e.g.
    #: ``https://api.example.com/api/v1``.
    lms_api_base_url: str | None = None
    #: Shared secret for the LMS endpoints reserved for service-to-service calls.
    lms_internal_api_key: str | None = None
    #: Public site root, used to build the course/pricing/signup links the agent
    #: sends. A prospect has no session yet, so the agent links into the funnel
    #: rather than minting a checkout it cannot authenticate.
    sales_web_base_url: str | None = None
    #: Seconds to wait on an LMS lookup before answering without it.
    lms_request_timeout_seconds: float = 8.0

    # ------------------------------------------------------------------
    # Meta platform
    # ------------------------------------------------------------------
    #: App secret used to verify X-Hub-Signature-256 on every webhook.
    meta_app_secret: str | None = None
    meta_graph_version: str = "v21.0"

    whatsapp_verify_token: str | None = None
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_template_language: str = "ar"

    messenger_verify_token: str | None = None
    messenger_page_access_token: str | None = None
    messenger_page_id: str | None = None

    #: Approved WhatsApp template for the first touch after a Facebook lead form
    #: is submitted. Without it, form leads wait for a human — by design: an
    #: unapproved template name is a silent failure, not a fallback to text.
    sales_lead_form_template_name: str | None = None
    #: Approved template used to re-open a closed window during a cadence.
    sales_reengage_template_name: str | None = None

    # ------------------------------------------------------------------
    # Messaging policy
    # ------------------------------------------------------------------
    #: Meta's customer-service window. Free-form replies are only legal inside
    #: it; outside, WhatsApp needs an approved template and Messenger needs the
    #: prospect to write first.
    sales_service_window_hours: int = 24
    #: Local-time quiet hours [start, end) for proactive messages only. 21 -> 9
    #: means "nothing between 9pm and 9am". Replies to a live question are never
    #: withheld.
    sales_quiet_hours_start: int = 21
    sales_quiet_hours_end: int = 9
    sales_default_timezone: str = "Africa/Cairo"
    sales_max_outbound_per_lead_per_day: int = 1
    sales_max_outbound_per_lead_total: int = 5

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------
    #: How often the beat schedule drains the outbound queue and ticks cadences.
    outbound_tick_seconds: int = 300
    sequence_tick_seconds: int = 900
    outbound_batch_size: int = 50
    sequence_batch_size: int = 100

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    @property
    def sales_chat_deployment(self) -> str:
        return self.azure_deployment_chat


settings = Settings()
