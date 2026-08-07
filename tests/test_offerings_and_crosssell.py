"""The service catalogue, cross-sell gating, and cadence rendering.

The cross-sell tests are mostly about restraint: the interesting assertions are
the ones where the answer is "say nothing", because the audience is paying
clients and a mistimed pitch there costs more than a missed sale.
"""

import pytest

from app.sales import booking, offerings, outbound
from app.sales.enums import Segment, Stage
from app.sales.upsell import (
    ClientSignals,
    choose_next_service,
)
from tests.test_agent import make_lead


class TestCatalogue:
    def test_all_four_packages_are_present(self) -> None:
        assert {s.slug for s in offerings.SERVICES} == {
            "marketing-site",
            "cms-dashboard",
            "ai-automation",
            "workspace-setup",
        }

    def test_prices_match_the_site(self) -> None:
        """These are the figures on the public pricing section."""
        assert offerings.get("marketing-site").price_egp == 11_900  # type: ignore[union-attr]
        assert offerings.get("cms-dashboard").price_egp == 29_900  # type: ignore[union-attr]
        assert offerings.get("ai-automation").price_egp == 13_900  # type: ignore[union-attr]
        assert offerings.get("workspace-setup").price_egp == 3_500  # type: ignore[union-attr]

    def test_exactly_one_package_is_flagged_popular(self) -> None:
        assert sum(1 for s in offerings.SERVICES if s.popular) == 1

    def test_every_package_is_bilingual(self) -> None:
        for service in offerings.SERVICES:
            assert service.title_ar and service.title_en
            assert service.tagline_ar and service.tagline_en
            assert service.features_ar and service.features_en

    def test_unknown_slug_returns_none(self) -> None:
        assert offerings.get("does-not-exist") is None
        assert offerings.get("") is None

    def test_payload_always_carries_the_starting_price_framing(self) -> None:
        """The model must not be able to drop it — so it travels with the data."""
        for service in offerings.SERVICES:
            payload = offerings.as_payload(service, "ar")
            assert payload["price_is_starting_point"] is True
            assert payload["currency"] == "EGP"

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("عايز موقع للشركة", "marketing-site"),
            ("I need a website", "marketing-site"),
            ("محتاج لوحة تحكم أدير بيها السجلات", "cms-dashboard"),
            ("we need a dashboard for our records", "cms-dashboard"),
            ("عايز بوت واتساب يرد على العملاء", "ai-automation"),
            ("automation with AI", "ai-automation"),
            ("عايز ايميل بالدومين بتاعنا", "workspace-setup"),
            ("google workspace migration", "workspace-setup"),
        ],
    )
    def test_search_matches_how_people_actually_ask(
        self, query: str, expected: str
    ) -> None:
        results = offerings.search(query)
        assert results, f"no match for {query!r}"
        assert results[0].slug == expected

    def test_empty_query_lists_the_catalogue(self) -> None:
        assert len(offerings.search("")) == 4

    def test_unrelated_query_matches_nothing(self) -> None:
        """Recommending a package for an unrelated ask is worse than admitting none."""
        assert offerings.search("do you sell office furniture") == []

    def test_price_formatting_is_localised(self) -> None:
        assert offerings.format_price(11_900, "ar") == "11,900 جنيه"
        assert offerings.format_price(11_900, "en") == "11,900 EGP"

    def test_custom_payment_bounds_match_the_site(self) -> None:
        bounds = offerings.custom_payment_bounds()
        assert bounds == {"min_egp": 500, "max_egp": 1_000_000}


class TestSiteLinks:
    def test_no_links_when_the_site_root_is_unset(self, monkeypatch) -> None:
        monkeypatch.setattr(offerings.settings, "site_base_url", None, raising=False)
        assert offerings.checkout_url("ar") is None
        assert offerings.booking_url("ar") is None
        assert offerings.pricing_url("ar") is None

    def test_arabic_links_use_the_ar_prefix(self, monkeypatch) -> None:
        monkeypatch.setattr(
            offerings.settings, "site_base_url", "https://optimatech.test/", raising=False
        )
        assert offerings.checkout_url("ar") == "https://optimatech.test/ar/checkout"
        assert offerings.checkout_url("en") == "https://optimatech.test/checkout"

    def test_package_preselect_is_passed_as_a_query_param(self, monkeypatch) -> None:
        monkeypatch.setattr(
            offerings.settings, "site_base_url", "https://optimatech.test", raising=False
        )
        url = offerings.checkout_url("en", "cms-dashboard")
        assert url == "https://optimatech.test/checkout?package=cms-dashboard"

    def test_booking_link_preselects_the_call_type(self, monkeypatch) -> None:
        monkeypatch.setattr(
            offerings.settings, "site_base_url", "https://optimatech.test", raising=False
        )
        assert (
            offerings.booking_url("en", "discovery")
            == "https://optimatech.test/book?type=discovery"
        )


