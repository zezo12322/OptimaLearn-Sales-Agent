"""Normalisation for the values the agent extracts from free-form chat.

Everything a prospect types arrives inconsistent: phone numbers as "0100 123
4567" or "+20 100-123-4567", e-mails with stray spaces, and a language that has
to be guessed from the script. Normalising once, here, is what lets the lead
table carry a real unique constraint on phone and e-mail.
"""

import re
from typing import Optional

#: Default country for bare local numbers. Egypt is the primary market; a number
#: starting 01 with no prefix is an Egyptian mobile, not an ambiguity.
DEFAULT_COUNTRY_CALLING_CODE = "20"
DEFAULT_COUNTRY_ISO = "EG"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

#: Arabic, Arabic Supplement and Arabic Presentation Forms.
_ARABIC_RANGES = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
)

#: Eastern Arabic and Persian digit forms, which phone numbers arrive in often.
_DIGIT_TRANSLATION = str.maketrans(
    {
        **{chr(0x0660 + i): str(i) for i in range(10)},
        **{chr(0x06F0 + i): str(i) for i in range(10)},
    }
)


def _is_arabic(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _ARABIC_RANGES)


#: How much English a message may contain before it stops counting as Arabic.
#: Arabic words are weighted 2:1 because the asymmetry is real: Egyptian
#: prospects write Arabic peppered with Latin product and tech terms ("عايز
#: أعرف سعر الـ Pro plan"), while English speakers almost never drop Arabic words
#: into an English sentence. Counting letters instead of words gets this exactly
#: backwards — "شكرا OptimaLearn" has four Arabic letters and eleven Latin ones.
_ARABIC_WORD_WEIGHT = 2


def detect_locale(text: str, default: str = "ar") -> str:
    """Guess ``"ar"`` or ``"en"`` from the script actually used.

    Compares words rather than characters, so a Latin brand name inside an Arabic
    sentence does not flip the whole message to English — while a genuinely
    English sentence containing one Arabic word still reads as English.
    """
    arabic_words = latin_words = 0
    for word in text.split():
        has_arabic = any(_is_arabic(ch) for ch in word)
        has_latin = any(ch.isalpha() and ch.isascii() for ch in word)
        if has_arabic:
            arabic_words += 1
        elif has_latin:
            latin_words += 1

    if arabic_words == 0 and latin_words == 0:
        return default
    if arabic_words == 0:
        return "en"
    return "ar" if arabic_words * _ARABIC_WORD_WEIGHT >= latin_words else "en"


def normalize_phone(
    raw: Optional[str], default_calling_code: str = DEFAULT_COUNTRY_CALLING_CODE
) -> Optional[str]:
    """Best-effort E.164. Returns ``None`` when the input cannot be a number.

    Not a full libphonenumber replacement — it handles the shapes people
    actually type in this market and refuses everything it is unsure about,
    because a wrong phone number in the CRM is worse than a missing one.
    """
    if not raw:
        return None

    text = str(raw).translate(_DIGIT_TRANSLATION).strip()
    had_plus = text.startswith("+") or text.startswith("00")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None

    if text.startswith("00"):
        digits = digits[2:]
    if not digits:
        return None

    if not had_plus:
        if digits.startswith("0"):
            # Local trunk form: 01xxxxxxxxx -> +2 01xxxxxxxxx
            digits = default_calling_code + digits.lstrip("0")
        elif not digits.startswith(default_calling_code):
            # A bare subscriber number with no trunk zero and no country code.
            digits = default_calling_code + digits

    # E.164 allows at most 15 digits; anything under 8 is not a real number.
    if not 8 <= len(digits) <= 15:
        return None
    return f"+{digits}"


def normalize_email(raw: Optional[str]) -> Optional[str]:
    """Lower-cased, whitespace-stripped e-mail, or ``None`` if malformed."""
    if not raw:
        return None
    candidate = str(raw).strip().strip("<>").replace(" ", "").lower()
    if not _EMAIL_RE.match(candidate):
        return None
    return candidate


def clean_text(raw: Optional[str], max_length: int = 500) -> Optional[str]:
    """Collapse whitespace and bound length for a free-text field."""
    if raw is None:
        return None
    collapsed = " ".join(str(raw).split())
    if not collapsed:
        return None
    return collapsed[:max_length]


def coerce_int(raw: object, minimum: int = 0, maximum: int = 1_000_000) -> Optional[int]:
    """Parse an integer out of anything, clamped, or ``None``.

    Handles "about 50", "50 employees" and "٥٠" — all things a prospect writes
    when asked how many people need access.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        value = int(raw)
    else:
        text = str(raw or "").translate(_DIGIT_TRANSLATION)
        match = re.search(r"\d+", text)
        if not match:
            return None
        value = int(match.group())
    if value < minimum or value > maximum:
        return None
    return value
