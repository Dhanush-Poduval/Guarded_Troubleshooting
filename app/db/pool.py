"""Async connection pool for the request path.

psycopg's documentation is explicit that a pool must not be opened at import time, so the
pool is constructed with open=False and opened from the application lifespan. That keeps
module import free of side effects and avoids a startup hang when the database is down.

Pool sizing is deliberately modest. Postgres connections are not free, and a pool larger
than the database can usefully serve converts a queue into contention. The work per
request here is a few milliseconds of query time, so a small pool sustains a high request
rate; raise max_size only in response to get_stats() showing real queue waits.
"""

from __future__ import annotations

import logging

from psycopg_pool import AsyncConnectionPool

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


async def _configure(conn) -> None:
    """Register pgvector adapters on every pooled connection.

    Without this, a numpy array bound into a `<=>` expression is sent as
    double precision[] and the operator fails to resolve.
    """
    from pgvector.psycopg import register_vector_async

    await register_vector_async(conn)


def create_pool(
    settings: Settings | None = None,
    *,
    min_size: int = 2,
    max_size: int = 10,
) -> AsyncConnectionPool:
    """Build an unopened pool. Call `await pool.open()` from the lifespan."""
    settings = settings or get_settings()
    return AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=min_size,
        max_size=max_size,
        open=False,
        configure=_configure,
        # Recycle connections so a long-lived process does not hold stale sessions.
        max_lifetime=30 * 60,
        max_idle=5 * 60,
        # Fail fast rather than letting a request hang on an exhausted pool.
        timeout=10.0,
        name="troubleshooting",
    )


def pool_stats(pool: AsyncConnectionPool) -> dict:
    """Snapshot for the health and stats endpoints."""
    stats = pool.get_stats()
    return {
        "pool_min": pool.min_size,
        "pool_max": pool.max_size,
        "pool_size": stats.get("pool_size"),
        "pool_available": stats.get("pool_available"),
        "requests_waiting": stats.get("requests_waiting"),
        "requests_errors": stats.get("requests_errors"),
        "connections_num": stats.get("connections_num"),
    }
