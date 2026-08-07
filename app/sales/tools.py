"""The agent's tools: the only way it learns a fact or changes the world.

Two rules shape this module.

*Read tools return data, never prose.* Whatever comes back is handed to the
model as JSON so the answer is composed from real values instead of remembered
ones. When a lookup fails, the tool says so explicitly (``"available": false``)
rather than returning an empty list the model might read as "we have nothing".

*Write tools are narrow.* ``save_lead_details`` cannot set the stage or the
score — those are derived — and it never overwrites a known value with null.
The model can add information; it cannot quietly erase it.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.embeddings import EmbeddingError, embed_texts
from app.core.config import settings
from app.models.sales import Lead
from app.sales import lms
from app.sales.enums import Audience, Channel, DocType, EventType, Segment
from app.sales.kb import retrieve_sales_chunks
from app.sales.normalize import clean_text, coerce_int, normalize_email, normalize_phone
from app.sales.qualification import AUTHORITY_POINTS, TIMELINE_POINTS

logger = logging.getLogger(__name__)

#: Free-text qualification keys the model may write into ``lead.qualification``.
#: An allowlist, so a hallucinated key never lands in the CRM.
QUALIFICATION_TEXT_KEYS = frozenset(
    {"need", "budget_range", "interests", "objections", "current_solution", "use_case"}
)

BILLING_CYCLES = ("MONTHLY", "QUARTERLY", "YEARLY")


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
    #: Set by book_demo.
    demo: Optional[dict[str, Any]] = None
    #: Every call and its arguments, stored on the reply for auditing.
    call_log: list[dict[str, Any]] = field(default_factory=list)


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": (
                "Search the company's own sales material: what the platform "
                "does, pricing policy, FAQs, prepared answers to objections, "
                "case studies. Use this before answering any factual question "
                "about the company or the product."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "What to look up, as a self-contained phrase. "
                            "English or Arabic both work."
                        ),
                    },
                    "doc_types": {
                        "type": "array",
                        "description": "Optional filter on document kind.",
                        "items": {
                            "type": "string",
                            "enum": [d.value for d in DocType],
                        },
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
            "name": "search_courses",
            "description": (
                "Search the live published course catalogue. Use for 'do you "
                "have a course about X' and to recommend specific courses."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Topic, skill or course name to look for.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum courses to return (1-5).",
                        "minimum": 1,
                        "maximum": 5,
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
            "name": "get_pricing",
            "description": (
                "Get the live subscription plans. Pass seats for a team to also "
                "get an indicative total. The total is list price arithmetic, "
                "not a negotiated offer — say so when you share it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seats": {
                        "type": "integer",
                        "description": "How many people need access, if known.",
                        "minimum": 1,
                    },
                    "billing_cycle": {
                        "type": "string",
                        "description": "Filter to one billing cycle.",
                        "enum": list(BILLING_CYCLES),
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
                        "description": "As the prospect wrote it; it gets normalised.",
                    },
                    "company_name": {"type": "string"},
                    "company_size": {
                        "type": "integer",
                        "description": "Total employees at the company.",
                        "minimum": 1,
                    },
                    "job_title": {"type": "string"},
                    "segment": {
                        "type": "string",
                        "description": (
                            "B2B when buying for a team or company, B2C when "
                            "buying for themselves."
                        ),
                        "enum": ["B2B", "B2C"],
                    },
                    "need": {
                        "type": "string",
                        "description": "What they are trying to solve, in their words.",
                    },
                    "seats": {
                        "type": "integer",
                        "description": "People who need access (may differ from company size).",
                        "minimum": 1,
                    },
                    "timeline": {
                        "type": "string",
                        "enum": list(TIMELINE_POINTS.keys()),
                    },
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
                        "description": "Topics or courses they showed interest in.",
                    },
                    "objections": {
                        "type": "string",
                        "description": "Concerns they raised, so a human can prepare.",
                    },
                    "marketing_opt_in": {
                        "type": "boolean",
                        "description": (
                            "True ONLY if they explicitly agreed to be "
                            "contacted with follow-ups. Never infer it."
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
            "name": "book_demo",
            "description": (
                "Record that the prospect wants a demo or a call, and get the "
                "booking link to share if one is configured."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "preferred_time": {
                        "type": "string",
                        "description": "When they said they are free, in their words.",
                    },
                    "contact_method": {
                        "type": "string",
                        "enum": ["PHONE", "WHATSAPP", "VIDEO_CALL", "EMAIL"],
                    },
                    "notes": {
                        "type": "string",
                        "description": "Anything the rep should know before the call.",
                    },
                },
                "required": ["preferred_time", "contact_method"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_human_handoff",
            "description": (
                "Hand this conversation to a person. Use for discounts, "
                "contracts, invoices, complaints, refunds, legal or privacy "
                "requests, anything you cannot ground, and any explicit request "
                "to talk to a human."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": [
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


async def _tool_search_courses(
    ctx: SalesToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    query = clean_text(args.get("query"), max_length=200) or ""
    limit = coerce_int(args.get("limit"), minimum=1, maximum=5) or 3

    try:
        courses = await lms.list_published_courses()
    except lms.LmsUnavailable as exc:
        logger.warning("Course catalogue lookup failed: %s", exc)
        return {
            "available": False,
            "courses": [],
            "note": (
                "The catalogue is temporarily unreachable. Do not name courses "
                "from memory; offer to send the list shortly."
            ),
        }

    matches = lms.search_courses_locally(courses, query, limit=limit)
    return {
        "available": True,
        "total_published": len(courses),
        "courses": [
            {
                "title": course.title,
                "category": course.category,
                "level": course.level,
                "price": course.price,
                "currency": course.currency,
                "rating": course.avg_rating,
                "url": course.url,
                "description": (course.description or "")[:280] or None,
            }
            for course in matches
        ],
        "note": None
        if matches
        else "No published course matches that topic. Say so honestly.",
    }


async def _tool_get_pricing(
    ctx: SalesToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    try:
        tiers = await lms.list_subscription_tiers()
    except lms.LmsUnavailable as exc:
        logger.warning("Pricing lookup failed: %s", exc)
        return {
            "available": False,
            "plans": [],
            "note": (
                "Live pricing is unreachable. Do not quote any price. Offer to "
                "send exact pricing and call request_human_handoff."
            ),
        }

    cycle = args.get("billing_cycle")
    if isinstance(cycle, str) and cycle.upper() in BILLING_CYCLES:
        wanted = cycle.upper()
        tiers = [t for t in tiers if (t.billing_cycle or "").upper() == wanted] or tiers

    seats = coerce_int(args.get("seats"), minimum=1, maximum=100_000)
    quotes = []
    if seats and seats > 1:
        quotes = [
            lms.quote_for_seats(
                tier, seats, volume_discount_bands=settings.sales_volume_discount_bands
            )
            for tier in tiers
        ]

    if not tiers:
        return {
            "available": True,
            "plans": [],
            "note": "No active plans are published. Hand off to a human for pricing.",
        }

    return {
        "available": True,
        "plans": [
            {
                "name": tier.name,
                "price": tier.price,
                "currency": tier.currency,
                "billing_cycle": tier.billing_cycle,
                "features": tier.features,
            }
            for tier in tiers
        ],
        "team_quotes": quotes,
        "pricing_page": lms.pricing_url(),
        "signup_page": lms.signup_url(),
        "note": (
            "Team totals are indicative list-price arithmetic. Any negotiated "
            "price must go through a human."
        )
        if quotes
        else None,
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
        contact details would make the lead's identity depend on the last
        thing the model believed.
        """
        if value in (None, "", []):
            return
        if getattr(lead, attr, None) in (None, "", 0):
            setattr(lead, attr, value)
            saved[attr] = value

    name = clean_text(args.get("full_name"), max_length=120)
    set_if_new("full_name", name)

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
    company_size = coerce_int(args.get("company_size"), minimum=1, maximum=1_000_000)
    set_if_new("company_size", company_size)

    # Segment may be corrected: a prospect asking for themselves and then for
    # their team is genuinely re-segmenting, not mistyping.
    segment = args.get("segment")
    if (
        isinstance(segment, str)
        and segment.upper() in (Segment.B2B.value, Segment.B2C.value)
        and lead.segment != segment.upper()
    ):
        lead.segment = segment.upper()
        saved["segment"] = lead.segment

    qualification = dict(lead.qualification or {})

    for key in QUALIFICATION_TEXT_KEYS:
        if key in args:
            value = clean_text(args.get(key), max_length=600)
            if value:
                qualification[key] = value
                saved[key] = value

    seats = coerce_int(args.get("seats"), minimum=1, maximum=100_000)
    if seats:
        qualification["seats"] = seats
        saved["seats"] = seats

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


