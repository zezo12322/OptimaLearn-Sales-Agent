"""Booking calls, by calling the site's own booking logic.

The marketing site already computes free slots as
``availability − already booked − Google Calendar conflicts`` and, on submit,
re-verifies the slot, creates the Calendar event with a Meet link, and sends both
confirmation emails. Reimplementing that here would mean:

* duplicating slot arithmetic that must agree exactly, or the agent offers a time
  the site would reject;
* no visibility of Google Calendar conflicts, so the agent would happily book
  over a meeting that exists only in the owner's calendar;
* no Meet link and no confirmation e-mails on agent-made bookings.

So this module is a thin client for two internal endpoints on the site that wrap
the existing server actions. The agent reads real availability and creates a real
booking, and there is exactly one implementation of "is this slot free".

Bookings land as ``pending`` — the same status the public form produces — so a
human still confirms.
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Business call types. The site also has ``intern-*`` types under
#: ``category = 'interview'``; those belong to hiring and must never be offered
#: to a prospect.
CALL_TYPES: dict[str, dict[str, Any]] = {
    "discovery": {
        "title_en": "Free discovery call",
        "title_ar": "مكالمة تعارف مجانية",
        "duration_min": 20,
        "when_to_offer": (
            "First conversation. Free, short, no commitment — the default offer."
        ),
    },
    "consultation": {
        "title_en": "Technical consultation",
        "title_ar": "استشارة تقنية",
        "duration_min": 45,
        "when_to_offer": (
            "They have a specific technical question or an existing system to "
            "review."
        ),
    },
    "kickoff": {
        "title_en": "Project kickoff",
        "title_ar": "اجتماع بدء مشروع",
        "duration_min": 60,
        "when_to_offer": "Scope is agreed and they are ready to start.",
    },
}

DEFAULT_CALL_TYPE = "discovery"


class BookingUnavailable(Exception):
    """The site's booking API could not be reached or refused the request."""


@dataclass
class Slot:
    """One bookable start time, as the site reports it."""

    start_time: str  # "HH:MM"
    end_time: str  # "HH:MM"

    def label(self, locale: str = "ar") -> str:
        return (
            f"{self.start_time} – {self.end_time}"
            if not locale.startswith("ar")
            else f"من {self.start_time} لـ {self.end_time}"
        )


def call_type_title(slug: str, locale: str = "ar") -> str:
    entry = CALL_TYPES.get(slug, CALL_TYPES[DEFAULT_CALL_TYPE])
    return entry["title_ar"] if locale.startswith("ar") else entry["title_en"]


def normalize_call_type(raw: Optional[str]) -> str:
    """Map anything the model sends onto a real business call type.

    Falls back to the free discovery call rather than erroring: offering the
    cheapest, lowest-commitment call is never the wrong answer.
    """
    slug = (raw or "").strip().lower()
    return slug if slug in CALL_TYPES else DEFAULT_CALL_TYPE


def _base_url() -> str:
    if not settings.site_api_base_url:
        raise BookingUnavailable("SITE_API_BASE_URL is not configured")
    return settings.site_api_base_url.rstrip("/")


def _headers() -> dict[str, str]:
    if not settings.site_internal_api_key:
        raise BookingUnavailable("SITE_INTERNAL_API_KEY is not configured")
    return {
        "Content-Type": "application/json",
        "X-Internal-Api-Key": settings.site_internal_api_key,
    }


async def _call(method: str, path: str, payload: Optional[dict[str, Any]] = None) -> Any:
    url = f"{_base_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=settings.site_request_timeout_seconds) as client:
            response = await client.request(
                method, url, headers=_headers(), json=payload
            )
    except httpx.HTTPError as exc:
        raise BookingUnavailable(f"{method} {path} failed: {exc}") from exc

    if response.status_code >= 400:
        # The site's own error text is the useful diagnostic ("slot taken",
        # "calendar unavailable"); keep it rather than flattening it.
        raise BookingUnavailable(
            f"{method} {path} returned {response.status_code}: {response.text[:300]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise BookingUnavailable(f"{method} {path} returned non-JSON") from exc


async def available_slots(
    call_type: str, on_date: date, limit: int = 6
) -> list[Slot]:
    """Free slots for a call type on a date, as the site computes them.

    ``limit`` is a presentation choice, not a data one: reading twelve times out
    loud in a WhatsApp message is worse than offering a few and asking.
    """
    payload = await _call(
        "GET",
        f"/api/internal/booking/slots?type={normalize_call_type(call_type)}"
        f"&date={on_date.isoformat()}",
    )
    slots = payload.get("slots") if isinstance(payload, dict) else None
    if not isinstance(slots, list):
        return []

    out: list[Slot] = []
    for entry in slots[:limit]:
        if isinstance(entry, dict) and entry.get("start_time"):
            out.append(
                Slot(
                    start_time=str(entry["start_time"])[:5],
                    end_time=str(entry.get("end_time") or "")[:5],
                )
            )
    return out


async def next_available_days(
    call_type: str, days: int = 10, max_days_with_slots: int = 3
) -> list[dict[str, Any]]:
    """The next few days that actually have room, with their slots.

    Asking the site day by day rather than for a range: the site's endpoint is
    per-day because its Google Calendar check is per-day, and a range endpoint
    would be a second implementation of the same thing.
    """
    payload = await _call(
        "GET",
        f"/api/internal/booking/days?type={normalize_call_type(call_type)}&days={days}",
    )
    found = payload.get("days") if isinstance(payload, dict) else None
    if not isinstance(found, list):
        return []
    return [entry for entry in found if isinstance(entry, dict)][:max_days_with_slots]


async def create_booking(
    call_type: str,
    on_date: date,
    start_time: str,
    client_name: str,
    client_email: str,
    client_phone: Optional[str] = None,
    notes: Optional[str] = None,
    locale: str = "ar",
) -> dict[str, Any]:
    """Book a slot through the site, which re-verifies it before committing.

    The re-verification is the point: between the agent listing a slot and the
    prospect choosing one, the public form may have taken it. Letting the site
    arbitrate means the agent cannot double-book.
    """
    return await _call(
        "POST",
        "/api/internal/booking/create",
        {
            "type": normalize_call_type(call_type),
            "date": on_date.isoformat(),
            "start_time": start_time,
            "client_name": client_name,
            "client_email": client_email,
            "client_phone": client_phone,
            "notes": notes,
            "locale": "ar" if locale.startswith("ar") else "en",
            "source": "sales_agent",
        },
    )


def describe_call_types(locale: str = "ar") -> list[dict[str, Any]]:
    """Call types as a tool result, including when each one is appropriate."""
    return [
        {
            "type": slug,
            "title": entry["title_ar"] if locale.startswith("ar") else entry["title_en"],
            "duration_min": entry["duration_min"],
            "when_to_offer": entry["when_to_offer"],
            "is_free": slug == "discovery",
        }
        for slug, entry in CALL_TYPES.items()
    ]
