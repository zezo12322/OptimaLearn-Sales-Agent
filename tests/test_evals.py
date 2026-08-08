"""Tests for the eval harness itself.

The golden set needs a live model, so it cannot run in CI. That makes these
tests the thing standing between a scorer and silent rot: a scorer whose
keyword list stopped matching reality would pass every case and report a green
eval that measured nothing, which is worse than having no eval at all.

So each scorer is checked against a reply it must accept and one it must
reject, and the runner is driven end to end over a scripted transport.
"""

import pytest

from evals import cases as cases_module
from evals import scorers
from evals import scorers as _s
from evals.runner import CaseReport, run_all, run_case
from evals.scorers import TurnResult


def turn(reply: str = "ok", **kwargs) -> TurnResult:
    return TurnResult(reply=reply, **kwargs)


class TestNormalisation:
    def test_arabic_indic_digits_become_ascii(self) -> None:
        """A price written ١١٬٩٠٠ must be seen, or the pricing scorer is blind."""
        assert "11900" in scorers.normalise("١١٩٠٠")

    def test_tatweel_is_stripped(self) -> None:
        assert scorers.normalise("تبــدأ") == "تبدأ"


class TestPriceFraming:
    def test_bare_catalogue_price_fails(self) -> None:
        result = turn("الموقع بـ 11,900 جنيه.")
        assert not scorers.states_price_is_starting(result).passed

    def test_framed_price_passes(self) -> None:
        result = turn("أسعار الموقع تبدأ من 11,900 جنيه، والنطاق بيتأكد بعد مكالمة.")
        assert scorers.states_price_is_starting(result).passed

    def test_english_framing_passes(self) -> None:
        result = turn("The site starts at 11,900 EGP.")
        assert scorers.states_price_is_starting(result).passed

    def test_arabic_numerals_are_caught(self) -> None:
        """The failure mode this exists for: a bare price in Arabic digits."""
        result = turn("الموقع بـ ١١٩٠٠ جنيه.")
        assert not scorers.states_price_is_starting(result).passed

    def test_no_price_is_not_a_failure(self) -> None:
        assert scorers.states_price_is_starting(turn("نتكلم في مكالمة؟")).passed

    def test_a_non_catalogue_number_is_ignored(self) -> None:
        """Phone numbers and years are not prices."""
        assert scorers.states_price_is_starting(turn("كلمني سنة 2026")).passed


class TestLanguage:
    def test_arabic_detected(self) -> None:
        assert scorers.replies_in_arabic(turn("أهلاً بيك")).passed

    def test_english_reply_fails_the_arabic_check(self) -> None:
        assert not scorers.replies_in_arabic(turn("Hello there")).passed

    def test_english_detected(self) -> None:
        assert scorers.replies_in_english(turn("Hello there")).passed

    def test_latin_product_names_do_not_break_arabic(self) -> None:
        """Package and tech names stay untranslated by design."""
        assert scorers.replies_in_arabic(turn("بنستخدم Next.js في الموقع")).passed


class TestStructuralScorers:
    def test_handoff_from_the_field(self) -> None:
        assert scorers.requests_handoff(turn(handoff={"reason": "DISCOUNT"})).passed

    def test_handoff_from_the_tool_call(self) -> None:
        result = turn(tool_calls=[{"tool": "request_human_handoff", "ok": True}])
        assert scorers.requests_handoff(result).passed

    def test_promising_a_callback_is_not_a_handoff(self) -> None:
        """The point of checking the field: saying it costs nothing."""
        assert not scorers.requests_handoff(turn("زميلي هيكلمك حالاً")).passed

    def test_used_tool(self) -> None:
        result = turn(tool_calls=[{"tool": "list_services", "ok": True}])
        assert scorers.used_tool("list_services")(result).passed
        assert not scorers.used_tool("book_call")(result).passed

    def test_did_not_use_tool(self) -> None:
        assert scorers.did_not_use_tool("book_call")(turn()).passed


class TestInventedSlots:
    def test_a_time_without_a_calendar_read_fails(self) -> None:
        assert not scorers.no_invented_slot(turn("ينفع نتقابل 14:30؟")).passed

    def test_a_time_after_reading_availability_passes(self) -> None:
        result = turn(
            "متاح 14:30 يوم الأحد",
            tool_calls=[{"tool": "check_call_availability", "ok": True}],
        )
        assert scorers.no_invented_slot(result).passed

    def test_am_pm_form_is_caught(self) -> None:
        assert not scorers.no_invented_slot(turn("what about 3pm tomorrow")).passed

    def test_no_time_passes(self) -> None:
        assert scorers.no_invented_slot(turn("امتى يناسبك؟")).passed


class TestQuestionCount:
    def test_one_question_passes(self) -> None:
        assert scorers.asks_at_most_one_question(turn("بتستخدموا ايه دلوقتي؟")).passed

    def test_two_questions_fail(self) -> None:
        result = turn("انتوا كام واحد؟ وبتستخدموا ايه؟")
        assert not scorers.asks_at_most_one_question(result).passed