class TestCallTypes:
    def test_only_business_calls_are_offerable(self) -> None:
        """The site's intern-* interview types must never reach a prospect."""
        assert set(booking.CALL_TYPES) == {"discovery", "consultation", "kickoff"}
        assert not any(slug.startswith("intern") for slug in booking.CALL_TYPES)

    def test_durations_match_the_site(self) -> None:
        assert booking.CALL_TYPES["discovery"]["duration_min"] == 20
        assert booking.CALL_TYPES["consultation"]["duration_min"] == 45
        assert booking.CALL_TYPES["kickoff"]["duration_min"] == 60

    @pytest.mark.parametrize(
        "raw", [None, "", "nonsense", "intern-backend", "DISCOVERY", " discovery "]
    )
    def test_unknown_types_fall_back_to_the_free_call(self, raw) -> None:
        """Offering the cheapest, lowest-commitment call is never wrong."""
        assert booking.normalize_call_type(raw) in booking.CALL_TYPES

    def test_intern_types_cannot_be_selected_even_explicitly(self) -> None:
        assert booking.normalize_call_type("intern-ai") == "discovery"

    def test_only_discovery_is_free(self) -> None:
        described = {entry["type"]: entry for entry in booking.describe_call_types("en")}
        assert described["discovery"]["is_free"] is True
        assert described["consultation"]["is_free"] is False
        assert described["kickoff"]["is_free"] is False


class TestCrossSellGating:
    def test_says_nothing_to_someone_who_bought_nothing(self) -> None:
        """Not a client yet — the normal lead conversation handles this."""
        assert choose_next_service(ClientSignals(client_ref="c1")) is None

    def test_says_nothing_while_the_current_project_is_undelivered(self) -> None:
        signals = ClientSignals(
            client_ref="c1",
            purchased_slugs=["marketing-site"],
            latest_delivered=False,
            days_since_last_purchase=90,
        )
        assert choose_next_service(signals) is None

    def test_says_nothing_during_the_settling_period(self) -> None:
        """Selling the next thing days after handover reads as revenue-chasing."""
        signals = ClientSignals(
            client_ref="c1",
            purchased_slugs=["marketing-site"],
            latest_delivered=True,
            days_since_last_purchase=2,
        )
        assert choose_next_service(signals) is None

    def test_suggests_the_natural_next_step_once_settled(self) -> None:
        signals = ClientSignals(
            client_ref="c1",
            purchased_slugs=["marketing-site"],
            latest_delivered=True,
            days_since_last_purchase=45,
        )
        service = choose_next_service(signals)
        assert service is not None and service.slug == "cms-dashboard"

    def test_never_suggests_something_they_already_own(self) -> None:
        signals = ClientSignals(
            client_ref="c1",
            purchased_slugs=["marketing-site", "cms-dashboard"],
            latest_delivered=True,
            days_since_last_purchase=60,
        )
        service = choose_next_service(signals)
        assert service is not None
        assert service.slug not in {"marketing-site", "cms-dashboard"}

    def test_says_nothing_when_they_own_everything(self) -> None:
        signals = ClientSignals(
            client_ref="c1",
            purchased_slugs=[s.slug for s in offerings.SERVICES],
            latest_delivered=True,
            days_since_last_purchase=120,
        )
        assert choose_next_service(signals) is None

    def test_an_explicit_ask_beats_every_other_guard(self) -> None:
        """If the client asked, mid-project and cooldown are irrelevant."""
        signals = ClientSignals(
            client_ref="c1",
            purchased_slugs=["marketing-site"],
            latest_delivered=False,
            days_since_last_purchase=1,
            asked_about_slug="ai-automation",
        )
        service = choose_next_service(signals)
        assert service is not None and service.slug == "ai-automation"

    def test_an_ask_for_something_they_own_is_not_a_suggestion(self) -> None:
        signals = ClientSignals(
            client_ref="c1",
            purchased_slugs=["ai-automation"],
            latest_delivered=True,
            days_since_last_purchase=60,
            asked_about_slug="ai-automation",
        )
        service = choose_next_service(signals)
        assert service is None or service.slug != "ai-automation"

    def test_every_package_has_a_defined_next_step(self) -> None:
        """A client with no mapped next step would silently never hear from us."""
        from app.sales.upsell import NEXT_STEP

        for service in offerings.SERVICES:
            assert NEXT_STEP.get(service.slug), f"{service.slug} has no next step"

    def test_next_steps_only_reference_real_packages(self) -> None:
        from app.sales.upsell import NEXT_STEP

        for options in NEXT_STEP.values():
            for slug, reason in options:
                assert offerings.get(slug) is not None
                assert reason and len(reason) > 10


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
    def test_starter_documents_contain_no_invented_prices(self) -> None:
        """Prices come from the catalogue; a number in prose goes stale silently."""
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


