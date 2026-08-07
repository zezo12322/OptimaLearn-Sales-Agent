"""Test configuration.

Settings are validated at import, so the environment has to exist before any
``app.*`` module is imported. pytest loads ``conftest.py`` first, which makes this
the only place the defaults can be set.

Credentials here are deliberately fake. No test in this suite reaches the network:
the units that would (providers, the model, the site) are injected or monkeypatched.
"""

import os

os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test"
)
os.environ.setdefault(
    "SYNC_DATABASE_URL", "postgresql+psycopg2://test:test@localhost:5432/test"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test-key")
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://test.openai.azure.com/")
os.environ.setdefault("AZURE_OPENAI_API_VERSION", "2024-10-21")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test-wa-verify")
os.environ.setdefault("MESSENGER_VERIFY_TOKEN", "test-fb-verify")
