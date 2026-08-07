"""Upsell gating, quote arithmetic and cadence rendering.

The upsell tests are mostly about restraint: the interesting assertions are the
ones where the answer is "say nothing".
"""

import uuid

import pytest

from app.sales import lms, outbound
from app.sales.enums import Segment, Stage
from app.sales.upsell import (
    REASON_ENGAGED,
    REASON_HIGH_COMPLETION,
    REASON_HIT_LIMIT,
    UsageSignals,
    choose_reason,
    pick_tier,
)
from tests.test_agent import make_lead


def tier(name: str, price: int) -> lms.SubscriptionTier:
    return lms.SubscriptionTier(
        id=str(uuid.uuid4()),
        name=name,
        price=price,
        billing_cycle="MONTHLY",
        features=[f"{name} feature"],
    )


class TestUpsellGating:
    def test_says_nothing_to_a_brand_new_user(self) -> None:
        assert choose_reason(UsageSignals(lms_user_id="u1")) is None

    def test_says_nothing_to_a_barely_active_user(self) -> None:
        signals = UsageSignals(lms_user_id="u1", completed_lectures=2)
        assert choose_reason(signals) is None

    def test_steady_watching_is_a_weak_but_real_signal(self) -> None:
        signals = UsageSignals(lms_user_id="u1", completed_lectures=6)
        assert choose_reason(signals) == REASON_ENGAGED

    def test_finishing_a_course_is_earned_momentum(self) -> None:
        signals = UsageSignals(lms_user_id="u1", completed_courses=1)
        assert choose_reason(signals) == REASON_HIGH_COMPLETION

    def test_hitting_a_limit_is_the_strongest_signal(self) -> None:
        signals = UsageSignals(lms_user_id="u1", hit_limit=True)
        assert choose_reason(signals) == REASON_HIT_LIMIT

    def test_a_paying_customer_is_left_alone(self) -> None:
        """Interrupting someone who already pays to sell them more is worse than silence."""
        signals = UsageSignals(
            lms_user_id="u1", current_plan="Pro", completed_courses=5
        )
        assert choose_reason(signals) is None

    def test_a_paying_customer_who_hits_a_limit_is_asking_for_it(self) -> None:
        signals = UsageSignals(lms_user_id="u1", current_plan="Basic", hit_limit=True)
        assert choose_reason(signals) == REASON_HIT_LIMIT


class TestTierSelection:
    def test_free_user_gets_the_smallest_real_step(self) -> None:
        tiers = [tier("Enterprise", 5000), tier("Basic", 200), tier("Pro", 800)]
        chosen = pick_tier(tiers, UsageSignals(lms_user_id="u1"))
        assert chosen is not None and chosen.name == "Basic"

    def test_paying_user_gets_the_next_price_up(self) -> None:
        tiers = [tier("Basic", 200), tier("Pro", 800), tier("Enterprise", 5000)]
        chosen = pick_tier(tiers, UsageSignals(lms_user_id="u1", current_plan="Pro"))
        assert chosen is not None and chosen.name == "Enterprise"

    def test_top_tier_user_has_nowhere_to_go(self) -> None:
        tiers = [tier("Basic", 200), tier("Pro", 800)]
        assert pick_tier(tiers, UsageSignals(lms_user_id="u1", current_plan="Pro")) is None

    def test_free_tiers_are_not_suggested(self) -> None:
        assert pick_tier([tier("Free", 0)], UsageSignals(lms_user_id="u1")) is None

    def test_no_tiers_at_all(self) -> None:
        assert pick_tier([], UsageSignals(lms_user_id="u1")) is None

    def test_unrecognised_current_plan_degrades_to_the_cheapest(self) -> None:
        tiers = [tier("Basic", 200), tier("Pro", 800)]
        chosen = pick_tier(tiers, UsageSignals(lms_user_id="u1", current_plan="Legacy"))
        assert chosen is not None and chosen.name == "Basic"


class TestQuoteArithmetic:
    def test_list_price_times_seats(self) -> None:
        quote = lms.quote_for_seats(tier("Pro", 800), 10)
        assert quote["list_total"] == 8000
        assert quote["total"] == 8000
        assert quote["volume_discount_pct"] == 0
        assert quote["is_indicative"] is True

    def test_a_published_band_applies(self) -> None:
        quote = lms.quote_for_seats(tier("Pro", 800), 30, {25: 0.10, 100: 0.20})
        assert quote["list_total"] == 24000
        assert quote["total"] == 21600
        assert quote["volume_discount_pct"] == 10.0

    def test_the_highest_matching_band_wins(self) -> None:
        quote = lms.quote_for_seats(tier("Pro", 800), 150, {25: 0.10, 100: 0.20})
        assert quote["volume_discount_pct"] == 20.0

    def test_a_band_below_the_threshold_does_not_apply(self) -> None:
        quote = lms.quote_for_seats(tier("Pro", 800), 5, {25: 0.10})
        assert quote["total"] == quote["list_total"]

    def test_seat_count_is_floored_at_one(self) -> None:
        assert lms.quote_for_seats(tier("Pro", 800), 0)["seats"] == 1


