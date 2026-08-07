"""Normalisation and knowledge-base chunking.

Phone normalisation is what makes the unique index on ``sales_leads.phone_e164``
meaningful, so it is tested against the shapes people actually type — including
Eastern Arabic digits, which arrive more often than you would expect.
"""

import pytest

from app.sales.kb import (
    MAX_CHUNK_TOKENS,
    chunk_text,
    content_hash,
)
from app.sales.normalize import (
    clean_text,
    coerce_int,
    detect_locale,
    normalize_email,
    normalize_phone,
)


class TestPhoneNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("01001234567", "+201001234567"),
            ("0100 123 4567", "+201001234567"),
            ("+20 100-123-4567", "+201001234567"),
            ("00201001234567", "+201001234567"),
            ("201001234567", "+201001234567"),
            ("1001234567", "+201001234567"),
            ("+201001234567", "+201001234567"),
        ],
    )
    def test_egyptian_shapes_converge(self, raw: str, expected: str) -> None:
        assert normalize_phone(raw) == expected

    def test_eastern_arabic_digits(self) -> None:
        assert normalize_phone("٠١٠٠١٢٣٤٥٦٧") == "+201001234567"

    def test_international_number_is_preserved(self) -> None:
        assert normalize_phone("+44 7911 123456") == "+447911123456"

    @pytest.mark.parametrize("raw", ["", None, "abc", "12", "  ", "+"])
    def test_junk_is_rejected_rather_than_guessed(self, raw) -> None:
        """A wrong number in the CRM is worse than a missing one."""
        assert normalize_phone(raw) is None

    def test_absurdly_long_input_is_rejected(self) -> None:
        assert normalize_phone("1" * 25) is None


class TestEmailNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Zeyad@Example.com", "zeyad@example.com"),
            ("  a@b.co  ", "a@b.co"),
            ("<x@y.org>", "x@y.org"),
            ("a b@c.com", "ab@c.com"),
        ],
    )
    def test_valid_addresses(self, raw: str, expected: str) -> None:
        assert normalize_email(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["", None, "not-an-email", "a@b", "a@@b.com", "@b.com", "a@.com"]
    )
    def test_invalid_addresses(self, raw) -> None:
        assert normalize_email(raw) is None


class TestLocaleDetection:
    def test_arabic(self) -> None:
        assert detect_locale("عايز أعرف الأسعار") == "ar"

    def test_english(self) -> None:
        assert detect_locale("What are your prices?") == "en"

    def test_arabic_with_a_latin_product_name(self) -> None:
        assert detect_locale("شكرا OptimaLearn") == "ar"

    def test_mostly_english_with_one_arabic_word(self) -> None:
        assert detect_locale("Thanks, please send pricing details شكرا") == "en"

    def test_no_letters_falls_back_to_the_default(self) -> None:
        assert detect_locale("👍 123", default="ar") == "ar"
        assert detect_locale("", default="en") == "en"

    def test_arabic_with_several_latin_technical_terms(self) -> None:
        """Egyptian prospects code-switch constantly; that is still Arabic."""
        assert detect_locale("عايز أعرف سعر الـ Pro plan للـ team") == "ar"

    def test_a_single_arabic_word_in_a_long_english_sentence_is_english(self) -> None:
        assert (
            detect_locale(
                "Could you please share the enterprise pricing sheet with me شكرا"
            )
            == "en"
        )


class TestCoerceInt:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("about 50", 50), ("50 employees", 50), ("٥٠", 50), (12, 12), (12.9, 12)],
    )
    def test_extracts_a_number_from_how_people_answer(self, raw, expected) -> None:
        assert coerce_int(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "many", True, False])
    def test_non_numbers(self, raw) -> None:
        assert coerce_int(raw) is None

    def test_out_of_range_is_rejected(self) -> None:
        assert coerce_int(5, minimum=10) is None
        assert coerce_int(500, maximum=100) is None


class TestCleanText:
    def test_collapses_whitespace(self) -> None:
        assert clean_text("  a   b\n c ") == "a b c"

    def test_bounds_length(self) -> None:
        assert len(clean_text("x" * 900, max_length=100) or "") == 100

    def test_blank_becomes_none(self) -> None:
        assert clean_text("   ") is None
        assert clean_text(None) is None


class TestChunking:
    def test_empty_document(self) -> None:
        assert chunk_text("") == []
        assert chunk_text("   \n\n  ") == []

    def test_short_document_is_one_chunk(self) -> None:
        chunks = chunk_text("باقة Pro فيها كل الكورسات.")
        assert len(chunks) == 1
        assert chunks[0].index == 0

    def test_indices_are_sequential(self) -> None:
        document = "\n\n".join(f"فقرة رقم {i} " + ("كلمة " * 200) for i in range(6))
        chunks = chunk_text(document)
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_chunks_respect_the_token_ceiling(self) -> None:
        document = "\n\n".join("word " * 300 for _ in range(5))
        for chunk in chunk_text(document):
            assert chunk.token_count <= MAX_CHUNK_TOKENS * 2

    def test_oversized_single_paragraph_is_split(self) -> None:
        """A pricing table pasted as one block must still be indexable."""
        document = "sentence here. " * 800
        chunks = chunk_text(document)
        assert len(chunks) > 1

    def test_no_content_is_lost(self) -> None:
        document = "\n\n".join(f"Paragraph {i}." for i in range(20))
        rejoined = " ".join(chunk.text for chunk in chunk_text(document))
        for i in range(20):
            assert f"Paragraph {i}." in rejoined

    def test_a_trailing_scrap_is_merged_backwards(self) -> None:
        """A lone closing line retrieves badly on its own."""
        document = "\n\n".join(["كلمة " * 400, "شكرا."])
        chunks = chunk_text(document)
        assert chunks[-1].text.endswith("شكرا.")
        assert len(chunks[-1].text) > len("شكرا.")

    def test_content_hash_is_stable_and_whitespace_insensitive(self) -> None:
        assert content_hash("hello") == content_hash("  hello  ")
        assert content_hash("hello") != content_hash("hello!")
