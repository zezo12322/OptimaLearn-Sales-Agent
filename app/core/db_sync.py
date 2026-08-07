"""Synchronous engine, used by Alembic.

The Celery worker runs the same async code as the API (via ``asyncio.run``) so
there is exactly one data-access path to reason about; this engine exists for
migrations, which are synchronous by nature.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

sync_engine = create_engine(settings.sync_database_url, pool_pre_ping=True)
SyncSessionLocal = sessionmaker(bind=sync_engine, autocommit=False, autoflush=False)