class TestUpsellRoute:
    """The /upsell/recommend wiring.

    These exist because the route was dead for a while and nothing noticed: the
    handler built a dataclass that a refactor had renamed, so every call raised
    AttributeError. Unit tests on `choose_next_service` passed the whole time —
    they never went through the handler. So the assertions here are deliberately
    about the *seam* between the request schema, the handler, and the dataclass,
    not about recommendation quality.
    """

    def test_request_schema_matches_the_signals_dataclass(self) -> None:
        """Every UpsellIn field must land somewhere on ClientSignals.

        `tenant_id` is routing, not a signal, so it is the one exception.
        """
        import dataclasses

        from app.schemas.sales import UpsellIn

        signal_fields = {f.name for f in dataclasses.fields(ClientSignals)}
        payload_fields = set(UpsellIn.model_fields) - {"tenant_id"}
        assert payload_fields <= signal_fields, payload_fields - signal_fields

    @pytest.mark.asyncio
    async def test_handler_builds_signals_and_returns_silence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A client with nothing delivered gets no pitch — and no exception."""
        from app.routers import admin
        from app.schemas.sales import UpsellIn

        captured: dict[str, object] = {}

        async def fake_recommend(db, tenant_id, signals, force=False):  # type: ignore[no-untyped-def]
            captured["signals"] = signals
            return None

        monkeypatch.setattr(admin.upsell, "recommend", fake_recommend)

        result = await admin.recommend_upsell(
            UpsellIn(
                client_ref="client@example.com",
                purchased_slugs=["marketing-site"],
                latest_delivered=False,
            ),
            db=_StubSession(),
        )

        assert result.recommendation is None
        signals = captured["signals"]
        assert isinstance(signals, ClientSignals)
        assert signals.client_ref == "client@example.com"
        assert signals.purchased_slugs == ["marketing-site"]
        assert signals.latest_delivered is False

    @pytest.mark.asyncio
    async def test_handler_returns_the_payload_when_there_is_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wire shape stays service_slug/service_title, not the column names."""
        import uuid as _uuid
        from datetime import datetime, timezone

        from app.core.config import settings
        from app.models.sales import UpsellRecommendation
        from app.routers import admin
        from app.schemas.sales import UpsellIn

        # Without a site base URL the links are correctly None, so set one — the
        # point of this test is that the slug reaches the link.
        monkeypatch.setattr(settings, "site_base_url", "https://optimatech.test")

        recommendation = UpsellRecommendation(
            id=_uuid.uuid4(),
            tenant_id=_uuid.uuid4(),
            client_ref="client@example.com",
            recommended_service_slug="cms-dashboard",
            recommended_service_title="لوحة تحكم + CMS مخصص",
            reason_code="NATURAL_NEXT_STEP",
            pitch="…",
            locale="ar",
            created_at=datetime.now(timezone.utc),
        )

        async def fake_recommend(db, tenant_id, signals, force=False):  # type: ignore[no-untyped-def]
            return recommendation

        monkeypatch.setattr(admin.upsell, "recommend", fake_recommend)

        result = await admin.recommend_upsell(
            UpsellIn(client_ref="client@example.com"), db=_StubSession()
        )

        assert result.recommendation is not None
        assert result.recommendation["service_slug"] == "cms-dashboard"
        assert "cms-dashboard" in result.recommendation["checkout_url"]


class _StubSession:
    """Just enough AsyncSession for a handler that only commits."""

    async def commit(self) -> None:
        return None
