"""Pooler detection, and the kwargs that follow from it.

The bug this guards against does not appear on connect. It appears later, under
concurrency, as InvalidCachedStatementError or a duplicate prepared-statement
name — which looks like an agent that intermittently drops replies rather than
like a misconfiguration.
"""

import pytest
from sqlalchemy.pool import NullPool

from app.core.engine import async_engine_kwargs, uses_transaction_pooler

POOLER = "postgresql+asyncpg://postgres.ref:pw@aws-0-eu-central-1.pooler.supabase.com:6543/postgres"
SESSION_POOLER = "postgresql+asyncpg://postgres.ref:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
DIRECT = "postgresql+asyncpg://postgres:pw@db.ref.supabase.co:5432/postgres"
LOCAL = "postgresql+asyncpg://sales:sales@postgres:5432/sales_agent"


@pytest.mark.parametrize("url", [POOLER, SESSION_POOLER])
def test_pooler_urls_are_detected(url: str) -> None:
    # The session pooler on 5432 is caught by hostname rather than port. It keeps
    # a backend for the whole session so it does not strictly need this, but a
    # host that says "pooler" is not somewhere to assume prepared statements.
    assert uses_transaction_pooler(url)


@pytest.mark.parametrize("url", [DIRECT, LOCAL])
def test_direct_connections_are_left_alone(url: str) -> None:
    assert not uses_transaction_pooler(url)


def test_pooler_disables_caches_pooling_and_fixed_names() -> None:
    kwargs = async_engine_kwargs(POOLER)
    assert kwargs["poolclass"] is NullPool
    connect = kwargs["connect_args"]
    assert connect["prepared_statement_cache_size"] == 0
    assert connect["statement_cache_size"] == 0
    # Unique per call, or two clients sharing a backend collide.
    name_func = connect["prepared_statement_name_func"]
    assert name_func() != name_func()


def test_direct_connection_keeps_the_defaults() -> None:
    kwargs = async_engine_kwargs(DIRECT)
    assert "poolclass" not in kwargs
    assert "connect_args" not in kwargs
    assert kwargs["pool_pre_ping"] is True


def test_pool_sizing_is_dropped_for_the_pooler_but_kept_otherwise() -> None:
    # NullPool takes no sizing arguments; passing them raises at engine creation,
    # which is why the worker's pool_size cannot simply be forwarded.
    pooled = async_engine_kwargs(POOLER, pool_size=5, max_overflow=5)
    assert "pool_size" not in pooled and "max_overflow" not in pooled

    direct = async_engine_kwargs(DIRECT, pool_size=5, max_overflow=5)
    assert direct["pool_size"] == 5 and direct["max_overflow"] == 5


def test_engines_actually_build_under_both_shapes() -> None:
    # Cheap, but it is the assertion that catches a kwarg SQLAlchemy or asyncpg
    # would reject — the reason this file exists rather than a code review.
    from app.core.engine import build_async_engine

    assert build_async_engine(POOLER) is not None
    assert build_async_engine(DIRECT, pool_size=5, max_overflow=5) is not None


def test_a_malformed_url_does_not_raise() -> None:
    assert uses_transaction_pooler("not a url") is False
