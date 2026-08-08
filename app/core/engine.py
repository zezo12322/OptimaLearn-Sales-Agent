"""Engine construction, and the one thing a transaction pooler changes.

Supabase's transaction pooler (and every other PgBouncer in transaction mode)
hands each transaction whatever backend connection is free. Prepared statements
live on a backend connection, so the statement asyncpg prepared a moment ago may
not exist on the one it gets next — and asyncpg names them in numeric order, so
the name it picks may already belong to somebody else's statement.

That does not fail on connect. It fails later, under concurrency, as
``InvalidCachedStatementError`` or a duplicate prepared-statement name — which
reads as an agent that intermittently drops replies rather than as a
misconfiguration. A clean failure would have been kinder.

SQLAlchemy's remedy, applied here:

* ``prepared_statement_cache_size=0`` — stop caching statements that may not
  exist on the next connection.
* ``prepared_statement_name_func`` — give every statement a unique name so two
  clients sharing a backend cannot collide.
* ``NullPool`` — do not hold connections open. Pooling on top of a pooler
  accumulates server-side state the pooler cannot clean up, and the pooler is
  already doing the pooling.

None of that is free — no cache, no connection reuse — so it is applied only
when the URL actually points at a pooler.
"""

from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

#: Supabase's transaction pooler. Its session pooler on 5432 keeps a backend for
#: the whole session, so prepared statements are safe there.
TRANSACTION_POOLER_PORT = 6543


def uses_transaction_pooler(url: str) -> bool:
    """True when this URL points at a connection pooler in transaction mode.

    Two signals, because deployments spell it differently: the port Supabase
    publishes for transaction mode, and a hostname that says so. Neither alone
    covers a self-hosted PgBouncer on a custom port behind a name that mentions
    it.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.port == TRANSACTION_POOLER_PORT:
        return True
    return "pooler." in host or "pgbouncer" in host


def async_engine_kwargs(url: str, **overrides: Any) -> dict[str, Any]:
    """Engine kwargs for ``url``, pooler-safe when the URL calls for it."""
    kwargs: dict[str, Any] = {"pool_pre_ping": True}

    if uses_transaction_pooler(url):
        kwargs["poolclass"] = NullPool
        kwargs["connect_args"] = {
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
            # asyncpg's own cache, separate from SQLAlchemy's.
            "statement_cache_size": 0,
        }
        # NullPool takes no sizing arguments; passing them raises.
        overrides = {
            k: v for k, v in overrides.items() if k not in ("pool_size", "max_overflow")
        }

    kwargs.update(overrides)
    return kwargs


def build_async_engine(url: str, **overrides: Any) -> AsyncEngine:
    return create_async_engine(url, **async_engine_kwargs(url, **overrides))
