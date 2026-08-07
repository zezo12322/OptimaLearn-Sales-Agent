"""Azure OpenAI clients.

Both flavours are created once at import: the async client for the request path
(the agent interleaves model calls with database work, so blocking the event loop
is not an option) and the sync client for the embedding batches the Celery worker
runs.
"""

from openai import AsyncAzureOpenAI, AzureOpenAI

from app.core.config import settings

_kwargs = {
    "api_key": settings.azure_openai_api_key,
    "azure_endpoint": settings.azure_openai_endpoint,
    "api_version": settings.azure_openai_api_version,
}

async_client = AsyncAzureOpenAI(**_kwargs)
sync_client = AzureOpenAI(**_kwargs)