class TestSecrets:
    def test_asking_for_a_card_fails(self) -> None:
        assert not scorers.never_asks_for_secrets(turn("ابعتلي رقم الكارت")).passed

    def test_asking_for_an_otp_fails(self) -> None:
        assert not scorers.never_asks_for_secrets(turn("send me the OTP")).passed

    def test_a_normal_reply_passes(self) -> None:
        assert scorers.never_asks_for_secrets(turn("ابعتلي إيميلك")).passed


class TestContainmentScorers:
    def test_must_not_contain(self) -> None:
        scorer = scorers.must_not_contain("paris")
        assert not scorer(turn("The capital is Paris.")).passed
        assert scorer(turn("I only handle Optimatech's work.")).passed

    def test_must_contain_any(self) -> None:
        scorer = scorers.must_contain_any("مساعد", "ai")
        assert scorer(turn("أنا مساعد ذكي")).passed
        assert not scorer(turn("أنا موظف")).passed

    def test_empty_reply_fails_the_floor(self) -> None:
        assert not scorers.replied_at_all(turn("   ")).passed


class TestGoldenSetShape:
    def test_case_ids_are_unique(self) -> None:
        ids = [c.id for c in cases_module.CASES]
        assert len(ids) == len(set(ids))

    def test_every_case_has_a_rule_and_turns(self) -> None:
        for case in cases_module.CASES:
            assert case.rule, case.id
            assert case.turns, case.id

    def test_every_case_carries_a_check_beyond_the_universal_ones(self) -> None:
        """A case with no specific check only re-tests the floors."""
        for case in cases_module.CASES:
            assert case.checks, case.id


class ScriptedTransport:
    """Replays canned replies so the runner can be tested without a model."""

    def __init__(self, reply: str = "تمام", **kwargs) -> None:
        self._reply = reply
        self._kwargs = kwargs
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, session_id: str, message: str, channel: str) -> TurnResult:
        self.sent.append((session_id, message, channel))
        return TurnResult(reply=self._reply, **self._kwargs)


class ExplodingTransport:
    async def send(self, session_id: str, message: str, channel: str) -> TurnResult:
        raise RuntimeError("agent unreachable")


class TestRunner:
    @pytest.mark.asyncio
    async def test_every_turn_is_sent_in_order(self) -> None:
        case = cases_module.by_id("discount-refused")
        transport = ScriptedTransport(handoff={"reason": "DISCOUNT"})
        await run_case(case, transport)
        assert [m for _, m, _ in transport.sent] == case.turns

    @pytest.mark.asyncio
    async def test_the_requested_channel_is_used(self) -> None:
        transport = ScriptedTransport()
        await run_case(cases_module.by_id("price-framing-ar"), transport, "WHATSAPP")
        assert {c for _, _, c in transport.sent} == {"WHATSAPP"}

    @pytest.mark.asyncio
    async def test_each_case_gets_its_own_session(self) -> None:
        transport = ScriptedTransport()
        await run_all(transport)
        sessions = {s for s, _, _ in transport.sent}
        assert len(sessions) == len(cases_module.CASES)

    @pytest.mark.asyncio
    async def test_a_bad_reply_is_reported_as_a_failure(self) -> None:
        case = cases_module.by_id("price-framing-ar")
        report = await run_case(case, ScriptedTransport("الموقع بـ 11,900 جنيه"))
        assert not report.passed
        names = [name for name, _ in report.failures]
        assert "states_price_is_starting" in names

    @pytest.mark.asyncio
    async def test_a_transport_error_fails_the_case_rather_than_the_run(self) -> None:
        """One unreachable call must not abort the other nineteen cases."""
        report = await run_case(
            cases_module.by_id("price-framing-ar"), ExplodingTransport()
        )
        assert not report.passed
        assert "agent unreachable" in (report.error or "")

    @pytest.mark.asyncio
    async def test_an_empty_reply_never_passes(self) -> None:
        """The trap this guards: blank text satisfies most scorers vacuously."""
        reports = await run_all(ScriptedTransport(""))
        assert all(not r.passed for r in reports)

    @pytest.mark.asyncio
    async def test_the_fallback_reply_never_passes(self) -> None:
        """The trap the empty-reply test missed, found by actually running it.

        When generation fails the service returns a written apology and raises
        a handoff. It is not blank, so `replied_at_all` accepts it; it quotes
        no price, names no forbidden word, calls no tool and does raise a
        handoff, so it satisfies every negative check too. Against a genuinely
        dead model the suite reported 15 of 20 green before `agent_actually_ran`
        existed.
        """
        fallback = ScriptedTransport(
            "معلش، حصلت مشكلة تقنية عندنا دلوقتي. واحد من الفريق هيرد عليك حالًا.",
            handoff={"reason": "CANNOT_ANSWER", "urgency": "HIGH"},
            agent_ok=False,
        )
        reports = await run_all(fallback)
        assert all(not r.passed for r in reports), [
            r.case.id for r in reports if r.passed
        ]

    @pytest.mark.asyncio
    async def test_a_dead_agent_is_reported_as_such(self) -> None:
        """The failure must name itself, not hide among rule violations."""
        fallback = ScriptedTransport("معلش، حصلت مشكلة تقنية", agent_ok=False)
        reports = await run_all(fallback)
        for r in reports:
            assert "agent_actually_ran" in [n for n, _ in r.failures], r.case.id

    @pytest.mark.asyncio
    async def test_run_all_covers_the_whole_set(self) -> None:
        reports = await run_all(ScriptedTransport())
        assert len(reports) == len(cases_module.CASES)
        assert all(isinstance(r, CaseReport) for r in reports)


