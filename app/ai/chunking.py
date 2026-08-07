"""Tokenisation helpers.

``cl100k_base`` is the encoding behind the embedding model, so counting with it
means the chunk budget matches what the API will actually charge and truncate on.

The catch: ``tiktoken.get_encoding`` downloads its vocabulary from a Microsoft
CDN the first time it runs. A restricted network, an air-gapped deployment or a
CDN outage would otherwise take the knowledge-base ingest down with it — for a
*byte-counting* helper. So the encoder is loaded lazily and, if it cannot be
loaded, we fall back to a character-ratio estimate and say so once in the log.

Chunk sizes are heuristics either way; approximate chunking that works beats
exact chunking that is unavailable. The Dockerfile pre-warms the cache at build
time so production never takes the fallback.
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Average characters per token for the mixed Arabic/English content this service
#: handles. Arabic runs denser than English in ``cl100k_base``; 3 is a
#: deliberately conservative middle, so the estimate over- rather than
#: under-counts and chunks stay inside the real limit.
_FALLBACK_CHARS_PER_TOKEN = 3

_encoder: Optional[Any] = None
_encoder_unavailable = False


def _get_encoder() -> Optional[Any]:
    global _encoder, _encoder_unavailable
    if _encoder is not None or _encoder_unavailable:
        return _encoder
    try:
        import tiktoken

        _encoder = tiktoken.get_encoding("cl100k_base")
    except Exception as exc:  # noqa: BLE001 - network, disk, or import failure
        _encoder_unavailable = True
        logger.warning(
            "tiktoken encoding unavailable (%s); falling back to a character "
            "estimate for chunk sizing. Pre-warm the tiktoken cache to avoid this.",
            exc,
        )
        return None
    return _encoder


def count_tokens(text: str) -> int:
    encoder = _get_encoder()
    if encoder is None:
        return max(1, len(text) // _FALLBACK_CHARS_PER_TOKEN) if text else 0
    return len(encoder.encode(text))


def take_token_tail(text: str, max_tokens: int) -> str:
    """The last ``max_tokens`` worth of text, for chunk overlap."""
    encoder = _get_encoder()
    if encoder is None:
        return text[-(max_tokens * _FALLBACK_CHARS_PER_TOKEN) :]
    tokens = encoder.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return encoder.decode(tokens[-max_tokens:])


def encoder_available() -> bool:
    """Whether exact token counting is in use. Surfaced on the readiness probe."""
    return _get_encoder() is not None
