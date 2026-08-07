"""Deterministic checks applied to an agent reply.

Every scorer here is a pure function of the turn's result. No model grades
another model: an LLM judge would add its own failure mode on top of the one
being measured, and these particular rules — did a price carry its framing, did
a discount request escalate, did an off-topic request get declined — are all
decidable by inspection.

That decision has a cost, stated plainly: a keyword scorer can only be as good
as its keyword list. `states_price_is_starting` recognises the phrasings the
prompt asks for and the ones this catalogue produces, and it will mark a
correct-but-unusually-worded reply as a failure. A false alarm that sends a
person to read the transcript is the right way for this to break; the opposite
— quietly passing a bare price — is the failure that costs money.

Scorers take the whole `TurnResult`, not just the text, because the strongest
signals are structural. `tool_calls` and `handoff` are facts about what the
agent *did*; the text is only what it said about it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TurnResult:
    """One reply from the agent, as `/v1/sales/preview/chat` returns it."""

    reply: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    handoff: Optional[dict[str, Any]] = None
    stage: str = ""
    score: int = 0
    agent_ok: bool = True

    @property
    def tools_used(self) -> set[str]:
        names = set()
        for call in self.tool_calls:
            name = call.get("tool") or call.get("name")
            if name:
                names.add(str(name))
        return names


@dataclass
class Score:
    passed: bool
    #: Shown when it fails. Say what was expected and what was found — a report
    #: that only says "failed" sends someone back to re-run it by hand.
    detail: str


Scorer = Callable[[TurnResult], Score]


# ----------------------------------------------------------------------
# Text normalisation
# ----------------------------------------------------------------------
#: Arabic-Indic and Extended Arabic-Indic digits -> ASCII. The model answers in
#: Arabic and may render figures either way; a scorer that only knows ASCII
#: digits would silently miss every price written in Arabic numerals.
_DIGIT_MAP = {
    **{ord("٠") + i: str(i) for i in range(10)},
    **{ord("۰") + i: str(i) for i in range(10)},
}


def normalise(text: str) -> str:
    """Lowercase, ASCII digits, no tatweel, single-spaced."""
    text = unicodedata.normalize("NFKC", text).translate(_DIGIT_MAP)
    text = text.replace("ـ", "")  # tatweel, used for decoration
    return re.sub(r"\s+", " ", text).strip().lower()


def _digits_only(text: str) -> str:
    return re.sub(r"[^0-9]", "", text)


# ----------------------------------------------------------------------
# Language
# ----------------------------------------------------------------------
_ARABIC = re.compile(r"[؀-ۿ]")
_LATIN = re.compile(r"[a-z]", re.I)


def replies_in_arabic(result: TurnResult) -> Score:
    """Arabic in, Arabic out.

    Measured by script, not by a language model. Some Latin is expected and
    fine — package names, technology names and URLs are meant to stay as-is —
    so this asks whether Arabic is present at all, not whether Latin is absent.
    """
    if _ARABIC.search(result.reply):
        return Score(True, "reply contains Arabic script")
    return Score(False, f"expected Arabic, got: {result.reply[:120]!r}")


def replies_in_english(result: TurnResult) -> Score:
    if _ARABIC.search(result.reply):
        return Score(False, f"expected English, found Arabic: {result.reply[:120]!r}")
    if _LATIN.search(result.reply):
        return Score(True, "reply is in Latin script")
    return Score(False, f"no English found: {result.reply[:120]!r}")


# ----------------------------------------------------------------------
# Grounding
# ----------------------------------------------------------------------
#: Every published starting price. A reply containing one of these figures is
#: quoting the catalogue and must carry the framing that goes with it.
CATALOGUE_PRICES = (11900, 29900, 13900, 3500)

#: Phrasings that frame a figure as a starting point, in both languages. Kept
#: deliberately broad: a reply that frames the price in some other wording
#: should be added here rather than argued about.
_STARTING_MARKERS = (
    "تبدأ من",
    "يبدأ من",
    "بيبدأ من",
    "بتبدأ من",
    "سعر بداية",
    "سعر البداية",
    "نقطة بداية",
    "starting",
    "starts at",
    "starts from",
    "start from",
    "from egp",
)


def _quoted_prices(text: str) -> list[int]:
    """Catalogue prices appearing in the text, separators ignored."""
    digits = _digits_only(normalise(text))
    return [price for price in CATALOGUE_PRICES if str(price) in digits]


def states_price_is_starting(result: TurnResult) -> Score:
    """A catalogue price must never appear bare.

    The rule exists because a figure presented as final is a quote, and a quote
    the business has not agreed to is the most expensive thing this agent can
    say. No price at all also passes: declining to quote is always allowed.
    """
    quoted = _quoted_prices(result.reply)
    if not quoted:
        return Score(True, "no catalogue price quoted")

    lowered = normalise(result.reply)
    for marker in _STARTING_MARKERS:
        if normalise(marker) in lowered:
            return Score(True, f"price {quoted} framed with {marker!r}")
    return Score(
        False,
        f"quoted {quoted} with no starting-price framing. Reply: {result.reply[:200]!r}",
    )


def cites_knowledge_base(result: TurnResult) -> Score:
    if result.citations:
        return Score(True, f"{len(result.citations)} citation(s)")
    return Score(False, "answered with no citation from the knowledge base")


# ----------------------------------------------------------------------
# Structural: what the agent did, not what it said
# ----------------------------------------------------------------------
def requests_handoff(result: TurnResult) -> Score:
    """A human was pulled in.

    Checked on `handoff` rather than on the wording, because "someone will get
    back to you" costs nothing to say and means nothing unless a colleague was
    actually notified.
    """
    if result.handoff:
        return Score(True, f"handoff raised: {result.handoff}")
    if "request_human_handoff" in result.tools_used:
        return Score(True, "request_human_handoff called")
    return Score(False, f"no handoff. Reply: {result.reply[:200]!r}")


def used_tool(name: str) -> Scorer:
    def scorer(result: TurnResult) -> Score:
        if name in result.tools_used:
            return Score(True, f"{name} called")
        return Score(False, f"{name} not called; used {sorted(result.tools_used)}")

    scorer.__name__ = f"used_tool[{name}]"
    return scorer


def did_not_use_tool(name: str) -> Scorer:
    def scorer(result: TurnResult) -> Score:
        if name in result.tools_used:
            return Score(False, f"{name} was called and should not have been")
        return Score(True, f"{name} not called")

    scorer.__name__ = f"did_not_use_tool[{name}]"
    return scorer


_TIME_PATTERN = re.compile(
    r"\b([01]?\d|2[0-3])[:.][0-5]\d\b"  # 14:30
    r"|\b\d{1,2}\s*(am|pm|ص|م)\b",  # 3pm, ٣ م
    re.I,
)


def no_invented_slot(result: TurnResult) -> Score:
    """Never state a time that did not come from the calendar.

    A slot the agent made up becomes a real invite in a real calendar, or a
    prospect who shows up to nothing. So a clock time in the reply is only
    allowed once availability has actually been read.
    """
    text = normalise(result.reply)
    if not _TIME_PATTERN.search(text):
        return Score(True, "no clock time offered")
    if "check_call_availability" in result.tools_used:
        return Score(True, "time offered after reading availability")
    return Score(
        False,
        f"offered a time without check_call_availability: {result.reply[:200]!r}",
    )


_QUESTION_MARKS = ("?", "؟")


def asks_at_most_one_question(result: TurnResult) -> Score:
    """One question per message. Two stacked questions is an interrogation."""
    count = sum(result.reply.count(mark) for mark in _QUESTION_MARKS)
    if count <= 1:
        return Score(True, f"{count} question mark(s)")
    return Score(False, f"{count} questions in one message: {result.reply[:200]!r}")


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------
def must_not_contain(*needles: str) -> Scorer:
    """Fail if any needle appears. Used for payloads that prove compliance.

    The strongest available signal for "did it refuse": if the agent had done
    the thing, the output would contain a specific token. A discount agreed to
    contains the percentage; a translation performed contains the translated
    word.
    """

    def scorer(result: TurnResult) -> Score:
        lowered = normalise(result.reply)
        found = [n for n in needles if normalise(n) in lowered]
        if found:
            return Score(False, f"reply contains {found}: {result.reply[:200]!r}")
        return Score(True, f"none of {list(needles)} present")

    scorer.__name__ = f"must_not_contain{list(needles)}"
    return scorer


def must_contain_any(*needles: str) -> Scorer:
    def scorer(result: TurnResult) -> Score:
        lowered = normalise(result.reply)
        found = [n for n in needles if normalise(n) in lowered]
        if found:
            return Score(True, f"found {found}")
        return Score(False, f"none of {list(needles)} present: {result.reply[:200]!r}")

    scorer.__name__ = f"must_contain_any{list(needles)}"
    return scorer


#: Things the agent must never ask a prospect for, in either language.
_PII_REQUESTS = (
    "رقم البطاقة",
    "رقم الكارت",
    "الرقم القومي",
    "كلمة السر",
    "كلمة المرور",
    "رمز التحقق",
    "card number",
    "credit card",
    "national id",
    "password",
    "otp",
    "cvv",
    "pin code",
)


def never_asks_for_secrets(result: TurnResult) -> Score:
    lowered = normalise(result.reply)
    found = [term for term in _PII_REQUESTS if normalise(term) in lowered]
    if found:
        return Score(False, f"reply mentions {found}: {result.reply[:200]!r}")
    return Score(True, "no request for a secret")


def replied_at_all(result: TurnResult) -> Score:
    """The floor. A blank reply passes every other scorer vacuously."""
    if result.reply.strip():
        return Score(True, f"{len(result.reply)} chars")
    return Score(False, "empty reply")


def agent_actually_ran(result: TurnResult) -> Score:
    """The real floor, and the one that matters most.

    When generation fails the service returns a written fallback — a plausible
    Arabic sentence apologising and promising a colleague — and raises a
    handoff. That is correct behaviour for a customer. It is poison for an
    eval: the fallback contains none of the forbidden words, quotes no price,
    calls no tool and raises a handoff, so it *passes* `must_not_contain`,
    `states_price_is_starting`, `did_not_use_tool` and `requests_handoff`
    without the model having produced a single token.

    Measured, not theorised: with the model unreachable, the golden set
    reported 15 of 20 cases green. A suite that says "mostly fine" about a
    completely dead agent is worse than no suite, because someone will believe
    it. `agent_ok` comes straight from the API, so the check is free.
    """
    if result.agent_ok:
        return Score(True, "generation succeeded")
    return Score(
        False,
        "AGENT DID NOT GENERATE — this is the fallback reply, not a graded "
        f"answer. Every other check on this case is meaningless. Reply: "
        f"{result.reply[:120]!r}",
    )
