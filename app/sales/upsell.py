"""In-product upgrade suggestions driven by real usage.

The decision of *whether* to suggest anything is deterministic and conservative;
only the wording is generated. That split matters: a nag loop is the fastest way
to make people resent a paywall, so the gate has to be auditable and the same
learner cannot be asked twice inside the cooldown.

Learners who already pay are left alone unless they actually hit a limit. A
suggestion nobody asked for is an interruption, and interrupting a paying
customer to sell them something is worse than saying nothing.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.sales import UpsellRecommendation
from app.sales import lms
from app.sales.agent import draft_text
from app.sales.enums import UpsellOutcome
from app.sales.prompts import UPSELL_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

#: Reason codes, strongest signal first. The first one that matches wins.
REASON_HIT_LIMIT = "HIT_FREE_LIMIT"
REASON_HIGH_COMPLETION = "HIGH_COMPLETION"
REASON_ENGAGED = "NEW_AND_ENGAGED"

#: Written fallbacks, used when generation fails. Never mention a price.
FALLBACK_PITCH = {
    "ar": {
        REASON_HIT_LIMIT: "المحتوى اللي بتحاول توصله متاح في باقة {tier}. تحب تشوف تفاصيلها؟",
        REASON_HIGH_COMPLETION: "خلّصت كورس كامل — مبروك! باقة {tier} تفتحلك باقي المسار. تشوفها؟",
        REASON_ENGAGED: "ماشي بمعدل حلو. باقة {tier} فيها محتوى أوسع لو حابب تكمل. تشوفها؟",
    },
    "en": {
        REASON_HIT_LIMIT: "The content you're opening is part of the {tier} plan. Want to see what's in it?",
        REASON_HIGH_COMPLETION: "You finished a whole course — nice work. {tier} opens up the rest of the track. Take a look?",
        REASON_ENGAGED: "You're on a good run. {tier} covers more ground if you want to keep going. Take a look?",
    },
}


@dataclass
class UsageSignals:
    """What the LMS knows about how this learner uses the platform."""

    lms_user_id: str
    locale: str = "ar"
    #: Current paid plan name; ``None`` means free.
    current_plan: Optional[str] = None
    enrolled_courses: int = 0
    completed_courses: int = 0
    completed_lectures: int = 0
    quiz_pass_rate: Optional[float] = None
    days_active: int = 0
    #: True when they just tried to open something their plan does not include.
    hit_limit: bool = False


def choose_reason(signals: UsageSignals) -> Optional[str]:
    """Decide whether to suggest an upgrade, and on what grounds.

    Returns ``None`` for "say nothing", which is the correct answer most of the
    time. Ordered by how much the learner has actually demonstrated: hitting a
    limit is a request for the upgrade, finishing a course is earned momentum,
    and steady watching is a weak-but-real signal.
    """
    if signals.hit_limit:
        return REASON_HIT_LIMIT

    # A paying learner who has not hit a limit gets left alone.
    if signals.current_plan:
        return None

    if signals.completed_courses >= 1:
        return REASON_HIGH_COMPLETION
    if signals.completed_lectures >= 5:
        return REASON_ENGAGED
    return None


def pick_tier(
    tiers: list[lms.SubscriptionTier], signals: UsageSignals
) -> Optional[lms.SubscriptionTier]:
    """The next plan up. Cheapest for a free learner, next price for a payer.

    Suggesting the top plan to someone on the free tier reads as a shakedown;
    the smallest real step is the one people take.
    """
    priced = sorted((t for t in tiers if t.price > 0), key=lambda t: t.price)
    if not priced:
        return None
    if not signals.current_plan:
        return priced[0]

    current = next(
        (t for t in priced if t.name.lower() == signals.current_plan.lower()), None
    )
    if current is None:
        return priced[0]
    higher = [t for t in priced if t.price > current.price]
    return higher[0] if higher else None


async def _recent_recommendation(
    db: AsyncSession, tenant_id: uuid.UUID, lms_user_id: str
) -> Optional[UpsellRecommendation]:
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=max(0, settings.sales_upsell_cooldown_days)
    )
    result = await db.execute(
        select(UpsellRecommendation)
        .where(
            UpsellRecommendation.tenant_id == tenant_id,
            UpsellRecommendation.lms_user_id == lms_user_id,
            UpsellRecommendation.created_at >= cutoff,
        )
        .order_by(UpsellRecommendation.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _build_user_prompt(
    signals: UsageSignals, tier: lms.SubscriptionTier, reason: str
) -> str:
    lines = [
        f"Learner language: {signals.locale}",
        f"Trigger: {reason}",
        f"Current plan: {signals.current_plan or 'free'}",
        f"Courses enrolled: {signals.enrolled_courses}",
        f"Courses completed: {signals.completed_courses}",
        f"Lectures completed: {signals.completed_lectures}",
        f"Days active: {signals.days_active}",
    ]
    if signals.quiz_pass_rate is not None:
        lines.append(f"Quiz pass rate: {round(signals.quiz_pass_rate * 100)}%")
    lines.append("")
    lines.append(f"Plan to suggest: {tier.name}")
    lines.append(f"Plan price: {tier.price} {tier.currency} / {tier.billing_cycle}")
    if tier.features:
        lines.append("Plan includes: " + "; ".join(tier.features[:6]))
    return "\n".join(lines)


async def recommend(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    signals: UsageSignals,
    force: bool = False,
) -> Optional[UpsellRecommendation]:
    """Produce and persist an upgrade suggestion, or ``None`` to stay quiet."""
    reason = choose_reason(signals)
    if reason is None:
        return None

    if not force:
        recent = await _recent_recommendation(db, tenant_id, signals.lms_user_id)
        if recent is not None:
            # Inside the cooldown: reuse the last suggestion rather than
            # generating a fresh nag.
            return recent

    try:
        tiers = await lms.list_subscription_tiers()
    except lms.LmsUnavailable as exc:
        logger.info("Upsell skipped, pricing unavailable: %s", exc)
        return None

    tier = pick_tier(tiers, signals)
    if tier is None:
        return None

    locale = "en" if signals.locale.lower().startswith("en") else "ar"
    pitch = await draft_text(
        UPSELL_SYSTEM_PROMPT.format(company_name=settings.sales_company_name),
        _build_user_prompt(signals, tier, reason),
        max_tokens=200,
    )
    if not pitch:
        pitch = FALLBACK_PITCH[locale][reason].format(tier=tier.name)

    recommendation = UpsellRecommendation(
        tenant_id=tenant_id,
        lms_user_id=signals.lms_user_id,
        recommended_tier_id=tier.id or None,
        recommended_tier_name=tier.name,
        reason_code=reason,
        pitch=pitch,
        locale=locale,
        signals={
            "current_plan": signals.current_plan,
            "enrolled_courses": signals.enrolled_courses,
            "completed_courses": signals.completed_courses,
            "completed_lectures": signals.completed_lectures,
            "quiz_pass_rate": signals.quiz_pass_rate,
            "days_active": signals.days_active,
            "hit_limit": signals.hit_limit,
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
    return {
        "id": str(recommendation.id),
        "tier_id": recommendation.recommended_tier_id,
        "tier_name": recommendation.recommended_tier_name,
        "reason_code": recommendation.reason_code,
        "pitch": recommendation.pitch,
        "locale": recommendation.locale,
        "pricing_url": lms.pricing_url(),
        "created_at": recommendation.created_at.isoformat()
        if recommendation.created_at
        else None,
    }
