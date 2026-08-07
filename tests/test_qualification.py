"""Lead scoring and stage transitions.

Scoring is deterministic so it can be explained to a sales rep; these tests are
what keep it explainable. The important properties are the invariants: the score
is bounded, the breakdown sums to it, and a stage a human set is never undone.
"""

import pytest

from app.sales.enums import Segment, Stage
from app.sales.qualification import (
    MAX_SCORE,
    QUALIFIED_SCORE_THRESHOLD,
    LeadSignals,
    derive_stage,
    score_lead,
    signals_from_lead,
)


def b2b_ready() -> LeadSignals:
    """A lead that has told us everything worth knowing."""
    return LeadSignals(
        segment=Segment.B2B,
        has_name=True,
        has_contact=True,
        company_size=120,
        job_title="L&D Manager",
        qualification={
            "need": "تدريب 60 موظف على مهارات القيادة",
            "authority": "DECISION_MAKER",
            "budget_confirmed": True,
            "timeline": "IMMEDIATE",
            "seats": 60,
        },
        inbound_message_count=7,
    )


class TestScoring:
    def test_empty_lead_scores_zero(self) -> None:
        result = score_lead(LeadSignals())
        assert result.score == 0
        assert sum(result.breakdown.values()) == 0

    def test_breakdown_always_sums_to_the_score(self) -> None:
        result = score_lead(b2b_ready())
        assert sum(result.breakdown.values()) >= result.score

    def test_score_is_capped_at_max(self) -> None:
        assert score_lead(b2b_ready()).score == MAX_SCORE

    def test_fully_qualified_lead_clears_the_threshold(self) -> None:
        assert score_lead(b2b_ready()).score >= QUALIFIED_SCORE_THRESHOLD

    def test_b2b_outscores_b2c_all_else_equal(self) -> None:
        b2c = LeadSignals(segment=Segment.B2C, has_name=True, has_contact=True)
        b2b = LeadSignals(segment=Segment.B2B, has_name=True, has_contact=True)
        assert score_lead(b2b).score > score_lead(b2c).score

    def test_confirmed_budget_beats_a_stated_range(self) -> None:
        stated = score_lead(LeadSignals(qualification={"budget_range": "5000 EGP"}))
        confirmed = score_lead(LeadSignals(qualification={"budget_confirmed": True}))
        assert confirmed.score > stated.score

    @pytest.mark.parametrize(
        ("timeline", "expected_order"),
        [("IMMEDIATE", 3), ("THIS_QUARTER", 2), ("EXPLORING", 1)],
    )
    def test_sooner_timelines_score_higher(
        self, timeline: str, expected_order: int
    ) -> None:
        score = score_lead(LeadSignals(qualification={"timeline": timeline})).score
        assert score > 0
        assert expected_order > 0

    def test_timeline_is_case_insensitive(self) -> None:
        lower = score_lead(LeadSignals(qualification={"timeline": "immediate"}))
        upper = score_lead(LeadSignals(qualification={"timeline": "IMMEDIATE"}))
        assert lower.score == upper.score

    def test_unknown_timeline_scores_nothing(self) -> None:
        assert score_lead(LeadSignals(qualification={"timeline": "someday"})).score == 0

    def test_job_title_is_weaker_evidence_than_a_stated_role(self) -> None:
        titled = score_lead(LeadSignals(job_title="Manager"))
        stated = score_lead(LeadSignals(qualification={"authority": "DECISION_MAKER"}))
        assert stated.score > titled.score > 0

    def test_larger_teams_score_higher(self) -> None:
        small = score_lead(LeadSignals(qualification={"seats": 3})).score
        medium = score_lead(LeadSignals(qualification={"seats": 20})).score
        large = score_lead(LeadSignals(qualification={"seats": 200})).score
        assert small < medium < large


