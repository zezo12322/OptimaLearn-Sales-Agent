"""Embedding generation with retries.

Rate limits and 5xx are retried with exponential backoff; a 4xx that is not 429
is permanent and fails fast, because retrying a malformed request just delays the
error. Results are re-sorted by index — the API does not guarantee order, and a
mis-ordered batch would attach every embedding to the wrong chunk.
"""

import logging
import time

from openai import APIStatusError

from app.ai.client import sync_client
from app.core.config import settings

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SEC = 2
BATCH_SIZE = 100


class EmbeddingError(Exception):
    pass


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts in batches, preserving input order."""
    if not texts:
        return []

    all_vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        all_vectors.extend(_embed_batch_with_retry(texts[start : start + BATCH_SIZE]))
    return all_vectors


def _embed_batch_with_retry(texts: list[str]) -> list[list[float]]:
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = sync_client.embeddings.create(
                model=settings.azure_deployment_embeddings,
                input=texts,
            )
            ordered = sorted(response.data, key=lambda item: item.index)
            return [item.embedding for item in ordered]

        except APIStatusError as exc:
            last_error = exc
            transient = exc.status_code == 429 or exc.status_code >= 500
            if transient and attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
                continue
            if transient:
                raise EmbeddingError(
                    f"Embedding failed after {MAX_RETRIES} retries: {exc}"
                ) from exc
            raise EmbeddingError(
                f"Embedding permanent failure ({exc.status_code}): {exc}"
            ) from exc

        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"Unexpected embedding error: {exc}") from exc

    raise EmbeddingError(f"Embedding failed: {last_error}")