async def _tool_book_demo(
    ctx: SalesToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    preferred = clean_text(args.get("preferred_time"), max_length=200)
    method = str(args.get("contact_method") or "").upper()
    if method not in {"PHONE", "WHATSAPP", "VIDEO_CALL", "EMAIL"}:
        method = "WHATSAPP" if ctx.channel is Channel.WHATSAPP else "PHONE"

    ctx.demo = {
        "preferred_time": preferred,
        "contact_method": method,
        "notes": clean_text(args.get("notes"), max_length=600),
    }

    booking_url = settings.sales_booking_url
    has_contact = bool(ctx.lead.phone_e164 or ctx.lead.email)
    return {
        "recorded": True,
        "booking_url": booking_url,
        "next_step": (
            "Share the booking link and confirm the time you noted."
            if booking_url
            else "Confirm the time and tell them a colleague will reach out."
        ),
        "note": None
        if has_contact
        else "We still have no phone or e-mail — ask for one before closing.",
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
    "search_courses": _tool_search_courses,
    "get_pricing": _tool_get_pricing,
    "save_lead_details": _tool_save_lead_details,
    "book_demo": _tool_book_demo,
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
    "book_demo": EventType.DEMO_BOOKED,
    "request_human_handoff": EventType.HANDOFF_REQUESTED,
}
