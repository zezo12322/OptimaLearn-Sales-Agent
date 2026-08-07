"""Deterministic lead scoring and stage transitions.

The model runs the conversation; it does not decide how good a lead is. Scoring
lives here, in plain arithmetic, for three reasons: a rep can be shown exactly
why a lead sits at 78, the number does not drift when the prompt is edited, and
routing rules built on top of it stay stable.

The scorer also reports what is still unknown, which is what the agent uses to
pick its next question instead of interrogating everybody with the same script.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from app.sales.enums import Segment, Stage

MAX_SCORE = 100

#: Answers we try to collect, in the order a natural conversation reaches them.
#: ``label`` is shown to the model as the thing to find out next.
QUALIFICATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("need", "what they are trying to solve or learn"),
    ("segment", "whether they are buying for themselves or for a team"),
    ("seats", "how many people need access (team buyers)"),
    ("timeline", "when they want to start"),
    ("authority", "whether they decide or need someone else to approve"),
    ("budget_range", "the budget they have in mind"),
    ("contact", "a phone number or e-mail to send details to"),
)

TIMELINE_POINTS = {"IMMEDIATE": 15, "THIS_MONTH": 13, "THIS_QUARTER": 10, "EXPLORING": 3}
AUTHORITY_POINTS = {"DECISION_MAKER": 15, "INFLUENCER": 8, "END_USER": 3}

#: Score at or above which a lead is worth a human's time.
QUALIFIED_SCORE_THRESHOLD = 70

#: Stages that scoring must never overwrite — they are set by an explicit
#: action (a booked demo, a closed deal, a rep's judgement).
STAGE_FLOOR = frozenset(
    {
        Stage.DEMO_BOOKED,
        Stage.PROPOSAL_SENT,
        Stage.WON,
        Stage.LOST,
        Stage.UNQUALIFIED,
        Stage.NURTURE,
    }
)


@dataclass(frozen=True)
class LeadSignals:
    """Flattened view of a lead used for scoring."""

    segment: Segment = Segment.UNKNOWN
    has_name: bool = False
    has_contact: bool = False
    company_size: Optional[int] = None
    job_title: Optional[str] = None
    qualification: dict[str, Any] = field(default_factory=dict)
    inbound_message_count: int = 0


@dataclass(frozen=True)
class ScoreResult:
    score: int
    breakdown: dict[str, int]
    #: Qualification keys still unanswered, in ask-order.
    missing_fields: list[str]
    #: Human-readable prompt for the agent's next question.
    next_question_hint: Optional[str]


def _as_int(value: Any) -> Optional[int]:
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    return True


def score_lead(signals: LeadSignals) -> ScoreResult:
    """Score a lead 0-100 with a per-signal breakdown."""
    q = signals.qualification or {}
    breakdown: dict[str, int] = {}

    identity = 0
    if signals.has_name:
        identity += 5
    if signals.has_contact:
        identity += 10
    breakdown["identity"] = identity

    if signals.segment is Segment.B2B:
        breakdown["segment"] = 15
    elif signals.segment is Segment.B2C:
        breakdown["segment"] = 8
    else:
        breakdown["segment"] = 0

    breakdown["need"] = 15 if _truthy(q.get("need")) else 0

    authority = str(q.get("authority") or "").upper()
    if authority in AUTHORITY_POINTS:
        breakdown["authority"] = AUTHORITY_POINTS[authority]
    elif _truthy(signals.job_title):
        # A stated job title is weak evidence of authority — better than nothing,
        # not as good as the prospect telling us who signs.
        breakdown["authority"] = 4
    else:
        breakdown["authority"] = 0

    if q.get("budget_confirmed") is True:
        breakdown["budget"] = 15
    elif _truthy(q.get("budget_range")):
        breakdown["budget"] = 8
    else:
        breakdown["budget"] = 0

    timeline = str(q.get("timeline") or "").upper()
    breakdown["timeline"] = TIMELINE_POINTS.get(timeline, 0)

    seats = _as_int(q.get("seats")) or signals.company_size
    if seats is None or seats <= 0:
        breakdown["seats"] = 0
    elif seats >= 50:
        breakdown["seats"] = 10
    elif seats >= 10:
        breakdown["seats"] = 6
    else:
        breakdown["seats"] = 3

    if signals.inbound_message_count >= 6:
        breakdown["engagement"] = 5
    elif signals.inbound_message_count >= 3:
        breakdown["engagement"] = 3
    else:
        breakdown["engagement"] = 0

    score = min(MAX_SCORE, sum(breakdown.values()))

    missing = _missing_fields(signals, q)
    hint = None
    for key, label in QUALIFICATION_FIELDS:
        if key in missing:
            hint = label
            break

    return ScoreResult(
        score=score,
        breakdown=breakdown,
        missing_fields=missing,
        next_question_hint=hint,
    )


def _missing_fields(signals: LeadSignals, q: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for key, _label in QUALIFICATION_FIELDS:
        if key == "segment":
            if signals.segment is Segment.UNKNOWN:
                missing.append(key)
            continue
        if key == "contact":
            if not signals.has_contact:
                missing.append(key)
            continue
        if key == "seats":
            # Only relevant once we know it is a team purchase.
            if signals.segment is Segment.B2B and not (
                _truthy(q.get("seats")) or signals.company_size
            ):
                missing.append(key)
            continue
        if key == "budget_range":
            if not (_truthy(q.get("budget_range")) or q.get("budget_confirmed")):
                missing.append(key)
            continue
        if not _truthy(q.get(key)):
            missing.append(key)
    return missing


def derive_stage(
    current_stage: Stage,
    score_result: ScoreResult,
    signals: LeadSignals,
    disqualified: bool = False,
) -> Stage:
    """Advance the pipeline stage without ever moving a lead backwards.

    Stages a human or an explicit action put the lead in (demo booked, won,
    lost, nurture) are floors: scoring can add information but it cannot undo a
    decision somebody already made.
    """
    if disqualified:
        return Stage.UNQUALIFIED
    if current_stage in STAGE_FLOOR:
        return current_stage

    q = signals.qualification or {}
    has_need = _truthy(q.get("need"))
    has_intent = _truthy(q.get("timeline")) or _truthy(q.get("budget_range")) or (
        q.get("budget_confirmed") is True
    )

    if score_result.score >= QUALIFIED_SCORE_THRESHOLD and has_need and has_intent:
        return Stage.QUALIFIED
    if any(_truthy(q.get(key)) for key, _ in QUALIFICATION_FIELDS if key in q):
        return Stage.QUALIFYING
    if signals.inbound_message_count > 0:
        return Stage.ENGAGED
    return Stage.NEW


def signals_from_lead(lead: Any, inbound_message_count: int = 0) -> LeadSignals:
    """Build :class:`LeadSignals` from a ``Lead`` row.

    Takes ``Any`` so the scorer never imports the ORM — keeps this module and
    its tests free of a database dependency.
    """
    segment_value = getattr(lead, "segment", None) or Segment.UNKNOWN.value
    try:
        segment = Segment(segment_value)
    except ValueError:
        segment = Segment.UNKNOWN

    return LeadSignals(
        segment=segment,
        has_name=bool(getattr(lead, "full_name", None)),
        has_contact=bool(
            getattr(lead, "phone_e164", None) or getattr(lead, "email", None)
        ),
        company_size=getattr(lead, "company_size", None),
        job_title=getattr(lead, "job_title", None),
        qualification=dict(getattr(lead, "qualification", None) or {}),
        inbound_message_count=inbound_message_count,
    )