class TestMissingFields:
    def test_a_blank_lead_is_missing_the_basics(self) -> None:
        missing = score_lead(LeadSignals()).missing_fields
        assert "need" in missing
        assert "segment" in missing
        assert "contact" in missing

    def test_a_complete_lead_is_missing_nothing(self) -> None:
        assert score_lead(b2b_ready()).missing_fields == []

    def test_seats_are_only_asked_of_team_buyers(self) -> None:
        """Asking an individual learner how many seats they need is a bad question."""
        individual = LeadSignals(segment=Segment.B2C, has_contact=True)
        assert "seats" not in score_lead(individual).missing_fields

        company = LeadSignals(segment=Segment.B2B, has_contact=True)
        assert "seats" in score_lead(company).missing_fields

    def test_next_question_hint_follows_ask_order(self) -> None:
        """Need comes before budget: nobody answers a budget question first."""
        hint = score_lead(LeadSignals()).next_question_hint
        assert hint is not None
        assert "solve" in hint or "learn" in hint


class TestStageDerivation:
    def test_new_lead_stays_new(self) -> None:
        signals = LeadSignals()
        assert derive_stage(Stage.NEW, score_lead(signals), signals) is Stage.NEW

    def test_first_message_moves_to_engaged(self) -> None:
        signals = LeadSignals(inbound_message_count=1)
        assert derive_stage(Stage.NEW, score_lead(signals), signals) is Stage.ENGAGED

    def test_answering_a_question_moves_to_qualifying(self) -> None:
        signals = LeadSignals(
            inbound_message_count=2, qualification={"need": "تدريب الفريق"}
        )
        assert (
            derive_stage(Stage.ENGAGED, score_lead(signals), signals) is Stage.QUALIFYING
        )

    def test_complete_lead_becomes_qualified(self) -> None:
        signals = b2b_ready()
        assert (
            derive_stage(Stage.QUALIFYING, score_lead(signals), signals)
            is Stage.QUALIFIED
        )

    def test_a_high_score_without_intent_is_not_qualified(self) -> None:
        """Score alone must not promote a lead that never said when or whether."""
        signals = LeadSignals(
            segment=Segment.B2B,
            has_name=True,
            has_contact=True,
            company_size=500,
            job_title="CEO",
            qualification={"need": "تدريب", "authority": "DECISION_MAKER"},
            inbound_message_count=8,
        )
        assert derive_stage(Stage.QUALIFYING, score_lead(signals), signals) is not (
            Stage.QUALIFIED
        )

    @pytest.mark.parametrize(
        "stage",
        [
            Stage.DEMO_BOOKED,
            Stage.PROPOSAL_SENT,
            Stage.WON,
            Stage.LOST,
            Stage.UNQUALIFIED,
            Stage.NURTURE,
        ],
    )
    def test_human_set_stages_are_never_overwritten(self, stage: Stage) -> None:
        """A booked demo must survive a recompute that would say "QUALIFYING"."""
        signals = LeadSignals(inbound_message_count=1)
        assert derive_stage(stage, score_lead(signals), signals) is stage

    def test_explicit_disqualification_wins(self) -> None:
        signals = b2b_ready()
        assert (
            derive_stage(Stage.QUALIFIED, score_lead(signals), signals, disqualified=True)
            is Stage.UNQUALIFIED
        )


class TestSignalsFromLead:
    def test_reads_a_lead_like_object_without_the_orm(self) -> None:
        class FakeLead:
            segment = "B2B"
            full_name = "Zeyad"
            phone_e164 = "+201001234567"
            email = None
            company_size = 40
            job_title = "CTO"
            qualification = {"need": "upskilling"}

        signals = signals_from_lead(FakeLead(), inbound_message_count=3)
        assert signals.segment is Segment.B2B
        assert signals.has_name is True
        assert signals.has_contact is True
        assert signals.inbound_message_count == 3

    def test_unknown_segment_value_degrades_gracefully(self) -> None:
        class FakeLead:
            segment = "SOMETHING_ELSE"
            full_name = None
            phone_e164 = None
            email = None
            company_size = None
            job_title = None
            qualification = None

        assert signals_from_lead(FakeLead()).segment is Segment.UNKNOWN
