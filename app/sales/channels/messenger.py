"""Facebook Messenger + Page adapter.

This is the acquisition side of the agent, and it is built around what Meta
actually permits:

* **Direct messages** — free-form replies inside the 24-hour window.
* **Private replies to comments** — when someone comments on a post or an ad, the
  page may send them exactly one private message. This is the legitimate
  "comment to DM" path, and it is the single most effective compliant way to turn
  ad engagement into conversations. The reply is addressed by ``comment_id``; the
  response hands back the PSID, which is what makes every later message possible.
* **Lead ads** — a ``leadgen`` webhook carries only an id, so the field values
  (name, phone, e-mail) are fetched from the Graph API. A submitted form with
  consent language is a real opt-in, which is why these leads may be followed up
  on WhatsApp while a random commenter may not.

There is deliberately no promotional path outside the 24-hour window. Message
tags exist, none of them honestly cover sales follow-up, and using one to sneak
a pitch through is how pages get restricted.
"""

import logging
from typing import Any, Optional

import httpx

from app.core.config import settings
from app.sales.channels.base import (
    KIND_COMMENT,
    KIND_LEAD_FORM,
    KIND_MESSAGE,
    KIND_STATUS,
    KIND_UNSUPPORTED,
    InboundEvent,
    graph_url,
    parse_epoch,
    parse_iso,
    prepare_outgoing,
)
from app.sales.enums import Channel
from app.sales.normalize import normalize_email, normalize_phone

logger = logging.getLogger(__name__)

#: Lead-form field names vary by form; these are the ones we map.
_LEAD_FIELD_ALIASES = {
    "full_name": {"full_name", "name", "first_name_last_name"},
    "phone": {"phone_number", "phone", "mobile_number", "whatsapp_number"},
    "email": {"email", "email_address"},
    "company_name": {"company_name", "company", "organization"},
    "job_title": {"job_title", "position", "role"},
}


class MessengerError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def is_configured() -> bool:
    return bool(settings.messenger_page_access_token and settings.messenger_page_id)


def _messaging_events(entry: dict[str, Any]) -> list[InboundEvent]:
    events: list[InboundEvent] = []
    for item in entry.get("messaging") or []:
        sender_id = str(((item or {}).get("sender") or {}).get("id") or "")
        if not sender_id:
            continue

        # Echoes are our own sends coming back; recording them would double every
        # outbound message in the transcript.
        message = item.get("message") or {}
        if message.get("is_echo"):
            continue

        if item.get("delivery") or item.get("read"):
            events.append(
                InboundEvent(
                    channel=Channel.MESSENGER,
                    kind=KIND_STATUS,
                    external_id=sender_id,
                    text="delivery" if item.get("delivery") else "read",
                    timestamp=parse_epoch(item.get("timestamp")),
                    raw=item,
                )
            )
            continue

        postback = item.get("postback") or {}
        if postback:
            title = postback.get("title") or postback.get("payload") or ""
            referral = postback.get("referral") or {}
            events.append(
                InboundEvent(
                    channel=Channel.MESSENGER,
                    kind=KIND_MESSAGE,
                    external_id=sender_id,
                    text=str(title),
                    provider_message_id=str(item.get("timestamp") or "") or None,
                    timestamp=parse_epoch(item.get("timestamp")),
                    source_detail=str(referral.get("ad_id") or referral.get("ref") or "")
                    or None,
                    source_campaign=str(referral.get("source") or "") or None,
                    raw=item,
                )
            )
            continue

        if not message:
            # A bare referral: the user opened the thread from an ad or an m.me
            # link without writing yet. Worth recording for attribution.
            referral = item.get("referral") or {}
            if referral:
                events.append(
                    InboundEvent(
                        channel=Channel.MESSENGER,
                        kind=KIND_UNSUPPORTED,
                        external_id=sender_id,
                        text="",
                        timestamp=parse_epoch(item.get("timestamp")),
                        source_detail=str(
                            referral.get("ad_id") or referral.get("ref") or ""
                        )
                        or None,
                        source_campaign=str(referral.get("source") or "") or None,
                        raw=item,
                    )
                )
            continue

        text = str(message.get("text") or "")
        kind = KIND_MESSAGE if text else KIND_UNSUPPORTED
        if not text and message.get("attachments"):
            text = "[attachment]"

        events.append(
            InboundEvent(
                channel=Channel.MESSENGER,
                kind=kind,
                external_id=sender_id,
                text=text,
                provider_message_id=str(message.get("mid") or "") or None,
                timestamp=parse_epoch(item.get("timestamp")),
                raw=item,
            )
        )
    return events


def _feed_events(entry: dict[str, Any]) -> list[InboundEvent]:
    """Comments on page posts and ads, as private-reply opportunities."""
    events: list[InboundEvent] = []
    page_id = str(entry.get("id") or "")

    for change in entry.get("changes") or []:
        field = (change or {}).get("field")
        value = (change or {}).get("value") or {}

        if field == "feed" and value.get("item") == "comment":
            if value.get("verb") not in (None, "add"):
                continue
            author = value.get("from") or {}
            author_id = str(author.get("id") or "")
            comment_id = str(value.get("comment_id") or "")
            # Never private-reply to the page's own comment.
            if not comment_id or (author_id and author_id == page_id):
                continue
            events.append(
                InboundEvent(
                    channel=Channel.MESSENGER,
                    kind=KIND_COMMENT,
                    # No PSID yet — the private reply is what mints one.
                    external_id=f"comment_author:{author_id or comment_id}",
                    text=str(value.get("message") or ""),
                    provider_message_id=f"comment:{comment_id}",
                    profile_name=str(author.get("name") or "") or None,
                    timestamp=parse_epoch(value.get("created_time")),
                    reply_to=comment_id,
                    source_detail=str(value.get("post_id") or "") or None,
                    source_campaign="facebook_comment",
                    raw=value,
                )
            )
            continue

        if field == "leadgen":
            leadgen_id = str(value.get("leadgen_id") or "")
            if not leadgen_id:
                continue
            events.append(
                InboundEvent(
                    channel=Channel.MESSENGER,
                    kind=KIND_LEAD_FORM,
                    external_id=f"leadgen:{leadgen_id}",
                    text="",
                    provider_message_id=f"leadgen:{leadgen_id}",
                    timestamp=parse_epoch(value.get("created_time")),
                    source_detail=str(
                        value.get("ad_id") or value.get("form_id") or leadgen_id
                    ),
                    source_campaign="facebook_lead_ad",
                    raw=value,
                )
            )

    return events


