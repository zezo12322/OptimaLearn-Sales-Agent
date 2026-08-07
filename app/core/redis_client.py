"""Redis connection, shared by Celery and any short-lived caching."""

import redis.asyncio as aioredis

from app.core.config import settings

redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
