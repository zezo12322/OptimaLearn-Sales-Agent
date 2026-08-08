"""Azure OpenAI clients.

Both flavours are created once at import: the async client for the request path
(the agent interleaves model calls with database work, so blocking the event loop
is not an option) and the sync client for the embedding batches the Celery worker
runs.

Two ways to reach Azure, because Azure itself offers two.

The **v1 API** (``https://<resource>.openai.azure.com/openai/v1``) speaks the
same protocol as OpenAI's own service, so the stock client talks to it with a
``base_url`` and nothing else. There is no ``api-version`` to pin, which matters
more than it sounds: on the classic API a version too old for the deployed model
fails at request time with a message about an unknown parameter, not about the
version, and that is a genuinely confusing hour to lose.

The **classic API** builds ``/openai/deployments/<name>/...?api-version=`` and
needs the resource endpoint and a version. Still supported here so existing
deployments — and CI, which runs on fake credentials in this shape — keep
working untouched.

Set ``AZURE_OPENAI_BASE_URL`` for the first, or ``AZURE_OPENAI_ENDPOINT`` plus
``AZURE_OPENAI_API_VERSION`` for the second. Settings rejects a configuration
that is neither, at startup rather than at the first customer message.

Under both, the model name we pass is the **deployment** name, which is why the
rest of the code reads ``settings.sales_chat_deployment`` rather than a model id.
"""

from openai import AsyncAzureOpenAI, AsyncOpenAI, AzureOpenAI, OpenAI

from app.core.config import settings

# Shared by both shapes. See Settings for why the SDK defaults are not kept.
_common = {
    "api_key": settings.azure_openai_api_key,
    "max_retries": settings.azure_max_retries,
    "timeout": settings.azure_timeout_seconds,
}

if settings.azure_openai_base_url:
    _kwargs = {
        **_common,
        "base_url": settings.azure_openai_base_url,
    }
    async_client: AsyncOpenAI = AsyncOpenAI(**_kwargs)
    sync_client: OpenAI = OpenAI(**_kwargs)
else:
    _azure_kwargs = {
        **_common,
        "azure_endpoint": settings.azure_openai_endpoint,
        "api_version": settings.azure_openai_api_version,
    }
    async_client = AsyncAzureOpenAI(**_azure_kwargs)
    sync_client = AzureOpenAI(**_azure_kwargs)
