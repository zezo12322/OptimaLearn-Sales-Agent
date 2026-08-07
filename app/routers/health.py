"""Liveness and readiness.

``/v1/health`` is unauthenticated and answers without touching anything, so a
load balancer can use it. ``/v1/ready`` reports which integrations are actually
configured — the fastest way to answer "why isn't it replying on WhatsApp?".
"""

from fastapi import APIRouter, Depends

from app.core.auth import verify_internal_key
from app.core.config import settings
from app.sales.channels import messenger, whatsapp

router = APIRouter(tags=["health"])


@router.get("/v1/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/v1/ready", dependencies=[Depends(verify_internal_key)])
async def ready() -> dict[str, object]:
    return {
        "status": "ok",
        "agent_enabled": settings.sales_agent_enabled,
        "integrations": {
            "whatsapp": whatsapp.is_configured(),
            "messenger": messenger.is_configured(),
            "meta_signature_verification": bool(settings.meta_app_secret),
            "lms_api": bool(settings.lms_api_base_url),
            "lms_internal_key": bool(settings.lms_internal_api_key),
            "web_links": bool(settings.sales_web_base_url),
            "booking_url": bool(settings.sales_booking_url),
            "lead_form_template": bool(settings.sales_lead_form_template_name),
            "reengage_template": bool(settings.sales_reengage_template_name),
        },
        "policy": {
            "service_window_hours": settings.sales_service_window_hours,
            "quiet_hours": [
                settings.sales_quiet_hours_start,
                settings.sales_quiet_hours_end,
            ],
            "timezone": settings.sales_default_timezone,
            "max_outbound_per_day": settings.sales_max_outbound_per_lead_per_day,
            "max_outbound_total": settings.sales_max_outbound_per_lead_total,
        },
    }
