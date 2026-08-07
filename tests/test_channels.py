"""Channel plumbing: signatures, formatting, and webhook parsing.

The signature tests matter most. This endpoint can make the agent send messages
on the company's behalf, so an unsigned or wrongly-signed request has to be
rejected — including when the app secret is missing entirely, which is the case a
fail-open implementation would quietly wave through.
"""

import hashlib
import hmac

import pytest

from app.sales.channels import messenger, whatsapp
from app.sales.channels.base import (
    CHANNEL_TEXT_LIMIT,
    KIND_COMMENT,
    KIND_LEAD_FORM,
    KIND_MESSAGE,
    KIND_STATUS,
    KIND_UNSUPPORTED,
    InboundEvent,
    format_for_channel,
    prepare_outgoing,
    split_message,
    verify_meta_signature,
    verify_subscription,
)
from app.sales.enums import Channel

SECRET = "test-app-secret"
BODY = b'{"object":"whatsapp_business_account","entry":[]}'


def signature_for(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class TestSignatureVerification:
    def test_valid_signature_passes(self) -> None:
        assert verify_meta_signature(SECRET, BODY, signature_for(BODY)) is True

    def test_wrong_secret_fails(self) -> None:
        assert verify_meta_signature("other", BODY, signature_for(BODY)) is False

    def test_tampered_body_fails(self) -> None:
        assert verify_meta_signature(SECRET, b'{"object":"x"}', signature_for(BODY)) is False

    def test_missing_secret_fails_closed(self) -> None:
        """An unconfigured deployment must reject webhooks, not accept everything."""
        assert verify_meta_signature(None, BODY, signature_for(BODY)) is False
        assert verify_meta_signature("", BODY, signature_for(BODY)) is False

    def test_missing_header_fails(self) -> None:
        assert verify_meta_signature(SECRET, BODY, None) is False

    @pytest.mark.parametrize(
        "header",
        [
            "sha1=abc",
            "abc",
            "sha256=",
            "=abc",
            "sha256",
        ],
    )
    def test_malformed_headers_fail(self, header: str) -> None:
        assert verify_meta_signature(SECRET, BODY, header) is False

    def test_uppercase_prefix_is_accepted(self) -> None:
        digest = hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
        assert verify_meta_signature(SECRET, BODY, f"SHA256={digest}") is True


class TestSubscriptionHandshake:
    def test_correct_token_and_mode(self) -> None:
        assert verify_subscription("subscribe", "tok", "tok") is True

    def test_wrong_token(self) -> None:
        assert verify_subscription("subscribe", "nope", "tok") is False

    def test_wrong_mode(self) -> None:
        assert verify_subscription("unsubscribe", "tok", "tok") is False

    def test_unconfigured_token_rejects(self) -> None:
        assert verify_subscription("subscribe", "tok", None) is False


class TestFormatting:
    def test_markdown_bold_becomes_whatsapp_bold(self) -> None:
        assert format_for_channel("**Pro** plan", Channel.WHATSAPP) == "*Pro* plan"

    def test_markdown_bold_is_stripped_for_messenger(self) -> None:
        assert format_for_channel("**Pro** plan", Channel.MESSENGER) == "Pro plan"

    def test_links_are_flattened(self) -> None:
        out = format_for_channel("[Pricing](https://x.test/p)", Channel.WHATSAPP)
        assert out == "Pricing: https://x.test/p"

    def test_headings_are_removed(self) -> None:
        assert format_for_channel("## Plans\nBasic", Channel.WHATSAPP) == "Plans\nBasic"

    def test_bullets_become_dots(self) -> None:
        assert format_for_channel("- one\n- two", Channel.WHATSAPP) == "• one\n• two"

    def test_web_channel_is_left_alone(self) -> None:
        text = "**Pro** and [link](https://x.test)"
        assert format_for_channel(text, Channel.WEB) == text

    def test_arabic_is_untouched(self) -> None:
        text = "اهلا بيك في OptimaLearn"
        assert format_for_channel(text, Channel.WHATSAPP) == text


class TestSplitting:
    def test_short_text_is_one_part(self) -> None:
        assert split_message("hello", 100) == ["hello"]

    def test_empty_text_yields_nothing(self) -> None:
        assert split_message("   ", 100) == []

    def test_splits_on_paragraph_boundaries(self) -> None:
        text = "a" * 60 + "\n\n" + "b" * 60
        parts = split_message(text, 100)
        assert len(parts) == 2
        assert parts[0].startswith("a")
        assert parts[1].startswith("b")

    def test_splits_long_paragraph_on_sentences(self) -> None:
        text = "One sentence here. Another sentence here. A third one here."
        parts = split_message(text, 30)
        assert len(parts) > 1
        assert all(len(part) <= 30 for part in parts)

    def test_nothing_is_ever_truncated(self) -> None:
        text = "x" * 500
        parts = split_message(text, 100)
        assert "".join(parts) == text

    def test_prepare_outgoing_respects_the_channel_limit(self) -> None:
        long_text = "لوريم إيبسوم. " * 800
        parts = prepare_outgoing(long_text, Channel.MESSENGER)
        assert all(len(part) <= CHANNEL_TEXT_LIMIT[Channel.MESSENGER] for part in parts)


class TestEventSerialisation:
    def test_round_trips_through_the_celery_boundary(self) -> None:
        event = InboundEvent(
            channel=Channel.WHATSAPP,
            kind=KIND_MESSAGE,
            external_id="201001234567",
            text="عايز أعرف الأسعار",
            provider_message_id="wamid.X",
            profile_name="Zeyad",
            phone="+201001234567",
            source_detail="ad-9",
            source_campaign="launch",
        )
        restored = InboundEvent.from_dict(event.to_dict())
        assert restored.channel is Channel.WHATSAPP
        assert restored.text == event.text
        assert restored.provider_message_id == "wamid.X"
        assert restored.source_campaign == "launch"
        assert restored.timestamp == event.timestamp

    def test_actionable_kinds(self) -> None:
        def make(kind: str) -> InboundEvent:
            return InboundEvent(channel=Channel.WHATSAPP, kind=kind, external_id="1")

        assert make(KIND_MESSAGE).is_actionable is True
        assert make(KIND_COMMENT).is_actionable is True
        assert make(KIND_LEAD_FORM).is_actionable is True
        assert make(KIND_STATUS).is_actionable is False
        assert make(KIND_UNSUPPORTED).is_actionable is False


class TestWhatsAppParsing:
    def test_plain_text_message(self) -> None:
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "contacts": [
                                    {"wa_id": "201001234567", "profile": {"name": "Zeyad"}}
                                ],
                                "messages": [
                                    {
                                        "from": "201001234567",
                                        "id": "wamid.AAA",
                                        "timestamp": "1786000000",
                                        "type": "text",
                                        "text": {"body": "السلام عليكم"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        events = whatsapp.parse_webhook(payload)
        assert len(events) == 1
        event = events[0]
        assert event.kind == KIND_MESSAGE
        assert event.text == "السلام عليكم"
        assert event.profile_name == "Zeyad"
        assert event.phone == "+201001234567"
        assert event.provider_message_id == "wamid.AAA"

    def test_click_to_whatsapp_referral_is_captured(self) -> None:
        """Ad attribution is the difference between "we spent" and "this worked"."""
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "201001234567",
                                        "id": "wamid.BBB",
                                        "type": "text",
                                        "text": {"body": "hi"},
                                        "referral": {
                                            "source_id": "ad-123",
                                            "headline": "Train your team",
                                            "source_type": "ad",
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        event = whatsapp.parse_webhook(payload)[0]
        assert event.source_detail == "ad-123"
        assert event.source_campaign == "Train your team"

    def test_interactive_button_reply_reads_as_text(self) -> None:
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "2010",
                                        "id": "wamid.CCC",
                                        "type": "interactive",
                                        "interactive": {
                                            "button_reply": {
                                                "id": "pricing",
                                                "title": "الأسعار",
                                            }
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        event = whatsapp.parse_webhook(payload)[0]
        assert event.kind == KIND_MESSAGE
        assert event.text == "الأسعار"

    def test_image_without_caption_is_unsupported(self) -> None:
        """Better to admit an attachment was not read than answer the wrong thing."""
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "2010",
                                        "id": "wamid.DDD",
                                        "type": "image",
                                        "image": {},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        assert whatsapp.parse_webhook(payload)[0].kind == KIND_UNSUPPORTED

    def test_image_caption_is_treated_as_the_message(self) -> None:
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "2010",
                                        "id": "wamid.EEE",
                                        "type": "image",
                                        "image": {"caption": "ده الكتالوج بتاعنا"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        event = whatsapp.parse_webhook(payload)[0]
        assert event.kind == KIND_MESSAGE
        assert event.text == "ده الكتالوج بتاعنا"

    def test_delivery_status_events(self) -> None:
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {
                                        "id": "wamid.SENT",
                                        "recipient_id": "2010",
                                        "status": "delivered",
                                        "timestamp": "1786000000",
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        event = whatsapp.parse_webhook(payload)[0]
        assert event.kind == KIND_STATUS
        assert event.text == "delivered"

    @pytest.mark.parametrize("payload", [{}, {"entry": []}, {"entry": [None]}, {"entry": [{}]}])
    def test_junk_payloads_yield_no_events_and_no_exception(self, payload: dict) -> None:
        """A 500 here makes Meta retry the batch and eventually unsubscribe us."""
        assert whatsapp.parse_webhook(payload) == []


class TestMessengerParsing:
    def test_direct_message(self) -> None:
        payload = {
            "entry": [
                {
                    "id": "PAGE",
                    "messaging": [
                        {
                            "sender": {"id": "PSID1"},
                            "timestamp": 1786000000,
                            "message": {"mid": "m.1", "text": "إيه الباقات؟"},
                        }
                    ],
                }
            ]
        }
        event = messenger.parse_webhook(payload)[0]
        assert event.kind == KIND_MESSAGE
        assert event.external_id == "PSID1"
        assert event.provider_message_id == "m.1"

    def test_our_own_echo_is_ignored(self) -> None:
        """Recording echoes would duplicate every outbound message in the transcript."""
        payload = {
            "entry": [
                {
                    "id": "PAGE",
                    "messaging": [
                        {
                            "sender": {"id": "PAGE"},
                            "message": {"mid": "m.2", "text": "hi", "is_echo": True},
                        }
                    ],
                }
            ]
        }
        assert messenger.parse_webhook(payload) == []

    def test_comment_becomes_a_private_reply_opportunity(self) -> None:
        payload = {
            "entry": [
                {
                    "id": "PAGE",
                    "changes": [
                        {
                            "field": "feed",
                            "value": {
                                "item": "comment",
                                "verb": "add",
                                "comment_id": "c.99",
                                "post_id": "p.1",
                                "from": {"id": "U1", "name": "Sara"},
                                "message": "بكام؟",
                            },
                        }
                    ],
                }
            ]
        }
        event = messenger.parse_webhook(payload)[0]
        assert event.kind == KIND_COMMENT
        assert event.reply_to == "c.99"
        assert event.source_detail == "p.1"
        assert event.profile_name == "Sara"
        # No PSID exists yet; the private reply is what mints one.
        assert event.external_id.startswith("comment_author:")

    def test_our_own_page_comment_is_skipped(self) -> None:
        payload = {
            "entry": [
                {
                    "id": "PAGE",
                    "changes": [
                        {
                            "field": "feed",
                            "value": {
                                "item": "comment",
                                "comment_id": "c.1",
                                "from": {"id": "PAGE", "name": "Us"},
                                "message": "thanks!",
                            },
                        }
                    ],
                }
            ]
        }
        assert messenger.parse_webhook(payload) == []

    def test_comment_edits_and_deletes_are_skipped(self) -> None:
        payload = {
            "entry": [
                {
                    "id": "PAGE",
                    "changes": [
                        {
                            "field": "feed",
                            "value": {
                                "item": "comment",
                                "verb": "remove",
                                "comment_id": "c.2",
                                "from": {"id": "U2"},
                                "message": "gone",
                            },
                        }
                    ],
                }
            ]
        }
        assert messenger.parse_webhook(payload) == []

    def test_lead_form_submission(self) -> None:
        payload = {
            "entry": [
                {
                    "id": "PAGE",
                    "changes": [
                        {
                            "field": "leadgen",
                            "value": {
                                "leadgen_id": "L1",
                                "form_id": "F1",
                                "ad_id": "A1",
                                "created_time": 1786000000,
                            },
                        }
                    ],
                }
            ]
        }
        event = messenger.parse_webhook(payload)[0]
        assert event.kind == KIND_LEAD_FORM
        assert event.external_id == "leadgen:L1"
        assert event.source_campaign == "facebook_lead_ad"

    def test_lead_form_field_mapping(self) -> None:
        parsed = messenger.parse_lead_form(
            {
                "id": "L1",
                "created_time": "2026-08-07T10:00:00+0000",
                "form_id": "F1",
                "ad_id": "A1",
                "campaign_name": "August teams",
                "field_data": [
                    {"name": "full_name", "values": ["زياد محمد"]},
                    {"name": "phone_number", "values": ["0100 123 4567"]},
                    {"name": "email", "values": [" Zeyad@Example.COM "]},
                    {"name": "company_name", "values": ["OptimaTech"]},
                ],
            }
        )
        assert parsed["full_name"] == "زياد محمد"
        assert parsed["phone"] == "+201001234567"
        assert parsed["email"] == "zeyad@example.com"
        assert parsed["company_name"] == "OptimaTech"
        assert parsed["campaign_name"] == "August teams"

    def test_lead_form_with_unmapped_fields_still_parses(self) -> None:
        parsed = messenger.parse_lead_form(
            {"id": "L2", "field_data": [{"name": "favourite_colour", "values": ["blue"]}]}
        )
        assert parsed["full_name"] is None
        assert parsed["phone"] is None
        assert parsed["raw_fields"]["favourite_colour"] == "blue"

    def test_delivery_receipts(self) -> None:
        payload = {
            "entry": [
                {
                    "id": "PAGE",
                    "messaging": [
                        {"sender": {"id": "PSID1"}, "delivery": {"mids": ["m.1"]}}
                    ],
                }
            ]
        }
        assert messenger.parse_webhook(payload)[0].kind == KIND_STATUS

    @pytest.mark.parametrize("payload", [{}, {"entry": []}, {"entry": [None]}])
    def test_junk_payloads_are_safe(self, payload: dict) -> None:
        assert messenger.parse_webhook(payload) == []