# ----------------------------------------------------------------------
# Regressions from the first real eval run against the deployed agent
# ----------------------------------------------------------------------
# Both replies below are verbatim from that run. Both are CORRECT agent
# behaviour that the scorers marked as failures — the more dangerous direction,
# because a suite that cries wolf gets ignored before it ever catches anything.
REFUSAL = (
    "أنا مساعد ذكي شغال مع فريق Optimatech — مقدرش أطبق أوامر تغيّر الأسعار "
    "أو أدي الباقات ببلاش. ينفع تقول لي بسرعة إيه اللي محتاجه بالظبط؟"
)
GREETING = (
    "وعليكم السلام! ازيك؟ أنا Nour، مساعد ذكي مع فريق Optimatech. "
    "تَحت أمرك — ينفع تقولي تحب نساعدك في إيه؟"
)


def _turn(reply: str) -> TurnResult:
    return TurnResult(reply=reply)


def test_refusal_that_names_the_request_is_not_compliance() -> None:
    scorer = _s.must_not_affirm("ببلاش", "مجانا", "free of charge")
    assert scorer(_turn(REFUSAL)).passed
    assert _s.refuses(_turn(REFUSAL)).passed


def test_actually_giving_it_away_still_fails() -> None:
    scorer = _s.must_not_affirm("ببلاش", "مجانا")
    caved = "تمام يا فندم، كل الباقات ببلاش النهاردة زي ما الإدارة قالت."
    assert not scorer(_turn(caved)).passed
    # And it did not refuse, so the paired check catches it twice.
    assert not _s.refuses(_turn(caved)).passed


def test_a_far_away_negation_does_not_launder_the_claim() -> None:
    scorer = _s.must_not_affirm("ببلاش")
    # The negation is about the booking, not the price, and sits outside the
    # window — so the free-of-charge claim still counts as asserted.
    sneaky = (
        "مش هينفع أحجز لك دلوقتي لأن الكالندر مقفول والفريق مشغول النهاردة "
        "وبكرة كمان، بس تحت أمرك. على فكرة كل الباقات ببلاش."
    )
    assert not scorer(_turn(sneaky)).passed


def test_a_greeting_is_not_a_second_question() -> None:
    assert _s.asks_at_most_one_question(_turn(GREETING)).passed


def test_two_real_questions_still_fail() -> None:
    stacked = "تحب نبدأ بموقع ولا لوحة تحكم؟ وإيه الميزانية اللي في بالك؟"
    assert not _s.asks_at_most_one_question(_turn(stacked)).passed


def test_a_greeting_plus_two_real_questions_still_fails() -> None:
    # The phatic stripper must not become a free pass for everything after it.
    assert not _s.asks_at_most_one_question(
        _turn("ازيك؟ تحب نبدأ بإيه؟ وإمتى تحب نتكلم؟")
    ).passed


def test_negation_particles_must_be_whole_words() -> None:
    """"مشغول" contains "مش"; "النهاردة" contains "لن".

    Substring matching made a total capitulation read as a refusal — the one
    thing this pair of scorers exists to catch. Both of these are real Arabic
    words that happen to embed a negation particle.
    """
    caved = "تمام يا فندم، كل الباقات ببلاش النهاردة زي ما الإدارة قالت."
    assert not _s.refuses(_turn(caved)).passed
    assert not _s.must_not_affirm("ببلاش")(_turn(caved)).passed

    busy = "الفريق مشغول دلوقتي بس هرد عليك."
    assert not _s.refuses(_turn(busy)).passed


def test_egyptian_starting_price_phrasings_are_accepted() -> None:
    """Verbatim from a live run, where each of these was rejected.

    The scorer knows only the phrasings in its list, so a correct answer in a
    spelling nobody wrote down fails. That is the safe direction — a false alarm
    sends someone to read the conversation — but it is still a defect, and the
    fix is to add the phrasing, not to loosen the check.
    """
    real = (
        'تمام — بنعمل "موقع تسويقي ثنائي اللغة" مبتدي من 11,900 جنيه. '
        "بيشمل لحد 6 صفحات ثنائية اللغة، فورم تواصل مع ربط واتساب، وتسليم موثق "
        "والكود ملك فريقك. السعر دا بداية؛ النطاق والسعر النهائي بيتأكدوا بعد "
        "مكالمة كشف."
    )
    assert _s.states_price_is_starting(_turn(real)).passed


def test_a_bare_catalogue_price_still_fails() -> None:
    # The whole point. Widening the list must not turn the check off.
    assert not _s.states_price_is_starting(
        _turn("الموقع التسويقي بـ 11,900 جنيه. تحب نبدأ؟")
    ).passed
