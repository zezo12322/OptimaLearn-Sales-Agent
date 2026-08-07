"""Messaging policy: may we send this, right now, to this person?

This is the module that keeps the WhatsApp number and the Facebook page alive.
Meta does not police intent, it polices behaviour — unsolicited messages, sends
outside the customer-service window and free-form promotional content all lead
to quality-rating drops and then to a block. Every proactive send is therefore
routed through :func:`evaluate_outbound` first, and the verdict is persisted on
the queue row so a refusal is as auditable as a delivery.

Deliberately pure: no database, no HTTP, no settings import. Callers assemble a
:class:`LeadMessagingState` and a :class:`MessagingPolicyConfig` and get a
verdict back. That makes every rule directly unit-testable, which matters more
here than anywhere else in the service.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.sales.enums import TERMINAL_STAGES, Channel, Stage, StrEnum

#: Words that mean "stop messaging me", in Egyptian Arabic, MSA and English.
#: Matched on the whole normalised message, not as a substring, so "stop asking
#: me questions" is not read as an opt-out.
OPT_OUT_KEYWORDS = frozenset(
    {
        "stop",
        "unsubscribe",
        "remove me",
        "opt out",
        "optout",
        "الغاء",
        "إلغاء",
        "الغاء الاشتراك",
        "إلغاء الاشتراك",
        "توقف",
        "كفايه",
        "كفاية",
        "بلاش",
        "لا تراسلني",
        "متراسلنيش",
        "مش عايز",
        "مش عاوز",
        "ازالة",
        "إزالة",
    }
)

#: Re-subscribe keywords, so an opt-out is reversible by the lead (and only by
#: the lead).
OPT_IN_KEYWORDS = frozenset({"start", "subscribe", "ابدأ", "ابدا", "اشتراك", "موافق"})


class Decision(StrEnum):
    #: Plain text is allowed — the service window is open.
    ALLOW_FREEFORM = "ALLOW_FREEFORM"
    #: Only a pre-approved template may go out.
    ALLOW_TEMPLATE = "ALLOW_TEMPLATE"
    #: Legal, but not now. ``retry_at`` says when to try again.
    DEFER = "DEFER"
    #: Never send this.
    DENY = "DENY"


@dataclass(frozen=True)
class MessagingPolicyConfig:
    service_window_hours: int = 24
    quiet_hours_start: int = 21
    quiet_hours_end: int = 9
    default_timezone: str = "Africa/Cairo"
    max_per_lead_per_day: int = 1
    max_per_lead_total: int = 5


@dataclass(frozen=True)
class LeadMessagingState:
    """Everything the policy needs to know about a lead, and nothing more."""

    channel: Channel
    stage: Stage = Stage.NEW
    opted_out: bool = False
    marketing_opt_in: bool = False
    human_takeover: bool = False
    last_inbound_at: Optional[datetime] = None
    last_outbound_at: Optional[datetime] = None
    lead_timezone: Optional[str] = None
    #: Proactive messages already sent today (lead-local day) and ever.
    sent_today: int = 0
    sent_total: int = 0


@dataclass(frozen=True)
class PolicyVerdict:
    decision: Decision
    #: Stable machine code, stored on the queue row and used in tests.
    reason: str
    #: One-line explanation for the CRM timeline.
    detail: str
    retry_at: Optional[datetime] = None
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision in (Decision.ALLOW_FREEFORM, Decision.ALLOW_TEMPLATE)

    @property
    def requires_template(self) -> bool:
        return self.decision is Decision.ALLOW_TEMPLATE


def _zone(name: Optional[str], fallback: str) -> ZoneInfo:
    for candidate in (name, fallback, "UTC"):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo("UTC")


def is_quiet_hour(local_hour: int, start: int, end: int) -> bool:
    """Quiet-hours membership, handling windows that wrap past midnight."""
    if start == end:
        return False
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end


def _next_allowed_after_quiet_hours(
    now: datetime, zone: ZoneInfo, end_hour: int
) -> datetime:
    """First instant at or after ``now`` that falls outside quiet hours."""
    local = now.astimezone(zone)
    candidate = local.replace(hour=end_hour % 24, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _next_local_midnight(now: datetime, zone: ZoneInfo) -> datetime:
    local = now.astimezone(zone)
    tomorrow = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return tomorrow.astimezone(timezone.utc)


def service_window_open(
    state: LeadMessagingState, config: MessagingPolicyConfig, now: datetime
) -> bool:
    """True when the lead wrote to us recently enough for free-form replies."""
    if state.last_inbound_at is None:
        return False
    last_inbound = state.last_inbound_at
    if last_inbound.tzinfo is None:
        last_inbound = last_inbound.replace(tzinfo=timezone.utc)
    return now - last_inbound < timedelta(hours=config.service_window_hours)


def evaluate_outbound(
    state: LeadMessagingState,
    config: MessagingPolicyConfig,
    now: Optional[datetime] = None,
) -> PolicyVerdict:
    """Decide whether a *proactive* message may go out.

    Order matters: hard prohibitions are checked before timing ones so a lead
    who opted out is never merely deferred, and the recorded reason is always
    the most fundamental one.
    """
    now = now or datetime.now(timezone.utc)
    zone = _zone(state.lead_timezone, config.default_timezone)

    if state.opted_out:
        return PolicyVerdict(
            Decision.DENY,
            "OPTED_OUT",
            "Lead asked us to stop. Never contact again from automation.",
        )

    if state.human_takeover:
        return PolicyVerdict(
            Decision.DENY,
            "HUMAN_TAKEOVER",
            "A human owns this thread; automation must not talk over them.",
        )

    if state.stage in TERMINAL_STAGES:
        return PolicyVerdict(
            Decision.DENY,
            "TERMINAL_STAGE",
            f"Lead is in terminal stage {state.stage.value}.",
        )

    # Lawful basis: either they wrote to us, or they explicitly opted in
    # (form submission, "yes send me details"). Nothing else counts.
    if not state.marketing_opt_in and state.last_inbound_at is None:
        return PolicyVerdict(
            Decision.DENY,
            "NO_CONSENT",
            "No inbound message and no opt-in — cold messaging is not allowed.",
        )

    if config.max_per_lead_total and state.sent_total >= config.max_per_lead_total:
        return PolicyVerdict(
            Decision.DENY,
            "TOTAL_CAP",
            f"Lifetime outbound cap of {config.max_per_lead_total} reached.",
        )

    if config.max_per_lead_per_day and state.sent_today >= config.max_per_lead_per_day:
        return PolicyVerdict(
            Decision.DEFER,
            "DAILY_CAP",
            f"Daily outbound cap of {config.max_per_lead_per_day} reached.",
            retry_at=_next_local_midnight(now, zone),
        )

    local_hour = now.astimezone(zone).hour
    if is_quiet_hour(local_hour, config.quiet_hours_start, config.quiet_hours_end):
        return PolicyVerdict(
            Decision.DEFER,
            "QUIET_HOURS",
            f"Local time is {local_hour:02d}:00 — inside quiet hours.",
            retry_at=_next_allowed_after_quiet_hours(
                now, zone, config.quiet_hours_end
            ),
        )

    if service_window_open(state, config, now):
        return PolicyVerdict(
            Decision.ALLOW_FREEFORM,
            "WINDOW_OPEN",
            f"Lead wrote within the last {config.service_window_hours}h.",
        )

    # Window closed. What is possible now depends on the platform.
    if state.channel is Channel.WHATSAPP:
        return PolicyVerdict(
            Decision.ALLOW_TEMPLATE,
            "WINDOW_CLOSED_TEMPLATE_ONLY",
            "Outside the 24h window: only an approved template may be sent.",
        )

    if state.channel is Channel.MESSENGER:
        # Messenger has no template equivalent for promotional content. Message
        # tags exist but none of them legitimately cover sales follow-up, so we
        # wait for the prospect instead of risking the page.
        return PolicyVerdict(
            Decision.DENY,
            "MESSENGER_WINDOW_CLOSED",
            "Outside Messenger's 24h window there is no compliant way to "
            "follow up; wait for the prospect to write again.",
        )

    # WEB / EMAIL are not governed by a Meta service window.
    return PolicyVerdict(
        Decision.ALLOW_FREEFORM,
        "NOT_WINDOW_GOVERNED",
        f"{state.channel.value} has no platform service window.",
    )


def may_auto_reply(
    state: LeadMessagingState, agent_enabled: bool = True
) -> PolicyVerdict:
    """Decide whether the agent may answer an inbound message.

    Answering someone who just wrote is always inside the service window, so
    neither quiet hours nor send caps apply — throttling a reply to a live
    question would be worse service, not better compliance.
    """
    if not agent_enabled:
        return PolicyVerdict(
            Decision.DENY, "AGENT_DISABLED", "Sales agent is switched off."
        )
    if state.human_takeover:
        return PolicyVerdict(
            Decision.DENY,
            "HUMAN_TAKEOVER",
            "A human is handling this conversation.",
        )
    if state.opted_out:
        return PolicyVerdict(
            Decision.DENY,
            "OPTED_OUT",
            "Lead opted out; log the message but do not reply automatically.",
        )
    return PolicyVerdict(
        Decision.ALLOW_FREEFORM, "INBOUND_REPLY", "Replying inside the service window."
    )


def evaluate_private_reply(
    state: LeadMessagingState, agent_enabled: bool = True
) -> PolicyVerdict:
    """Decide whether to private-reply to a comment on a post or ad.

    A comment is an invitation: Meta permits the page exactly one private reply,
    and the person chose to engage publicly. So consent, service window and quiet
    hours do not apply — but an opt-out still does, and a human already in the
    thread still wins.
    """
    if not agent_enabled:
        return PolicyVerdict(
            Decision.DENY, "AGENT_DISABLED", "Sales agent is switched off."
        )
    if state.opted_out:
        return PolicyVerdict(
            Decision.DENY,
            "OPTED_OUT",
            "This person asked us to stop; do not private-reply.",
        )
    if state.human_takeover:
        return PolicyVerdict(
            Decision.DENY,
            "HUMAN_TAKEOVER",
            "A human owns this lead; let them answer the comment.",
        )
    return PolicyVerdict(
        Decision.ALLOW_FREEFORM,
        "COMMENT_PRIVATE_REPLY",
        "One private reply is permitted in response to a public comment.",
    )


def _normalise(message: str) -> str:
    """Fold a message down to a comparable form.

    Strips punctuation and Arabic diacritics, and normalises the alef/ya/ta-marbuta
    variants that make Arabic keyword matching unreliable otherwise.
    """
    text = message.strip().lower()
    # Arabic diacritics (harakat) and tatweel carry no lexical meaning here.
    text = "".join(ch for ch in text if not ("ً" <= ch <= "ْ") and ch != "ـ")
    for src, dst in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي"), ("ة", "ه")):
        text = text.replace(src, dst)
    text = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)
    return " ".join(text.split())


def _matches_keyword(message: str, keywords: frozenset[str]) -> bool:
    normalised = _normalise(message)
    if not normalised:
        return False
    return any(normalised == _normalise(kw) for kw in keywords)


def detect_opt_out(message: str) -> bool:
    """True when the whole message is an opt-out request.

    Whole-message matching on purpose: a lead writing "stop sending me the same
    pdf, send the pricing instead" is asking for pricing, not unsubscribing.
    """
    return _matches_keyword(message, OPT_OUT_KEYWORDS)


def detect_opt_in(message: str) -> bool:
    return _matches_keyword(message, OPT_IN_KEYWORDS)
