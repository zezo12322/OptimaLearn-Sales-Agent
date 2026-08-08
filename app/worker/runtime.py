"""Running async code inside Celery tasks.

The whole service is async; duplicating it synchronously for the worker would
mean two implementations of the same logic drifting apart. So the worker keeps
**one event loop and one engine per process** and drives the same coroutines the
API does.

Both are keyed by pid rather than created at import. Celery's prefork pool forks
after import, and a child that inherits its parent's loop (with the parent's
epoll descriptor) or its parent's connection pool misbehaves in ways that are
painful to debug. Re-creating them the first time a task runs in a given process
side-steps that entirely.
"""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any, Optional, TypeVar

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.core.config import settings
from app.core.engine import build_async_engine

logger = logging.getLogger(__name__)

T = TypeVar("T")

_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_pid: Optional[int] = None
_engine: Optional[AsyncEngine] = None
_engine_pid: Optional[int] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_pid
    if _loop is None or _loop.is_closed() or _loop_pid != os.getpid():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _loop_pid = os.getpid()
    return _loop


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _engine, _engine_pid, _session_factory
    if _session_factory is None or _engine_pid != os.getpid():
        # Small pool: concurrency here is the number of Celery slots, not the
        # number of HTTP requests. Dropped when the URL is a transaction pooler,
        # which does the pooling itself.
        _engine = build_async_engine(
            settings.database_url,
            pool_size=5,
            max_overflow=5,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
        _engine_pid = os.getpid()
    return _session_factory


def run_with_session(work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run ``work`` with a fresh session, committing on success.

    A task either commits everything it did or rolls all of it back: a lead with
    a recorded reply but no sent-message row is worse than a lead the worker
    retries from scratch.
    """
    factory = _get_session_factory()

    async def _main() -> T:
        async with factory() as session:
            try:
                result = await work(session)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise

    return _get_loop().run_until_complete(_main())


def run(coro: Awaitable[Any]) -> Any:
    """Run a bare coroutine on the worker's loop."""
    return _get_loop().run_until_complete(coro)
