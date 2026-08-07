"""WhatsApp Cloud API adapter.

Two things here carry most of the business value.

**Referral capture.** A message that arrives from a click-to-WhatsApp ad carries
a ``referral`` block with the ad and headline that produced it. Recording that on
the lead is what turns "we spent on ads" into "this creative produced 40
conversations and 6 qualified leads".

**Template sending.** Outside the 24-hour service window, a template is the only
legal way to reach someone. The sender refuses to fake it with free text, and
surfaces the provider's error verbatim so an unapproved or mis-parameterised
template is obvious instead of silently dropped.
"""

import logging
from typing import Any, Optional

import httpx

from app.core.config import settings
from app.sales.channels.base import (
    KIND_MESSAGE,
    KIND_STATUS,
    KIND_UNSUPPORTED,
    InboundEvent,
    graph_url,
    parse_epoch,
    prepare_outgoing,
)
from app.sales.enums import Channel
from app.sales.normalize import normalize_phone

logger = logging.getLogger(__name__)

#: Interactive payloads we can read as text.
_TEXTUAL_TYPES = {"text", "button", "interactive"}


class WhatsAppError(Exception):
    """The Cloud API rejected a send."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def is_configured() -> bool:
    return bool(settings.whatsapp_access_token and settings.whatsapp_phone_number_id)


def _extract_text(message: dict[str, Any]) -> tuple[str, str]:
    """Return ``(text, kind)`` for one WhatsApp message object."""
    msg_type = message.get("type")

    if msg_type == "text":
        return str((message.get("text") or {}).get("body") or ""), KIND_MESSAGE

    if msg_type == "button":
        return str((message.get("button") or {}).get("text") or ""), KIND_MESSAGE

    if msg_type == "interactive":
        interactive = message.get("interactive") or {}
        for key in ("button_reply", "list_reply"):
            reply = interactive.get(key) or {}
            title = reply.get("title") or reply.get("id")
            if title:
                return str(title), KIND_MESSAGE
        return "", KIND_UNSUPPORTED

    # Media, location, contacts, stickers. We acknowledge rather than pretend to
    # have read them — a reply about the wrong thing is worse than admitting the
    # attachment was not processed.
    if msg_type in {"image", "video", "audio", "document", "voice"}:
        caption = (message.get(msg_type) or {}).get("caption")
        if caption:
            return str(caption), KIND_MESSAGE
        return f"[{msg_type}]", KIND_UNSUPPORTED

    return f"[{msg_type or 'unknown'}]", KIND_UNSUPPORTED


def _referral(message: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """``(source_detail, source_campaign)`` from a click-to-WhatsApp referral."""
    referral = message.get("referral") or {}
    if not referral:
        return None, None
    detail = (
        referral.get("source_id")
        or referral.get("ctwa_clid")
        or referral.get("source_url")
    )
    campaign = referral.get("headline") or referral.get("source_type")
    return (str(detail) if detail else None, str(campaign) if campaign else None)


def parse_webhook(payload: dict[str, Any]) -> list[InboundEvent]:
    """Normalise a Cloud API webhook body into events.

    Tolerant by design: an unrecognised entry is skipped rather than raising,
    because a 500 makes Meta retry the whole batch and eventually disable the
    subscription.
    """
    events: list[InboundEvent] = []

    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            value = (change or {}).get("value") or {}

            # Profile names arrive alongside, keyed by wa_id.
            contacts: dict[str, str] = {}
            for contact in value.get("contacts") or []:
                wa_id = (contact or {}).get("wa_id")
                name = ((contact or {}).get("profile") or {}).get("name")
                if wa_id and name:
                    contacts[str(wa_id)] = str(name)

            for message in value.get("messages") or []:
                wa_id = str(message.get("from") or "")
                if not wa_id:
                    continue
                text, kind = _extract_text(message)
                detail, campaign = _referral(message)
                events.append(
                    InboundEvent(
                        channel=Channel.WHATSAPP,
                        kind=kind,
                        external_id=wa_id,
                        text=text,
                        provider_message_id=str(message.get("id") or "") or None,
                        profile_name=contacts.get(wa_id),
                        phone=normalize_phone(wa_id),
                        timestamp=parse_epoch(message.get("timestamp")),
                        source_detail=detail,
                        source_campaign=campaign,
                        raw=message,
                    )
                )

            for status in value.get("statuses") or []:
                recipient = str(status.get("recipient_id") or "")
                if not recipient:
                    continue
                events.append(
                    InboundEvent(
                        channel=Channel.WHATSAPP,
                        kind=KIND_STATUS,
                        external_id=recipient,
                        text=str(status.get("status") or ""),
                        provider_message_id=str(status.get("id") or "") or None,
                        timestamp=parse_epoch(status.get("timestamp")),
                        raw=status,
                    )
                )

    return events


async def _post(body: dict[str, Any]) -> dict[str, Any]:
    if not is_configured():
        raise WhatsAppError("WhatsApp Cloud API is not configured")

    url = graph_url(
        settings.meta_graph_version, f"{settings.whatsapp_phone_number_id}/messages"
    )
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise WhatsAppError(f"network error: {exc}") from exc

    if response.status_code >= 400:
        # Meta's error body names the actual cause (template not approved,
        # number not opted in, window closed). Keep it: it is the only useful
        # diagnostic and it lands on the outbound row.
        raise WhatsAppError(
            f"HTTP {response.status_code}: {response.text[:500]}",
            status_code=response.status_code,
        )
    try:
        return response.json()
    except ValueError as exc:
        raise WhatsAppError("non-JSON response from Cloud API") from exc


def _first_message_id(payload: dict[str, Any]) -> Optional[str]:
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        first = messages[0]
        if isinstance(first, dict) and first.get("id"):
            return str(first["id"])
    return None


async def send_text(to: str, text: str) -> list[str]:
    """Send free-form text. Only legal inside the 24-hour service window.

    Returns provider message ids, one per part, since a long reply is split
    rather than truncated.
    """
    message_ids: list[str] = []
    for part in prepare_outgoing(text, Channel.WHATSAPP):
        payload = await _post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                # Link previews are noise in a sales thread and can expose an
                # unrelated page title.
                "text": {"preview_url": False, "body": part},
            }
        )
        message_id = _first_message_id(payload)
        if message_id:
            message_ids.append(message_id)
    return message_ids


async def send_template(
    to: str,
    template_name: str,
    language: Optional[str] = None,
    body_params: Optional[list[str]] = None,
) -> Optional[str]:
    """Send a pre-approved template — the only way to reopen a closed window."""
    components = []
    if body_params:
        components.append(
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": str(value)} for value in body_params
                ],
            }
        )

    payload = await _post(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {
                    "code": language or settings.whatsapp_template_language
                },
                **({"components": components} if components else {}),
            },
        }
    )
    return _first_message_id(payload)
