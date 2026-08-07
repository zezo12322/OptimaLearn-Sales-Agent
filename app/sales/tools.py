"""The agent's tools: the only way it learns a fact or changes the world.

Two rules shape this module.

*Read tools return data, never prose.* Whatever comes back is handed to the model
as JSON so the answer is composed from real values instead of remembered ones.
When a lookup fails, the tool says so explicitly (``"available": false``) rather
than returning an empty list the model might read as "we have nothing".

*Write tools are narrow.* ``save_lead_details`` cannot set the stage or the score
— those are derived — and it never overwrites a known value with null. The model
can add information; it cannot quietly erase it.

Prices carry ``price_is_starting_point`` in every payload, because that is how
Optimatech actually sells: a starting figure, with the real scope confirmed in
writing after a discovery call. The model is told never to drop that framing.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embeddings import EmbeddingError, embed_texts
from app.core.config import settings
from app.models.sales import Lead
from app.sales import booking, offerings
from app.sales.enums import Audience, Channel, DocType, EventType, Segment
from app.sales.kb import retrieve_sales_chunks
from app.sales.normalize import clean_text, coerce_int, normalize_email, normalize_phone
from app.sales.qualification import AUTHORITY_POINTS, TIMELINE_POINTS

logger = logging.getLogger(__name__)

#: Free-text qualification keys the model may write into ``lead.qualification``.
#: An allowlist, so a hallucinated key never lands in the CRM.
QUALIFICATION_TEXT_KEYS = frozenset(
    {
        "need",
        "budget_range",
        "interests",
        "objections",
        "current_solution",
        "use_case",
        "org_type",
    }
)

#: The kinds of organisation Optimatech sells to, from the site's own ICP.
ORG_TYPES = ("SMB", "NGO", "STARTUP", "TRAINING_CENTER", "GOVERNMENT", "INDIVIDUAL")

#: How far ahead the agent will look for a free call slot.
AVAILABILITY_HORIZON_DAYS = 14


@dataclass
class SalesToolContext:
    """Everything the tools may touch for one inbound message."""

    db: AsyncSession
    tenant_id: str
    lead: Lead
    channel: Channel

    #: Knowledge-base passages used this turn, persisted with the reply.
    citations: list[dict[str, Any]] = field(default_factory=list)
    #: Facts written this turn, for the lead timeline.
    captured: dict[str, Any] = field(default_factory=dict)
    #: Set by request_human_handoff.
    handoff: Optional[dict[str, Any]] = None
    #: Set by book_call once a booking actually exists.
    booking: Optional[dict[str, Any]] = None
    #: Every call and its arguments, stored on the reply for auditing.
    call_log: list[dict[str, Any]] = field(default_factory=list)


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": (
                "Search Optimatech's own sales material: what we do, how we "
                "work, past client work, pricing policy, FAQs, prepared answers "
                "to objections. Search this before answering any factual "
                "question about the company."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "What to look up, as a self-contained phrase. "
                            "Arabic or English both work."
                        ),
                    },
                    "doc_types": {
                        "type": "array",
                        "description": "Optional filter on document kind.",
                        "items": {"type": "string", "enum": [d.value for d in DocType]},
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_services",
            "description": (
                "Optimatech's service packages with their starting prices in "
                "EGP. Use this for 'what do you do', 'how much', and to match a "
                "described need to a package. Prices are STARTING points — never "
                "present one as a final quote."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "need": {
                        "type": "string",
                        "description": (
                            "What the prospect described, in their words. Leave "
                            "empty to list everything."
                        ),
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_service_details",
            "description": (
                "Everything about one package: what is included, the starting "
                "price, and a payment link."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "enum": [service.slug for service in offerings.SERVICES],
                    }
                },
                "required": ["slug"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_call_availability",
            "description": (
                "Real free times for a call, straight from the team's calendar. "
                "Use this before offering a time — never invent availability. "
                "Omit the date to get the next few days that have room."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "call_type": {
                        "type": "string",
                        "enum": list(booking.CALL_TYPES.keys()),
                        "description": (
                            "Default to 'discovery' — it is free and short."
                        ),
                    },
                    "date": {
                        "type": "string",
                        "description": "A specific day as YYYY-MM-DD, if they named one.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_call",
            "description": (
                "Book a real call. Only call this with a time returned by "
                "check_call_availability, and only once you have a name and "
                "e-mail. The prospect gets a calendar invite and a confirmation "
                "e-mail, so do not use it to 'hold' a tentative time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "call_type": {
                        "type": "string",
                        "enum": list(booking.CALL_TYPES.keys()),
                    },
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "start_time": {"type": "string", "description": "HH:MM, 24-hour"},
                    "client_name": {"type": "string"},
                    "client_email": {"type": "string"},
                    "client_phone": {"type": "string"},
                    "notes": {
                        "type": "string",
                        "description": (
                            "What they want to discuss, so the team arrives "
                            "prepared."
                        ),
                    },
                },
                "required": [
                    "call_type",
                    "date",
                    "start_time",
                    "client_name",
                    "client_email",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_payment_link",
            "description": (
                "A payment link for a package, or for a custom deposit amount. "
                "Only send one when the prospect has asked to pay or to put down "
                "a deposit — never as a way to end a conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package_slug": {
                        "type": "string",
                        "enum": [service.slug for service in offerings.SERVICES],
                    },
                    "custom_amount_egp": {
                        "type": "integer",
                        "description": (
                            "For a deposit or an agreed custom figure, in EGP."
                        ),
                        "minimum": offerings.MIN_CUSTOM_EGP,
                        "maximum": offerings.MAX_CUSTOM_EGP,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_lead_details",
            "description": (
                "Record what you have learned about this prospect. Call it as "
                "soon as a fact appears — do not batch it to the end of the "
                "conversation. Only send fields you actually learned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "full_name": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {
                        "type": "string",
                        "description": "As they wrote it; it gets normalised.",
                    },
                    "company_name": {"type": "string"},
                    "company_size": {
                        "type": "integer",
                        "description": "People in their organisation.",
                        "minimum": 1,
                    },
                    "job_title": {"type": "string"},
                    "org_type": {
                        "type": "string",
                        "description": "What kind of organisation they are.",
                        "enum": list(ORG_TYPES),
                    },
                    "segment": {
                        "type": "string",
                        "description": (
                            "B2B when buying for an organisation, B2C for "
                            "themselves personally."
                        ),
                        "enum": ["B2B", "B2C"],
                    },
                    "need": {
                        "type": "string",
                        "description": (
                            "What they want built or fixed, in their own words."
                        ),
                    },
                    "current_solution": {
                        "type": "string",
                        "description": (
                            "What they use today — Excel, an old site, WhatsApp, "
                            "nothing."
                        ),
                    },
                    "timeline": {"type": "string", "enum": list(TIMELINE_POINTS.keys())},
                    "authority": {
                        "type": "string",
                        "description": "Their role in the decision.",
                        "enum": list(AUTHORITY_POINTS.keys()),
                    },
                    "budget_range": {
                        "type": "string",
                        "description": "Budget they stated, verbatim. Never guess one.",
                    },
                    "budget_confirmed": {
                        "type": "boolean",
                        "description": "True only if they confirmed budget is approved.",
                    },
                    "interests": {
                        "type": "string",
                        "description": "Which packages or capabilities they asked about.",
                    },
                    "objections": {
                        "type": "string",
                        "description": "Concerns they raised, so a human can prepare.",
                    },
                    "marketing_opt_in": {
                        "type": "boolean",
                        "description": (
                            "True ONLY if they explicitly agreed to be contacted "
                            "with follow-ups. Never infer it."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_human_handoff",
            "description": (
                "Hand this conversation to a person. Use for a discount or a "
                "custom quote, contracts, invoices, tenders, complaints, "
                "refunds, legal or privacy requests, anything you cannot ground, "
                "and any explicit request to talk to a human."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": [
                            "CUSTOM_QUOTE",
                            "DISCOUNT_REQUEST",
                            "CONTRACT_OR_INVOICE",
                            "COMPLAINT",
                            "REFUND",
                            "LEGAL_OR_PRIVACY",
                            "CANNOT_ANSWER",
                            "ASKED_FOR_HUMAN",
                            "HIGH_VALUE_OPPORTUNITY",
                        ],
                    },
                    "urgency": {"type": "string", "enum": ["LOW", "NORMAL", "HIGH"]},
                    "summary": {
                        "type": "string",
                        "description": (
                            "Two or three lines a colleague can act on without "
                            "reading the whole thread."
                        ),
                    },
                },
                "required": ["reason", "urgency", "summary"],
                "additionalProperties": False,
            },
        },
    },
]

TOOL_NAMES = frozenset(
    schema["function"]["name"] for schema in TOOL_SCHEMAS  # type: ignore[index]
)


def _locale(lead: Lead) -> str:
    return "ar" if (lead.locale or "ar").startswith("ar") else "en"


async def _embed_query(query: str) -> Optional[list[float]]:
    """Embed a query off the event loop. ``None`` when embedding is unavailable."""
    try:
        vectors = await asyncio.get_event_loop().run_in_executor(
            None, embed_texts, [query]
        )
    except EmbeddingError:
        logger.exception("Sales KB query embedding failed")
        return None
    return vectors[0] if vectors else None


def _audience_for(lead: Lead) -> Audience:
    if lead.segment == Segment.B2B.value:
        return Audience.B2B
    if lead.segment == Segment.B2C.value:
        return Audience.B2C
    return Audience.ALL


async def _tool_search_knowledge_base(
    ctx: SalesToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    query = clean_text(args.get("query"), max_length=400)
    if not query:
        return {"available": True, "results": [], "note": "Empty query."}

    vector = await _embed_query(query)
    if vector is None:
        return {
            "available": False,
            "results": [],
            "note": (
                "Knowledge base is temporarily unreachable. Do not answer from "
                "memory — offer to confirm and hand off."
            ),
        }

    doc_types = args.get("doc_types")
    if isinstance(doc_types, list):
        doc_types = [d for d in doc_types if d in {t.value for t in DocType}] or None
    else:
        doc_types = None

    chunks = await retrieve_sales_chunks(
        ctx.db,
        query_vector=vector,
        tenant_id=ctx.tenant_id,
        audience=_audience_for(ctx.lead),
        top_k=settings.sales_kb_top_k,
        min_similarity=settings.sales_kb_min_similarity,
        doc_types=doc_types,
    )

    for chunk in chunks[:3]:
        citation = {
            "document_id": chunk.document_id,
            "title": chunk.document_title,
            "doc_type": chunk.doc_type,
            "similarity": round(chunk.similarity, 3),
        }
        if citation not in ctx.citations:
            ctx.citations.append(citation)

    if not chunks:
        return {
            "available": True,
            "results": [],
            "note": (
                "Nothing in our material covers this. Say you will confirm it "
                "and call request_human_handoff — do not improvise an answer."
            ),
        }

    return {
        "available": True,
        "results": [
            {
                "source": chunk.document_title,
                "type": chunk.doc_type,
                "language": chunk.locale,
                "content": chunk.text,
                "similarity": round(chunk.similarity, 3),
            }
            for chunk in chunks
        ],
    }


async def _tool_list_services(
    ctx: SalesToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    locale = _locale(ctx.lead)
    need = clean_text(args.get("need"), max_length=300) or ""
    matches = offerings.search(need, limit=4)

    return {
        "available": True,
        "matched_on": need or None,
        "services": [offerings.as_payload(service, locale) for service in matches],
        "custom_payment": offerings.custom_payment_bounds(),
        "pricing_page": offerings.pricing_url(locale),
        "note": (
            "Every figure is a STARTING price. Final scope and price are "
            "confirmed in writing after a discovery call — say so when you "
            "quote. Never negotiate; hand off instead."
        ),
    }


async def _tool_get_service_details(
    ctx: SalesToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    service = offerings.get(str(args.get("slug") or ""))
    if service is None:
        return {
            "available": False,
            "note": (
                "No such package. Call list_services and offer what actually "
                "exists."
            ),
        }
    locale = _locale(ctx.lead)
    payload = offerings.as_payload(service, locale)
    payload["available"] = True
    payload["note"] = (
        "Starting price only. Confirm scope on a call before promising anything."
    )
    return payload


def _parse_date(raw: Any) -> Optional[date]:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


async def _tool_check_call_availability(
    ctx: SalesToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    locale = _locale(ctx.lead)
    call_type = booking.normalize_call_type(args.get("call_type"))
    wanted = _parse_date(args.get("date"))

    # A date in the past is a misread, not a request; treat it as "soon".
    today = datetime.now(timezone.utc).date()
    if wanted and wanted < today:
        wanted = None

    try:
        if wanted:
            slots = await booking.available_slots(call_type, wanted)
            if not slots:
                # Do not leave the prospect at a dead end on a full day.
                days = await booking.next_available_days(
                    call_type, days=AVAILABILITY_HORIZON_DAYS
                )
                return {
                    "available": True,
                    "call_type": call_type,
                    "requested_date": wanted.isoformat(),
                    "slots": [],
                    "alternative_days": days,
                    "note": (
                        "That day is full. Offer one of the alternative days "
                        "instead of asking them to guess again."
                    ),
                }
            return {
                "available": True,
                "call_type": call_type,
                "call_title": booking.call_type_title(call_type, locale),
                "date": wanted.isoformat(),
                "slots": [
                    {"start_time": slot.start_time, "end_time": slot.end_time}
                    for slot in slots
                ],
                "note": "Offer at most three of these, then let them pick.",
            }

        days = await booking.next_available_days(
            call_type, days=AVAILABILITY_HORIZON_DAYS
        )
        return {
            "available": True,
            "call_type": call_type,
            "call_title": booking.call_type_title(call_type, locale),
            "days": days,
            "note": (
                "Offer one day with two or three times. Do not list everything."
            )
            if days
            else (
                "No free slots in the next two weeks. Say so honestly and call "
                "request_human_handoff."
            ),
        }

    except booking.BookingUnavailable as exc:
        logger.warning("Availability lookup failed: %s", exc)
        return {
            "available": False,
            "booking_url": offerings.booking_url(locale, call_type),
            "call_types": booking.describe_call_types(locale),
            "note": (
                "The calendar is unreachable, so you cannot offer a specific "
                "time. Share the booking link and let them pick — do not invent "
                "a slot."
            ),
        }


async def _tool_book_call(
    ctx: SalesToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    locale = _locale(ctx.lead)
    call_type = booking.normalize_call_type(args.get("call_type"))
    on_date = _parse_date(args.get("date"))
    start_time = clean_text(args.get("start_time"), max_length=8)
    name = clean_text(args.get("client_name"), max_length=120)
    email = normalize_email(args.get("client_email"))
    phone = normalize_phone(args.get("client_phone")) or ctx.lead.phone_e164

    missing = [
        label
        for label, value in (
            ("date", on_date),
            ("start_time", start_time),
            ("client_name", name),
            ("client_email", email),
        )
        if not value
    ]
    if missing:
        return {
            "booked": False,
            "missing": missing,
            "note": (
                "Ask for what is missing — one thing at a time — then call this "
                "again. Do not book with a placeholder."
            ),
        }

    try:
        result = await booking.create_booking(
            call_type=call_type,
            on_date=on_date,  # type: ignore[arg-type]
            start_time=str(start_time),
            client_name=str(name),
            client_email=str(email),
            client_phone=phone,
            notes=clean_text(args.get("notes"), max_length=600),
            locale=locale,
        )
    except booking.BookingUnavailable as exc:
        logger.warning("Booking failed: %s", exc)
        return {
            "booked": False,
            "booking_url": offerings.booking_url(locale, call_type),
            "error": str(exc),
            "note": (
                "The booking did not go through. Do NOT claim it did. Share the "
                "booking link and call request_human_handoff."
            ),
        }

    ctx.booking = {
        "call_type": call_type,
        "date": on_date.isoformat(),  # type: ignore[union-attr]
        "start_time": start_time,
        "booking_id": result.get("booking_id") or result.get("id"),
        "meeting_link": result.get("meeting_link"),
    }
    # Filling in the contact details we just used keeps the CRM consistent with
    # what the calendar invite says.
    if name and not ctx.lead.full_name:
        ctx.lead.full_name = name
        ctx.captured["full_name"] = name
    if email and not ctx.lead.email:
        ctx.lead.email = email
        ctx.captured["email"] = email

    return {
        "booked": True,
        "call_title": booking.call_type_title(call_type, locale),
        "date": ctx.booking["date"],
        "start_time": start_time,
        "meeting_link": result.get("meeting_link"),
        "note": (
            "Confirm the day and time back to them in one short line. A "
            "confirmation e-mail with the meeting link is already on its way, so "
            "mention that rather than repeating the link if there isn't one."
        ),
    }


async def _tool_create_payment_link(
    ctx: SalesToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    locale = _locale(ctx.lead)
    slug = clean_text(args.get("package_slug"), max_length=60)
    amount = coerce_int(
        args.get("custom_amount_egp"),
        minimum=offerings.MIN_CUSTOM_EGP,
        maximum=offerings.MAX_CUSTOM_EGP,
    )

    if slug:
        service = offerings.get(slug)
        if service is None:
            return {
                "available": False,
                "note": "No such package. Use list_services first.",
            }
        url = offerings.checkout_url(locale, service.slug)
        if not url:
            return {
                "available": False,
                "note": (
                    "The site URL is not configured, so no payment link exists. "
                    "Hand off to a human for payment details."
                ),
            }
        return {
            "available": True,
            "url": url,
            "package": service.title(locale),
            "starting_price_formatted": offerings.format_price(
                service.price_egp, locale
            ),
            "note": (
                "This charges the package's starting price. If the scope is "
                "bigger, do not send this — hand off for a proper quote."
            ),
        }

    url = offerings.checkout_url(locale)
    if not url:
        return {
            "available": False,
            "note": "The site URL is not configured. Hand off for payment details.",
        }
    if amount:
        return {
            "available": True,
            "url": url,
            "custom_amount_egp": amount,
            "note": (
                "The checkout page has a custom-amount option; tell them the "
                "figure to enter. Only use an amount a human agreed to."
            ),
        }
    return {
        "available": True,
        "url": url,
        "bounds": offerings.custom_payment_bounds(),
        "note": "They pick the package or amount on the page.",
    }


async def _tool_save_lead_details(
    ctx: SalesToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    lead = ctx.lead
    saved: dict[str, Any] = {}
    rejected: dict[str, str] = {}

    def set_if_new(attr: str, value: Any) -> None:
        """Fill a blank field; never overwrite something we already know.

        Corrections are a human's job: if a prospect really did mistype their
        e-mail, a rep fixes it in the CRM. Letting the model rewrite known
        contact details would make the lead's identity depend on the last thing
        the model believed.
        """
        if value in (None, "", []):
            return
        if getattr(lead, attr, None) in (None, "", 0):
            setattr(lead, attr, value)
            saved[attr] = value

    set_if_new("full_name", clean_text(args.get("full_name"), max_length=120))

    if args.get("email") is not None:
        email = normalize_email(args.get("email"))
        if email:
            set_if_new("email", email)
        else:
            rejected["email"] = "not a valid e-mail address"

    if args.get("phone") is not None:
        phone = normalize_phone(args.get("phone"))
        if phone:
            set_if_new("phone_e164", phone)
        else:
            rejected["phone"] = "not a valid phone number"

    set_if_new("company_name", clean_text(args.get("company_name"), max_length=160))
    set_if_new("job_title", clean_text(args.get("job_title"), max_length=120))
    set_if_new(
        "company_size",
        coerce_int(args.get("company_size"), minimum=1, maximum=1_000_000),
    )

    # Segment may be corrected: asking for themselves and then for their
    # organisation is genuinely re-segmenting, not mistyping.
    segment = args.get("segment")
    if (
        isinstance(segment, str)
        and segment.upper() in (Segment.B2B.value, Segment.B2C.value)
        and lead.segment != segment.upper()
    ):
        lead.segment = segment.upper()
        saved["segment"] = lead.segment

    qualification = dict(lead.qualification or {})

    org_type = args.get("org_type")
    if isinstance(org_type, str) and org_type.upper() in ORG_TYPES:
        qualification["org_type"] = org_type.upper()
        saved["org_type"] = org_type.upper()

    for key in QUALIFICATION_TEXT_KEYS - {"org_type"}:
        if key in args:
            value = clean_text(args.get(key), max_length=600)
            if value:
                qualification[key] = value
                saved[key] = value

    timeline = args.get("timeline")
    if isinstance(timeline, str) and timeline.upper() in TIMELINE_POINTS:
        qualification["timeline"] = timeline.upper()
        saved["timeline"] = timeline.upper()

    authority = args.get("authority")
    if isinstance(authority, str) and authority.upper() in AUTHORITY_POINTS:
        qualification["authority"] = authority.upper()
        saved["authority"] = authority.upper()

    if isinstance(args.get("budget_confirmed"), bool):
        qualification["budget_confirmed"] = args["budget_confirmed"]
        saved["budget_confirmed"] = args["budget_confirmed"]

    # Reassigning (rather than mutating) is what marks the JSONB column dirty.
    lead.qualification = qualification

    # Opt-in is consent: it may only ever be turned on, and only explicitly.
    if args.get("marketing_opt_in") is True and not lead.marketing_opt_in:
        lead.marketing_opt_in = True
        lead.opt_in_at = datetime.now(timezone.utc)
        lead.opt_in_source = f"agent_conversation:{ctx.channel.value}"
        saved["marketing_opt_in"] = True

    ctx.captured.update(saved)
    return {
        "saved": saved,
        "rejected": rejected or None,
        "note": "Do not read saved values back to the prospect; just continue."
        if saved
        else "Nothing new to save.",
    }


async def _tool_request_human_handoff(
    ctx: SalesToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    ctx.handoff = {
        "reason": str(args.get("reason") or "CANNOT_ANSWER"),
        "urgency": str(args.get("urgency") or "NORMAL").upper(),
        "summary": clean_text(args.get("summary"), max_length=900) or "",
    }
    return {
        "recorded": True,
        "next_step": (
            "Tell the prospect a colleague will follow up shortly. Give a "
            "timeframe only if you were told one. Do not keep selling."
        ),
    }


_EXECUTORS = {
    "search_knowledge_base": _tool_search_knowledge_base,
    "list_services": _tool_list_services,
    "get_service_details": _tool_get_service_details,
    "check_call_availability": _tool_check_call_availability,
    "book_call": _tool_book_call,
    "create_payment_link": _tool_create_payment_link,
    "save_lead_details": _tool_save_lead_details,
    "request_human_handoff": _tool_request_human_handoff,
}


async def execute_tool(
    ctx: SalesToolContext, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Run one tool call. Never raises — a failure is data the model can use."""
    executor = _EXECUTORS.get(name)
    if executor is None:
        return {"error": f"Unknown tool '{name}'."}

    try:
        result = await executor(ctx, args or {})
    except Exception as exc:  # noqa: BLE001 - a tool crash must not kill the reply
        logger.exception("Sales tool %s failed", name)
        result = {
            "error": f"{name} failed: {exc}",
            "note": "Do not retry more than once. Offer to confirm and hand off.",
        }

    ctx.call_log.append({"tool": name, "arguments": args, "ok": "error" not in result})
    return result


#: Tools whose side effects the caller must reconcile after the loop finishes.
EVENT_FOR_TOOL = {
    "save_lead_details": EventType.DETAILS_CAPTURED,
    "book_call": EventType.DEMO_BOOKED,
    "request_human_handoff": EventType.HANDOFF_REQUESTED,
}

#: Kept so callers that reason about the horizon do not re-derive it.
__all__ = [
    "AVAILABILITY_HORIZON_DAYS",
    "EVENT_FOR_TOOL",
    "ORG_TYPES",
    "QUALIFICATION_TEXT_KEYS",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "SalesToolContext",
    "execute_tool",
]
