"""Meta webhooks — the only public surface of this service.

Three rules govern everything here.

**Verify before parsing.** The raw body is HMAC-checked against the app secret
before it is even decoded. Without that, anyone who learns the URL can invent
conversations and make the agent send messages.

**Always answer 200.** Meta retries non-2xx responses and disables a subscription
that keeps failing. A payload we cannot handle is acknowledged and logged, never
turned into a 500.

**Never work inline.** Events are handed to Celery and the handler returns
immediately. Meta's timeout is short, and one slow model call must not cost us
the whole batch.
"""

import logging
from typing import Any

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.sales.channels import messenger, whatsapp
from app.sales.channels.base import verify_meta_signature, verify_subscription
from app.worker.tasks import handle_inbound_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sales/webhooks", tags=["webhooks"])

def _ack(**extra: Any) -> Response:
    """Meta only cares that we answered 2xx; the body is for our own logs."""
    return JSONResponse({"status": "received", **extra}, status_code=status.HTTP_200_OK)


def _challenge(request: Request, expected_token: str | None) -> Response:
    """Answer Meta's GET verification handshake."""
    params = request.query_params
    if verify_subscription(
        params.get("hub.mode"), params.get("hub.verify_token"), expected_token
    ):
        return Response(
            content=params.get("hub.challenge") or "",
            media_type="text/plain",
            status_code=status.HTTP_200_OK,
        )
    logger.warning("Webhook verification failed for %s", request.url.path)
    return Response(status_code=status.HTTP_403_FORBIDDEN)


async def _authenticated_payload(request: Request) -> dict[str, Any] | None:
    """Verify the signature and decode the body, or ``None`` if either fails."""
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_meta_signature(settings.meta_app_secret, body, signature):
        return None
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is not our problem to fix
        logger.warning("Webhook body was not valid JSON")
        return {}
    return payload if isinstance(payload, dict) else {}


def _dispatch(events: list[Any]) -> int:
    """Queue actionable events. Returns how many were queued."""
    queued = 0
    for event in events:
        # Status receipts are cheap but still go through the worker so the whole
        # inbound path has one implementation and one retry policy.
        try:
            handle_inbound_event.delay(
                str(settings.default_tenant_id), event.to_dict()
            )
            queued += 1
        except Exception:  # noqa: BLE001 - broker hiccup must not fail the webhook
            logger.exception("Could not queue inbound event")
    return queued


@router.get("/whatsapp")
async def verify_whatsapp(request: Request) -> Response:
    return _challenge(request, settings.whatsapp_verify_token)


@router.post("/whatsapp")
async def receive_whatsapp(request: Request) -> Response:
    payload = await _authenticated_payload(request)
    if payload is None:
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    if not settings.sales_agent_enabled:
        return _ack(queued=0, note="agent disabled")

    try:
        events = whatsapp.parse_webhook(payload)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to parse WhatsApp webhook")
        return _ack(queued=0)
    return _ack(queued=_dispatch(events))


@router.get("/messenger")
async def verify_messenger(request: Request) -> Response:
    return _challenge(request, settings.messenger_verify_token)


@router.post("/messenger")
async def receive_messenger(request: Request) -> Response:
    payload = await _authenticated_payload(request)
    if payload is None:
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    if not settings.sales_agent_enabled:
        return _ack(queued=0, note="agent disabled")

    try:
        events = messenger.parse_webhook(payload)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to parse Messenger webhook")
        return _ack(queued=0)
    return _ack(queued=_dispatch(events))
