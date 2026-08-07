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

    sales_company_name: str = "Optimatech"
    sales_agent_display_name: str = "Nour"
    #: Where handoff notifications go. Empty disables notification.
    sales_handoff_notify_email: str | None = None

    #: Cap on tool-calling rounds per reply. Without it, a model that keeps
    #: re-searching turns one WhatsApp message into an unbounded bill.
    sales_max_tool_rounds: int = 4
    #: Turns of history handed to the model (each side counts as one).
    sales_history_messages: int = 12
    sales_kb_top_k: int = 6
    sales_kb_min_similarity: float = 0.20
    #: Days before the same client may be shown another cross-sell suggestion.
    sales_upsell_cooldown_days: int = 30
    #: Quiet period after a delivery before suggesting the next service. Selling
    #: the next thing while the current thing is still settling reads as
    #: revenue-chasing, and it is the fastest way to lose a client's trust.
    cross_sell_min_days_after_delivery: int = 14

    # ------------------------------------------------------------------
    # Optimatech site integration
    # ------------------------------------------------------------------
    #: Marketing-site origin, used to build /book, /checkout and pricing links.
    site_base_url: str | None = None
    #: Same origin normally; separate so the internal API can sit behind a
    #: different hostname than the public site if it ever needs to.
    site_api_base_url: str | None = None
    #: Shared secret for the site's /api/internal/* endpoints (booking slots and
    #: creation). Without it the agent shares a booking link instead of booking.
    site_internal_api_key: str | None = None
    site_request_timeout_seconds: float = 10.0

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
    #: Stop proactive contact once a lead has been silent this long. WhatsApp's
    #: marketing rules forbid templating long-dormant contacts. 0 disables it.
    sales_max_silence_days: int = 30

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