class TestCourseSearch:
    def make_courses(self) -> list[lms.Course]:
        return [
            lms.Course("1", "Leadership Essentials", "for managers", 500, "EGP", "Soft skills", None, 4.5, None),
            lms.Course("2", "Advanced Excel", "pivot tables", 300, "EGP", "Data", None, 4.0, None),
            lms.Course("3", "مهارات القيادة", "للمديرين", 500, "EGP", "Soft skills", None, 4.8, None),
        ]

    def test_title_matches_rank_above_description_matches(self) -> None:
        results = lms.search_courses_locally(self.make_courses(), "leadership")
        assert results[0].title == "Leadership Essentials"

    def test_arabic_query(self) -> None:
        results = lms.search_courses_locally(self.make_courses(), "القيادة")
        assert results and results[0].title == "مهارات القيادة"

    def test_category_match(self) -> None:
        results = lms.search_courses_locally(self.make_courses(), "data")
        assert results and results[0].title == "Advanced Excel"

    def test_no_match_returns_nothing_rather_than_everything(self) -> None:
        """An unrelated recommendation is worse than admitting we have none."""
        assert lms.search_courses_locally(self.make_courses(), "astrophysics") == []

    def test_empty_query_returns_a_sample(self) -> None:
        assert len(lms.search_courses_locally(self.make_courses(), "", limit=2)) == 2

    def test_limit_is_respected(self) -> None:
        results = lms.search_courses_locally(self.make_courses(), "skills soft", limit=1)
        assert len(results) == 1


class TestSequenceRendering:
    def test_placeholders_are_filled(self) -> None:
        lead = make_lead(full_name="زياد محمد", company_name="OptimaTech")
        rendered = outbound._render("اهلا {{name}} من {{company}}", lead)
        assert rendered == "اهلا زياد من OptimaTech"

    def test_a_missing_name_becomes_a_neutral_word(self) -> None:
        """Never send "اهلا  " with a hole where a name should be."""
        rendered = outbound._render("اهلا {{name}}", make_lead(full_name=None))
        assert "{{name}}" not in rendered
        assert rendered.strip() != "اهلا"

    def test_english_lead_gets_an_english_placeholder(self) -> None:
        rendered = outbound._render("Hi {{name}}", make_lead(full_name=None, locale="en"))
        assert "there" in rendered

    def test_product_and_agent_come_from_settings(self) -> None:
        rendered = outbound._render("{{product}} / {{agent}}", make_lead())
        assert "{{" not in rendered


class TestSequenceEligibility:
    @pytest.mark.parametrize("segment", [Segment.B2B, Segment.B2C, Segment.UNKNOWN])
    def test_default_sequence_choice(self, segment: Segment) -> None:
        lead = make_lead(segment=segment.value)
        expected = "b2b-followup" if segment is Segment.B2B else "b2c-followup"
        assert outbound.default_sequence_for(lead) == expected

    def test_goal_stages_cover_everything_a_cadence_was_chasing(self) -> None:
        assert Stage.QUALIFIED in outbound.GOAL_STAGES
        assert Stage.DEMO_BOOKED in outbound.GOAL_STAGES
        assert Stage.WON in outbound.GOAL_STAGES
        assert Stage.NEW not in outbound.GOAL_STAGES


class TestSeedData:
    def test_starter_documents_contain_no_prices(self) -> None:
        """Prices come from the live plans; a hard-coded number goes stale silently."""
        from app.sales import seed

        for document in seed.STARTER_DOCUMENTS:
            digits = [ch for ch in document["content"] if ch.isdigit()]
            assert not digits, f"{document['title']} contains digits: {digits[:5]}"

    def test_every_starter_document_is_marked_for_review(self) -> None:
        from app.sales import seed

        for document in seed.STARTER_DOCUMENTS:
            assert seed.REVIEW_MARKER in document["content"]

    def test_starter_sequences_are_well_formed(self) -> None:
        from app.sales import seed

        for sequence in seed.STARTER_SEQUENCES:
            assert sequence["name"]
            assert sequence["steps"]
            for step in sequence["steps"]:
                assert step["channel"]
                assert step["body"]
                assert float(step["delay_hours"]) > 0

    def test_sequence_delays_increase(self) -> None:
        """A cadence that fires faster over time reads as harassment."""
        from app.sales import seed

        for sequence in seed.STARTER_SEQUENCES:
            delays = [float(step["delay_hours"]) for step in sequence["steps"]]
            assert delays == sorted(delays)
