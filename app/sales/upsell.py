"""Cross-sell: the service an existing client would genuinely need next.

The gate is deterministic and conservative; only the wording is generated. That
split matters more here than anywhere else in this service, because the audience
is *clients*, not leads. A badly-timed pitch to a lead costs a lead; a badly-timed
pitch to someone who has already paid costs the relationship.

So the rules are: nothing until the work they bought has actually been delivered,
nothing they already own, one suggestion per cooldown window, and no suggestion at
all when the pairing does not make obvious sense.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.sales import UpsellRecommendation
from app.sales import offerings
from app.sales.agent import draft_text
from app.sales.enums import UpsellOutcome
from app.sales.prompts import CROSS_SELL_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

#: What each package naturally leads to, and why. Ordered by how obvious the
#: pairing is; the first entry the client does not already own wins.
#:
#: These are deliberately not "the most expensive thing next". A client who just
#: got a website needs somewhere to manage its content before they need an AI
#: assistant, and suggesting otherwise reads as revenue-chasing.
NEXT_STEP: dict[str, tuple[tuple[str, str], ...]] = {
    "marketing-site": (
        (
            "cms-dashboard",
            "they now have a site whose content and records they still manage by hand",
        ),
        ("ai-automation", "repetitive enquiries from the new site can be automated"),
    ),
    "cms-dashboard": (
        ("ai-automation", "the data is now in one place, so it can drive automation"),
        ("marketing-site", "the internal system exists but the public face does not"),
    ),
    "ai-automation": (
        (
            "cms-dashboard",
            "automation is running but there is no place for the team to see and manage it",
        ),
    ),
    "workspace-setup": (
        ("marketing-site", "the team is set up internally but has no public presence"),
        ("cms-dashboard", "files are organised but records are still in spreadsheets"),
    ),
}

REASON_NEXT_STEP = "NATURAL_NEXT_STEP"

#: Written fallbacks, used when generation fails. Never state a price.
FALLBACK_PITCH = {
    "ar": "بعد اللي خلصناه معاكم، الخطوة اللي بعدها عادةً {service}. تحب نتكلم فيها؟",
    "en": "After what we delivered for you, the usual next step is {service}. "
    "Want to talk it through?",
}


@dataclass
class ClientSignals:
    """What we know about an existing client's engagement so far."""

    #: Stable client identifier — the lead id, or the customer e-mail from orders.
    client_ref: str
    locale: str = "ar"
    #: Package slugs they have already paid for.
    purchased_slugs: list[str] = field(default_factory=list)
    #: True once the most recent purchase has actually been handed over. Nothing
    #: is suggested before then: selling the next thing while the current thing
    #: is unfinished is how trust is lost.
    latest_delivered: bool = False
    days_since_last_purchase: int = 0
    #: Set when the client themselves asked about something else.
    asked_about_slug: Optional[str] = None


def choose_next_service(signals: ClientSignals) -> Optional[offerings.Service]:
    """The service to suggest, or ``None`` for "say nothing".

    ``None`` is the right answer most of the time, and the ordering of these
    guards is the policy: an explicit request from the client beats our own
    ranking, and an undelivered project beats everything.
    """
    owned = {slug for slug in signals.purchased_slugs if slug}

    # They asked — that outranks any ranking of ours, even mid-project.
    if signals.asked_about_slug and signals.asked_about_slug not in owned:
        return offerings.get(signals.asked_about_slug)

    if not owned:
        # Not a client yet; the normal lead conversation handles this.
        return None
    if not signals.latest_delivered:
        return None

    # Let the work settle before suggesting more of it.
    if signals.days_since_last_purchase < settings.cross_sell_min_days_after_delivery:
        return None

    for slug in signals.purchased_slugs:
        for candidate, _why in NEXT_STEP.get(slug, ()):
            if candidate not in owned:
                return offerings.get(candidate)
    return None


def _why(signals: ClientSignals, candidate: str) -> str:
    for slug in signals.purchased_slugs:
        for option, reason in NEXT_STEP.get(slug, ()):
            if option == candidate:
                return reason
    return "they asked about it"


