"""Messaging policy.

These are the tests that protect the WhatsApp number and the Facebook page, so
they are written as a matrix of the situations that actually get accounts banned:
messaging without consent, messaging after an opt-out, messaging at 3am, and
free-form messaging outside the service window.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.sales.enums import Channel, Stage
from app.sales.policy import (
    Decision,
    LeadMessagingState,
    MessagingPolicyConfig,
    detect_opt_in,
    detect_opt_out,
    evaluate_outbound,
    evaluate_private_reply,
    is_quiet_hour,
    may_auto_reply,
    service_window_open,
)

CONFIG = MessagingPolicyConfig(
    service_window_hours=24,
    quiet_hours_start=21,
    quiet_hours_end=9,
    default_timezone="Africa/Cairo",
    max_per_lead_per_day=1,
    max_per_lead_total=5,
)

#: 13:00 in Cairo (UTC+2/+3) — comfortably outside quiet hours all year.
MIDDAY_UTC = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)
#: 01:00 in Cairo.
NIGHT_UTC = datetime(2026, 8, 7, 22, 0, tzinfo=timezone.utc)


def state(**overrides) -> LeadMessagingState:
    defaults = {
        "channel": Channel.WHATSAPP,
        "stage": Stage.ENGAGED,
        "marketing_opt_in": True,
        "last_inbound_at": MIDDAY_UTC - timedelta(hours=2),
    }
    defaults.update(overrides)
    return LeadMessagingState(**defaults)


class TestQuietHours:
    @pytest.mark.parametrize("hour", [21, 22, 23, 0, 3, 8])
    def test_inside_wrapping_window(self, hour: int) -> None:
        assert is_quiet_hour(hour, 21, 9) is True

    @pytest.mark.parametrize("hour", [9, 12, 17, 20])
    def test_outside_wrapping_window(self, hour: int) -> None:
        assert is_quiet_hour(hour, 21, 9) is False

    def test_non_wrapping_window(self) -> None:
        assert is_quiet_hour(2, 1, 6) is True
        assert is_quiet_hour(7, 1, 6) is False

    def test_disabled_when_start_equals_end(self) -> None:
        assert is_quiet_hour(3, 0, 0) is False


class TestServiceWindow:
    def test_open_within_configured_hours(self) -> None:
        assert service_window_open(state(), CONFIG, MIDDAY_UTC) is True

    def test_closed_after_window(self) -> None:
        stale = state(last_inbound_at=MIDDAY_UTC - timedelta(hours=25))
        assert service_window_open(stale, CONFIG, MIDDAY_UTC) is False

    def test_closed_with_no_inbound_ever(self) -> None:
        assert service_window_open(state(last_inbound_at=None), CONFIG, MIDDAY_UTC) is False

    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        """A tz-naive column value must not crash the comparison."""
        naive = state(last_inbound_at=datetime(2026, 8, 7, 9, 0))
        assert service_window_open(naive, CONFIG, MIDDAY_UTC) is True


class TestOutboundHardStops:
    def test_opt_out_is_absolute(self) -> None:
        verdict = evaluate_outbound(state(opted_out=True), CONFIG, MIDDAY_UTC)
        assert verdict.decision is Decision.DENY
        assert verdict.reason == "OPTED_OUT"

    def test_opt_out_beats_every_other_signal(self) -> None:
        """Opted out and inside the window is still denied, and denied for that reason."""
        verdict = evaluate_outbound(
            state(opted_out=True, marketing_opt_in=True), CONFIG, MIDDAY_UTC
        )
        assert verdict.reason == "OPTED_OUT"

    def test_human_takeover_silences_automation(self) -> None:
        verdict = evaluate_outbound(state(human_takeover=True), CONFIG, MIDDAY_UTC)
        assert verdict.decision is Decision.DENY
        assert verdict.reason == "HUMAN_TAKEOVER"

    @pytest.mark.parametrize("stage", [Stage.WON, Stage.LOST, Stage.UNQUALIFIED])
    def test_terminal_stages_are_left_alone(self, stage: Stage) -> None:
        verdict = evaluate_outbound(state(stage=stage), CONFIG, MIDDAY_UTC)
        assert verdict.decision is Decision.DENY
        assert verdict.reason == "TERMINAL_STAGE"

    def test_no_consent_and_no_inbound_is_denied(self) -> None:
        """The cold-messaging guard: this is the rule that keeps the number alive."""
        cold = state(marketing_opt_in=False, last_inbound_at=None)
        verdict = evaluate_outbound(cold, CONFIG, MIDDAY_UTC)
        assert verdict.decision is Decision.DENY
        assert verdict.reason == "NO_CONSENT"

    def test_prior_inbound_is_its_own_lawful_basis(self) -> None:
        replied_once = state(
            marketing_opt_in=False, last_inbound_at=MIDDAY_UTC - timedelta(hours=1)
        )
        assert evaluate_outbound(replied_once, CONFIG, MIDDAY_UTC).allowed is True

    def test_lifetime_cap_denies(self) -> None:
        verdict = evaluate_outbound(state(sent_total=5), CONFIG, MIDDAY_UTC)
        assert verdict.decision is Decision.DENY
        assert verdict.reason == "TOTAL_CAP"


class TestOutboundDeferrals:
    def test_daily_cap_defers_to_next_local_midnight(self) -> None:
        verdict = evaluate_outbound(state(sent_today=1), CONFIG, MIDDAY_UTC)
        assert verdict.decision is Decision.DEFER
        assert verdict.reason == "DAILY_CAP"
        assert verdict.retry_at is not None
        assert verdict.retry_at > MIDDAY_UTC

    def test_quiet_hours_defer_until_morning(self) -> None:
        verdict = evaluate_outbound(state(), CONFIG, NIGHT_UTC)
        assert verdict.decision is Decision.DEFER
        assert verdict.reason == "QUIET_HOURS"
        assert verdict.retry_at is not None and verdict.retry_at > NIGHT_UTC

    def test_deferral_is_not_treated_as_allowed(self) -> None:
        assert evaluate_outbound(state(), CONFIG, NIGHT_UTC).allowed is False

    def test_cap_is_checked_before_quiet_hours(self) -> None:
        """The recorded reason should be the more fundamental one."""
        verdict = evaluate_outbound(state(sent_today=1), CONFIG, NIGHT_UTC)
        assert verdict.reason == "DAILY_CAP"


class TestOutboundAllowances:
    def test_open_window_allows_free_text(self) -> None:
        verdict = evaluate_outbound(state(), CONFIG, MIDDAY_UTC)
        assert verdict.decision is Decision.ALLOW_FREEFORM
        assert verdict.requires_template is False

    def test_closed_whatsapp_window_requires_a_template(self) -> None:
        stale = state(last_inbound_at=MIDDAY_UTC - timedelta(hours=30))
        verdict = evaluate_outbound(stale, CONFIG, MIDDAY_UTC)
        assert verdict.decision is Decision.ALLOW_TEMPLATE
        assert verdict.requires_template is True

    def test_closed_messenger_window_has_no_compliant_path(self) -> None:
        stale = state(
            channel=Channel.MESSENGER, last_inbound_at=MIDDAY_UTC - timedelta(hours=30)
        )
        verdict = evaluate_outbound(stale, CONFIG, MIDDAY_UTC)
        assert verdict.decision is Decision.DENY
        assert verdict.reason == "MESSENGER_WINDOW_CLOSED"

    def test_web_channel_is_not_window_governed(self) -> None:
        web = state(channel=Channel.WEB, last_inbound_at=None, marketing_opt_in=True)
        verdict = evaluate_outbound(web, CONFIG, MIDDAY_UTC)
        assert verdict.decision is Decision.ALLOW_FREEFORM
        assert verdict.reason == "NOT_WINDOW_GOVERNED"


class TestAutoReply:
    def test_replies_are_never_blocked_by_quiet_hours_or_caps(self) -> None:
        """Refusing to answer a live question is worse service, not compliance."""
        busy = state(sent_today=99, sent_total=99)
        assert may_auto_reply(busy).decision is Decision.ALLOW_FREEFORM

    def test_human_takeover_stops_the_agent(self) -> None:
        verdict = may_auto_reply(state(human_takeover=True))
        assert verdict.decision is Decision.DENY
        assert verdict.reason == "HUMAN_TAKEOVER"

    def test_opted_out_lead_is_logged_but_not_answered(self) -> None:
        assert may_auto_reply(state(opted_out=True)).decision is Decision.DENY

    def test_master_switch_off(self) -> None:
        verdict = may_auto_reply(state(), agent_enabled=False)
        assert verdict.reason == "AGENT_DISABLED"


class TestPrivateReply:
    def test_a_comment_needs_no_prior_consent(self) -> None:
        commenter = state(marketing_opt_in=False, last_inbound_at=None)
        verdict = evaluate_private_reply(commenter)
        assert verdict.decision is Decision.ALLOW_FREEFORM
        assert verdict.reason == "COMMENT_PRIVATE_REPLY"

    def test_opt_out_still_applies_to_comments(self) -> None:
        assert evaluate_private_reply(state(opted_out=True)).decision is Decision.DENY

    def test_human_owner_answers_their_own_comments(self) -> None:
        assert (
            evaluate_private_reply(state(human_takeover=True)).decision is Decision.DENY
        )


class TestOptOutDetection:
    @pytest.mark.parametrize(
        "message",
        [
            "stop",
            "STOP",
            "  unsubscribe ",
            "الغاء",
            "إلغاء",
            "إلغاء الاشتراك",
            "مش عايز",
            "كفاية",
            "لا تراسلني",
        ],
    )
    def test_recognised_opt_outs(self, message: str) -> None:
        assert detect_opt_out(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "stop sending the same pdf, send the pricing instead",
            "عايز الغاء الكورس ده من عندي وأشترك في التاني",
            "إيه سعر الباقة؟",
            "",
            "لا",
        ],
    )
    def test_requests_that_merely_contain_a_keyword_are_not_opt_outs(
        self, message: str
    ) -> None:
        """Whole-message matching: a sentence about cancelling a course is not an opt-out."""
        assert detect_opt_out(message) is False

    def test_diacritics_and_alef_variants_are_folded(self) -> None:
        assert detect_opt_out("إلغَاء") is True

    @pytest.mark.parametrize("message", ["start", "اشتراك", "ابدأ", "موافق"])
    def test_opt_in_keywords(self, message: str) -> None:
        assert detect_opt_in(message) is True
