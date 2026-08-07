"""Read-only client for the LMS, so the agent quotes live data.

A sales agent that answers from a static price list goes stale the first time
marketing changes a tier. Courses and subscription tiers are therefore fetched
from the NestJS API at answer time.

Failures are non-fatal by design: if the LMS is slow or down the agent still
replies, just without catalogue detail, and says it will confirm. Silence beats
a fabricated price.
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class LmsUnavailable(Exception):
    """The LMS could not be reached or answered with an error."""


@dataclass
class Course:
    id: str
    title: str
    description: Optional[str]
    price: Optional[int]
    currency: str
    category: Optional[str]
    level: Optional[str]
    avg_rating: Optional[float]
    url: Optional[str]


@dataclass
class SubscriptionTier:
    id: str
    name: str
    price: int
    billing_cycle: str
    features: list[str]
    currency: str = "EGP"


def _api_base() -> str:
    if not settings.lms_api_base_url:
        raise LmsUnavailable("LMS_API_BASE_URL is not configured")
    return settings.lms_api_base_url.rstrip("/")


def web_url(path: str) -> Optional[str]:
    """Absolute public URL, or ``None`` when the site root is unconfigured."""
    if not settings.sales_web_base_url:
        return None
    return f"{settings.sales_web_base_url.rstrip('/')}/{path.lstrip('/')}"


def course_url(course_id: str) -> Optional[str]:
    return web_url(f"catalog/{quote(str(course_id), safe='')}")


def pricing_url() -> Optional[str]:
    return web_url("pricing")


def signup_url() -> Optional[str]:
    return web_url("register")


async def _get(path: str, internal: bool = False) -> Any:
    headers: dict[str, str] = {"Accept": "application/json"}
    if internal:
        if not settings.lms_internal_api_key:
            raise LmsUnavailable("LMS_INTERNAL_API_KEY is not configured")
        headers["X-Internal-Api-Key"] = settings.lms_internal_api_key

    url = f"{_api_base()}{path}"
    try:
        async with httpx.AsyncClient(
            timeout=settings.lms_request_timeout_seconds
        ) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise LmsUnavailable(f"GET {path} failed: {exc}") from exc

    if response.status_code >= 400:
        raise LmsUnavailable(f"GET {path} returned {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise LmsUnavailable(f"GET {path} returned non-JSON body") from exc


def _as_list(payload: Any) -> list[dict[str, Any]]:
    """Unwrap the shapes the LMS uses for collections."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("courses", "items", "data", "tiers", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _first(row: dict[str, Any], *keys: str) -> Any:
    """First present, non-null value among ``keys``.

    The catalogue mixes naming conventions (Data Connect camelCase alongside
    REST snake_case); reading both keeps this client from breaking on either.
    """
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _to_course(row: dict[str, Any]) -> Course:
    course_id = str(_first(row, "id", "courseId") or "")
    category = _first(row, "category", "categoryName")
    if isinstance(category, dict):
        category = category.get("name")
    price = _first(row, "price", "priceCents")
    try:
        price_int = int(price) if price is not None else None
    except (TypeError, ValueError):
        price_int = None
    rating = _first(row, "avgRating", "avg_rating", "rating")
    try:
        rating_float = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        rating_float = None

    return Course(
        id=course_id,
        title=str(_first(row, "title", "name") or "Untitled"),
        description=_first(row, "shortDescription", "description", "summary"),
        price=price_int,
        currency=str(_first(row, "currency") or "EGP"),
        category=category if isinstance(category, str) else None,
        level=_first(row, "level", "difficulty"),
        avg_rating=rating_float,
        url=course_url(course_id) if course_id else None,
    )


async def list_published_courses() -> list[Course]:
    """Published catalogue. Public endpoint — no internal key needed."""
    payload = await _get("/catalog/courses")
    return [_to_course(row) for row in _as_list(payload)]


async def list_subscription_tiers() -> list[SubscriptionTier]:
    """Active subscription tiers, via the service-to-service pricing endpoint."""
    payload = await _get("/sales/internal/pricing", internal=True)
    tiers: list[SubscriptionTier] = []
    for row in _as_list(payload):
        raw_price = _first(row, "price", "amount")
        try:
            price = int(raw_price) if raw_price is not None else 0
        except (TypeError, ValueError):
            price = 0
        features = _first(row, "features") or []
        tiers.append(
            SubscriptionTier(
                id=str(_first(row, "id") or ""),
                name=str(_first(row, "name") or "Plan"),
                price=price,
                billing_cycle=str(_first(row, "billingCycle", "billing_cycle") or ""),
                features=[str(f) for f in features if f is not None],
                currency=str(_first(row, "currency") or "EGP"),
            )
        )
    return tiers


def search_courses_locally(
    courses: list[Course], query: str, limit: int = 5
) -> list[Course]:
    """Rank the catalogue against a free-text query.

    Keyword scoring rather than embeddings: the catalogue is small enough that a
    term match over title/category/description is both accurate and instant, and
    it avoids an embedding round-trip on every mid-conversation lookup.
    """
    terms = [t for t in query.lower().split() if len(t) > 1]
    if not terms:
        return courses[:limit]

    scored: list[tuple[int, Course]] = []
    for course in courses:
        haystack_title = (course.title or "").lower()
        haystack_rest = " ".join(
            filter(None, [course.description or "", course.category or "", course.level or ""])
        ).lower()
        score = 0
        for term in terms:
            if term in haystack_title:
                score += 3
            elif term in haystack_rest:
                score += 1
        if score:
            scored.append((score, course))

    scored.sort(key=lambda pair: (-pair[0], pair[1].title))
    return [course for _score, course in scored[:limit]]


def quote_for_seats(
    tier: SubscriptionTier, seats: int, volume_discount_bands: Optional[dict[int, float]] = None
) -> dict[str, Any]:
    """Arithmetic for a team quote — never a negotiated price.

    Discount bands are configuration, not the model's invention: the agent
    presents list price times seats and any standing volume band, and anything
    beyond that is escalated to a human.
    """
    seats = max(1, int(seats))
    bands = volume_discount_bands or {}
    discount = 0.0
    for threshold in sorted(bands):
        if seats >= threshold:
            discount = bands[threshold]

    list_total = tier.price * seats
    discounted = int(round(list_total * (1 - discount)))
    return {
        "tier": tier.name,
        "seats": seats,
        "unit_price": tier.price,
        "currency": tier.currency,
        "billing_cycle": tier.billing_cycle,
        "list_total": list_total,
        "volume_discount_pct": round(discount * 100, 2),
        "total": discounted,
        "is_indicative": True,
    }
