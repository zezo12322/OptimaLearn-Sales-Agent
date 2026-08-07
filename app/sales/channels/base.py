"""Shared channel plumbing: signature checks, normalised events, formatting.

The webhook endpoints are the only public surface of this service, so signature
verification lives here and is applied before a payload is even parsed. It is a
constant-time HMAC comparison against the Meta app secret; without it anyone who
learns the URL can inject conversations and make the agent send messages.
"""

import contextlib
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.sales.enums import Channel

logger = logging.getLogger(__name__)

#: Provider message-size ceilings. We split rather than truncate.
CHANNEL_TEXT_LIMIT = {
    Channel.WHATSAPP: 4096,
    Channel.MESSENGER: 2000,
    Channel.WEB: 8000,
    Channel.EMAIL: 20000,
}

#: What a normalised inbound event represents.
KIND_MESSAGE = "MESSAGE"
KIND_COMMENT = "COMMENT"
KIND_LEAD_FORM = "LEAD_FORM"
KIND_STATUS = "STATUS"
KIND_UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class InboundEvent:
    """One thing that happened on a channel, in a provider-neutral shape."""

    channel: Channel
    kind: str
    #: Stable handle we can address on this channel (wa_id, PSID). For comments
    #: before a private reply has been sent there is no addressable handle, so
    #: this carries the commenter's page-scoped id and ``reply_to`` carries the
    #: comment id that the private-reply API needs.
    external_id: str
    text: str = ""
    provider_message_id: Optional[str] = None
    profile_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    #: Comment id for a private reply, when applicable.
    reply_to: Optional[str] = None
    #: Ad, post or form the prospect came from — the attribution that tells
    #: marketing which creative actually produces conversations.
    source_detail: Optional[str] = None
    source_campaign: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        """True when the agent should respond to this event."""
        return self.kind in (KIND_MESSAGE, KIND_COMMENT, KIND_LEAD_FORM)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form, for handing the event to a Celery worker."""
        return {
            "channel": self.channel.value,
            "kind": self.kind,
            "external_id": self.external_id,
            "text": self.text,
            "provider_message_id": self.provider_message_id,
            "profile_name": self.profile_name,
            "phone": self.phone,
            "email": self.email,
            "timestamp": self.timestamp.isoformat(),
            "reply_to": self.reply_to,
            "source_detail": self.source_detail,
            "source_campaign": self.source_campaign,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InboundEvent":
        timestamp = payload.get("timestamp")
        parsed = datetime.now(timezone.utc)
        if isinstance(timestamp, str):
            with contextlib.suppress(ValueError):
                parsed = datetime.fromisoformat(timestamp)
        return cls(
            channel=Channel(payload["channel"]),
            kind=str(payload.get("kind") or KIND_UNSUPPORTED),
            external_id=str(payload.get("external_id") or ""),
            text=str(payload.get("text") or ""),
            provider_message_id=payload.get("provider_message_id"),
            profile_name=payload.get("profile_name"),
            phone=payload.get("phone"),
            email=payload.get("email"),
            timestamp=parsed,
            reply_to=payload.get("reply_to"),
            source_detail=payload.get("source_detail"),
            source_campaign=payload.get("source_campaign"),
            raw=payload.get("raw") or {},
        )


def verify_meta_signature(
    app_secret: Optional[str], body: bytes, signature_header: Optional[str]
) -> bool:
    """Verify Meta's ``X-Hub-Signature-256`` over the raw request body.

    Returns ``False`` when the secret is unset: an unconfigured deployment must
    reject webhooks, not accept everything. Fail-closed is the only safe default
    for an endpoint that can make the agent send messages.
    """
    if not app_secret:
        logger.error("META_APP_SECRET is not configured; rejecting webhook")
        return False
    if not signature_header:
        return False

    prefix, _, provided = signature_header.partition("=")
    if prefix.strip().lower() != "sha256" or not provided:
        return False

    expected = hmac.new(
        app_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, provided.strip())


def verify_subscription(
    mode: Optional[str], token: Optional[str], expected_token: Optional[str]
) -> bool:
    """Meta's GET handshake when a webhook subscription is created."""
    if not expected_token or not token:
        return False
    return mode == "subscribe" and hmac.compare_digest(token, expected_token)


_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", flags=re.MULTILINE)
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MD_FENCE = re.compile(r"```[a-zA-Z0-9]*\n?")
_MD_BULLET = re.compile(r"^\s*[-*]\s+", flags=re.MULTILINE)


def format_for_channel(text: str, channel: Channel) -> str:
    """Render model output as the channel actually displays it.

    The prompt asks for plain text, but models reach for markdown by habit, and
    ``**bold**`` shows up literally in a WhatsApp bubble. Converting here is a
    deterministic guard that does not depend on the model complying.
    """
    if channel is Channel.WEB:
        return text.strip()

    cleaned = _MD_FENCE.sub("", text)
    cleaned = _MD_HEADING.sub("", cleaned)
    cleaned = _MD_LINK.sub(r"\1: \2", cleaned)

    if channel is Channel.WHATSAPP:
        # WhatsApp has its own emphasis syntax: single asterisks.
        cleaned = _MD_BOLD.sub(r"*\1*", cleaned)
        cleaned = _MD_BULLET.sub("• ", cleaned)
    else:
        cleaned = _MD_BOLD.sub(r"\1", cleaned)
        cleaned = _MD_BULLET.sub("• ", cleaned)

    # Collapse runs of blank lines that markdown stripping tends to leave behind.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def split_message(text: str, limit: int) -> list[str]:
    """Split an over-long message on the most natural boundary available.

    Paragraphs first, then sentences, then a hard cut. Truncation is never an
    option — a sales message that stops mid-sentence reads as a broken bot.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    buffer = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if len(candidate) <= limit:
            buffer = candidate
            continue
        if buffer:
            parts.append(buffer)
            buffer = ""
        if len(paragraph) <= limit:
            buffer = paragraph
            continue
        parts.extend(_split_sentences(paragraph, limit))
    if buffer:
        parts.append(buffer)
    return [p.strip() for p in parts if p.strip()]


def _split_sentences(paragraph: str, limit: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?؟])\s+", paragraph)
    parts: list[str] = []
    buffer = ""
    for sentence in sentences:
        candidate = f"{buffer} {sentence}".strip() if buffer else sentence
        if len(candidate) <= limit:
            buffer = candidate
            continue
        if buffer:
            parts.append(buffer)
            buffer = ""
        while len(sentence) > limit:
            parts.append(sentence[:limit])
            sentence = sentence[limit:]
        buffer = sentence
    if buffer:
        parts.append(buffer)
    return parts


def prepare_outgoing(text: str, channel: Channel) -> list[str]:
    """Format then split. What every sender calls before hitting the provider."""
    limit = CHANNEL_TEXT_LIMIT.get(channel, 2000)
    return split_message(format_for_channel(text, channel), limit)


def graph_url(version: str, path: str) -> str:
    return f"https://graph.facebook.com/{version.strip('/')}/{path.lstrip('/')}"


def parse_epoch(value: Any) -> datetime:
    """Provider timestamps arrive as epoch seconds, sometimes as strings."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def parse_iso(value: Any) -> datetime:
    """ISO-8601 with a trailing ``Z``, as the Graph API returns for lead forms."""
    if not isinstance(value, str) or not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