async def _recent_recommendation(
    db: AsyncSession, tenant_id: uuid.UUID, client_ref: str
) -> Optional[UpsellRecommendation]:
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=max(0, settings.sales_upsell_cooldown_days)
    )
    result = await db.execute(
        select(UpsellRecommendation)
        .where(
            UpsellRecommendation.tenant_id == tenant_id,
            UpsellRecommendation.lms_user_id == client_ref,
            UpsellRecommendation.created_at >= cutoff,
        )
        .order_by(UpsellRecommendation.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _build_user_prompt(
    signals: ClientSignals, service: offerings.Service, why: str
) -> str:
    locale = "ar" if signals.locale.startswith("ar") else "en"
    owned_titles = [
        s.title(locale) for s in (offerings.get(x) for x in signals.purchased_slugs) if s
    ]
    lines = [
        f"Client language: {locale}",
        f"Already delivered: {', '.join(owned_titles) or 'unknown'}",
        f"Days since last purchase: {signals.days_since_last_purchase}",
        f"Why this is the next step: {why}",
        "",
        f"Service to suggest: {service.title(locale)}",
        f"Starting price: {offerings.format_price(service.price_egp, locale)} "
        f"(a starting point, not a quote)",
        "It includes: " + "; ".join(service.features(locale)[:4]),
    ]
    return "\n".join(lines)


async def recommend(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    signals: ClientSignals,
    force: bool = False,
) -> Optional[UpsellRecommendation]:
    """Produce and persist a cross-sell suggestion, or ``None`` to stay quiet."""
    service = choose_next_service(signals)
    if service is None:
        return None

    if not force:
        recent = await _recent_recommendation(db, tenant_id, signals.client_ref)
        if recent is not None:
            # Inside the cooldown: reuse the last suggestion rather than
            # generating a fresh nudge.
            return recent

    locale = "en" if signals.locale.lower().startswith("en") else "ar"
    why = _why(signals, service.slug)
    pitch = await draft_text(
        CROSS_SELL_SYSTEM_PROMPT.format(company_name=settings.sales_company_name),
        _build_user_prompt(signals, service, why),
        max_tokens=220,
    )
    if not pitch:
        pitch = FALLBACK_PITCH[locale].format(service=service.title(locale))

    recommendation = UpsellRecommendation(
        tenant_id=tenant_id,
        lms_user_id=signals.client_ref,
        recommended_tier_id=service.slug,
        recommended_tier_name=service.title(locale),
        reason_code=REASON_NEXT_STEP,
        pitch=pitch,
        locale=locale,
        signals={
            "purchased_slugs": signals.purchased_slugs,
            "latest_delivered": signals.latest_delivered,
            "days_since_last_purchase": signals.days_since_last_purchase,
            "asked_about_slug": signals.asked_about_slug,
            "why": why,
        },
        model=settings.sales_chat_deployment,
        outcome=UpsellOutcome.SHOWN.value,
        outcome_at=datetime.now(timezone.utc),
    )
    db.add(recommendation)
    await db.flush()
    return recommendation


async def record_outcome(
    db: AsyncSession, recommendation_id: uuid.UUID, outcome: str
) -> Optional[UpsellRecommendation]:
    """Close the loop so conversion of these suggestions is measurable."""
    try:
        parsed = UpsellOutcome(outcome.upper())
    except ValueError:
        return None

    recommendation = await db.get(UpsellRecommendation, recommendation_id)
    if recommendation is None:
        return None
    recommendation.outcome = parsed.value
    recommendation.outcome_at = datetime.now(timezone.utc)
    return recommendation


def to_payload(recommendation: UpsellRecommendation) -> dict[str, Any]:
    locale = recommendation.locale or "ar"
    return {
        "id": str(recommendation.id),
        "service_slug": recommendation.recommended_tier_id,
        "service_title": recommendation.recommended_tier_name,
        "reason_code": recommendation.reason_code,
        "pitch": recommendation.pitch,
        "locale": locale,
        "checkout_url": offerings.checkout_url(
            locale, recommendation.recommended_tier_id or None
        ),
        "booking_url": offerings.booking_url(locale, "consultation"),
        "created_at": recommendation.created_at.isoformat()
        if recommendation.created_at
        else None,
    }