def parse_webhook(payload: dict[str, Any]) -> list[InboundEvent]:
    """Normalise a Messenger/Page webhook body into events."""
    events: list[InboundEvent] = []
    for entry in payload.get("entry") or []:
        entry = entry or {}
        events.extend(_messaging_events(entry))
        events.extend(_feed_events(entry))
    return events


async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    if not is_configured():
        raise MessengerError("Messenger page credentials are not configured")

    url = graph_url(settings.meta_graph_version, path)
    params = {"access_token": settings.messenger_page_access_token}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=body, params=params)
    except httpx.HTTPError as exc:
        raise MessengerError(f"network error: {exc}") from exc

    if response.status_code >= 400:
        raise MessengerError(
            f"HTTP {response.status_code}: {response.text[:500]}",
            status_code=response.status_code,
        )
    try:
        return response.json()
    except ValueError as exc:
        raise MessengerError("non-JSON response from Graph API") from exc


async def send_text(psid: str, text: str) -> list[str]:
    """Reply inside the 24-hour window.

    ``RESPONSE`` messaging type on purpose: it is the only type that honestly
    describes answering a person who just wrote to us.
    """
    message_ids: list[str] = []
    for part in prepare_outgoing(text, Channel.MESSENGER):
        payload = await _post(
            f"{settings.messenger_page_id}/messages",
            {
                "recipient": {"id": psid},
                "messaging_type": "RESPONSE",
                "message": {"text": part},
            },
        )
        message_id = payload.get("message_id")
        if message_id:
            message_ids.append(str(message_id))
    return message_ids


async def send_private_reply(comment_id: str, text: str) -> tuple[Optional[str], Optional[str]]:
    """Send the one private reply a comment entitles us to.

    Returns ``(message_id, psid)``. The PSID is the payoff: it is how this person
    becomes addressable for the rest of the conversation, so callers must store
    it as a lead identity.

    Only the first part of a long reply is sent — Meta allows a single private
    reply per comment, and a second call is rejected. The prompt keeps first
    touches short for exactly this reason.
    """
    parts = prepare_outgoing(text, Channel.MESSENGER)
    if not parts:
        return None, None

    payload = await _post(
        f"{settings.messenger_page_id}/messages",
        {
            "recipient": {"comment_id": comment_id},
            "message": {"text": parts[0]},
        },
    )
    message_id = payload.get("message_id")
    psid = payload.get("recipient_id")
    return (
        str(message_id) if message_id else None,
        str(psid) if psid else None,
    )


async def fetch_lead_form_data(leadgen_id: str) -> dict[str, Any]:
    """Read a submitted lead form.

    The webhook carries only an id; the answers must be fetched. Returns a dict
    with the normalised contact fields plus ``created_time`` and ``raw_fields``.
    """
    if not is_configured():
        raise MessengerError("Messenger page credentials are not configured")

    url = graph_url(settings.meta_graph_version, leadgen_id)
    params = {
        "access_token": settings.messenger_page_access_token,
        "fields": "id,created_time,field_data,ad_id,ad_name,campaign_name,form_id",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise MessengerError(f"network error: {exc}") from exc

    if response.status_code >= 400:
        raise MessengerError(
            f"HTTP {response.status_code}: {response.text[:300]}",
            status_code=response.status_code,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise MessengerError("non-JSON lead form response") from exc

    return parse_lead_form(payload)


def parse_lead_form(payload: dict[str, Any]) -> dict[str, Any]:
    """Map raw ``field_data`` onto our lead fields.

    Split out from the fetch so it can be tested against real payload shapes
    without a network call.
    """
    raw_fields: dict[str, str] = {}
    for field in payload.get("field_data") or []:
        name = str((field or {}).get("name") or "").strip().lower()
        values = (field or {}).get("values") or []
        if name and values:
            raw_fields[name] = str(values[0])

    def pick(target: str) -> Optional[str]:
        for key in _LEAD_FIELD_ALIASES.get(target, set()):
            if raw_fields.get(key):
                return raw_fields[key]
        return None

    return {
        "leadgen_id": str(payload.get("id") or ""),
        "created_time": parse_iso(payload.get("created_time")),
        "full_name": pick("full_name"),
        "phone": normalize_phone(pick("phone")),
        "email": normalize_email(pick("email")),
        "company_name": pick("company_name"),
        "job_title": pick("job_title"),
        "ad_id": str(payload.get("ad_id") or "") or None,
        "campaign_name": str(payload.get("campaign_name") or payload.get("ad_name") or "")
        or None,
        "form_id": str(payload.get("form_id") or "") or None,
        "raw_fields": raw_fields,
    }
