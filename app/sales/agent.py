"""The conversational core: one inbound message in, one reply out.

A bounded tool-calling loop. The model may search the knowledge base, look up
the live catalogue and pricing, and record what it learns; after
``sales_max_tool_rounds`` rounds it is asked to answer with what it has. The cap
is not a performance tweak — without it a model that keeps re-searching turns a
single WhatsApp message into an unbounded bill.

Failure is expected and handled: if the model errors, the reply falls back to a
canned "a colleague will follow up" and the turn is flagged for handoff. The
worst outcome for a sales channel is silence, and the second worst is a
confident invention.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import async_client as client
from app.core.config import settings
from app.models.sales import Lead, SalesConversation
from app.sales.enums import Channel, Segment, Stage
from app.sales.prompts import (
    FALLBACK_REPLY,
    PromptContext,
    build_system_prompt,
    localized,
)
from app.sales.qualification import ScoreResult, score_lead, signals_from_lead
from app.sales.tools import (
    TOOL_SCHEMAS,
    SalesToolContext,
    execute_tool,
)

logger = logging.getLogger(__name__)

MAX_REPLY_TOKENS = 900

#: Nudge used when the tool budget runs out, so the loop always terminates with
#: something sendable instead of another tool call.
_FINAL_ANSWER_NUDGE = (
    "You have used your tool budget for this turn. Reply now using only what "
    "the tools already returned. If a fact is still missing, say you will "
    "confirm it — do not guess."
)


@dataclass
class AgentReply:
    text: str
    model: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    captured: dict[str, Any] = field(default_factory=dict)
    handoff: Optional[dict[str, Any]] = None
    booking: Optional[dict[str, Any]] = None
    #: False when the reply is the canned fallback rather than a model answer.
    ok: bool = True


def summarize_lead(lead: Lead, score: ScoreResult) -> str:
    """Compact lead brief for the system prompt.

    Only facts, never instructions: the model decides what to ask next from the
    hint, and this block just tells it what it already knows so it stops asking.
    """
    lines: list[str] = []
    lines.append(f"- Name: {lead.full_name or 'unknown'}")
    buying_for = {
        Segment.B2B.value: "a team/company",
        Segment.B2C.value: "themselves",
    }.get(lead.segment or Segment.UNKNOWN.value, "unknown")
    lines.append(f"- Buying for: {buying_for}")
    if lead.company_name:
        size = f" (~{lead.company_size} employees)" if lead.company_size else ""
        lines.append(f"- Company: {lead.company_name}{size}")
    if lead.job_title:
        lines.append(f"- Role: {lead.job_title}")
    contact = [c for c in (lead.phone_e164, lead.email) if c]
    lines.append(
        f"- Contact on file: {', '.join(contact)}" if contact else "- Contact on file: none"
    )
    lines.append(f"- Pipeline stage: {lead.stage} (score {score.score}/100)")

    qualification = lead.qualification or {}
    if qualification:
        known = ", ".join(f"{k}={v}" for k, v in sorted(qualification.items()))
        lines.append(f"- Already told us: {known}")
    else:
        lines.append("- Already told us: nothing yet")

    if lead.opt_out_at:
        lines.append("- OPTED OUT of follow-ups. Do not ask to follow up.")
    elif lead.marketing_opt_in:
        lines.append("- Agreed to receive follow-ups.")

    return "\n".join(lines)


def _tool_call_payload(tool_calls: Any) -> list[dict[str, Any]]:
    """Re-serialise the model's tool calls for the next request."""
    payload = []
    for call in tool_calls:
        payload.append(
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments or "{}",
                },
            }
        )
    return payload


def _parse_arguments(raw: Optional[str]) -> tuple[dict[str, Any], Optional[str]]:
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"arguments were not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return {}, "arguments must be a JSON object"
    return parsed, None


async def generate_reply(
    db: AsyncSession,
    lead: Lead,
    conversation: SalesConversation,
    channel: Channel,
    message: str,
    history: list[dict[str, str]],
    inbound_count: int = 0,
) -> AgentReply:
    """Run one turn of the sales conversation."""
    signals = signals_from_lead(lead, inbound_message_count=inbound_count)
    score = score_lead(signals)

    ctx = SalesToolContext(
        db=db,
        tenant_id=str(lead.tenant_id),
        lead=lead,
        channel=channel,
    )

    prompt_ctx = PromptContext(
        company_name=settings.sales_company_name,
        agent_name=settings.sales_agent_display_name,
        channel=channel,
        locale=lead.locale or "ar",
        today=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        lead_summary=summarize_lead(lead, score),
        next_question_hint=score.next_question_hint,
        can_book_calls=bool(settings.site_internal_api_key),
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(prompt_ctx)},
        *history,
        {"role": "user", "content": message},
    ]

    model = settings.sales_chat_deployment
    reply_text = ""

    try:
        rounds = max(1, settings.sales_max_tool_rounds)
        for round_index in range(rounds):
            # The final round withholds the tools so the loop cannot help but
            # terminate with something sendable.
            last_round = round_index == rounds - 1
            if last_round:
                messages.append({"role": "system", "content": _FINAL_ANSWER_NUDGE})
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=None if last_round else TOOL_SCHEMAS,
                max_completion_tokens=MAX_REPLY_TOKENS,
            )
            choice = response.choices[0].message
            tool_calls = getattr(choice, "tool_calls", None)

            if not tool_calls:
                reply_text = (choice.content or "").strip()
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": choice.content or "",
                    "tool_calls": _tool_call_payload(tool_calls),
                }
            )

            for call in tool_calls:
                args, parse_error = _parse_arguments(call.function.arguments)
                if parse_error:
                    result: dict[str, Any] = {
                        "error": parse_error,
                        "note": "Call the tool again with valid JSON arguments.",
                    }
                    ctx.call_log.append(
                        {"tool": call.function.name, "arguments": None, "ok": False}
                    )
                else:
                    result = await execute_tool(ctx, call.function.name, args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )

    except Exception:  # noqa: BLE001 - never leave a prospect without a reply
        logger.exception("Sales agent generation failed for lead %s", lead.id)
        return AgentReply(
            text=localized(FALLBACK_REPLY, lead.locale),
            model=model,
            tool_calls=ctx.call_log,
            citations=ctx.citations,
            captured=ctx.captured,
            handoff=ctx.handoff
            or {
                "reason": "CANNOT_ANSWER",
                "urgency": "HIGH",
                "summary": "Agent generation failed; needs a human reply.",
            },
            booking=ctx.booking,
            ok=False,
        )

    if not reply_text:
        reply_text = localized(FALLBACK_REPLY, lead.locale)
        return AgentReply(
            text=reply_text,
            model=model,
            tool_calls=ctx.call_log,
            citations=ctx.citations,
            captured=ctx.captured,
            handoff=ctx.handoff
            or {
                "reason": "CANNOT_ANSWER",
                "urgency": "NORMAL",
                "summary": "Model returned no text; needs a human reply.",
            },
            booking=ctx.booking,
            ok=False,
        )

    return AgentReply(
        text=reply_text,
        model=model,
        tool_calls=ctx.call_log,
        citations=ctx.citations,
        captured=ctx.captured,
        handoff=ctx.handoff,
        booking=ctx.booking,
        ok=True,
    )


async def draft_text(
    system_prompt: str, user_prompt: str, max_tokens: int = 300
) -> Optional[str]:
    """One-shot generation for outbound copy and upsell pitches.

    Returns ``None`` on failure so callers can fall back to a written template
    rather than sending an empty or half-generated message.
    """
    try:
        response = await client.chat.completions.create(
            model=settings.sales_chat_deployment,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=max_tokens,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Sales draft generation failed")
        return None
    text = (response.choices[0].message.content or "").strip()
    return text or None


def stage_of(lead: Lead) -> Stage:
    try:
        return Stage(lead.stage)
    except ValueError:
        return Stage.NEW
