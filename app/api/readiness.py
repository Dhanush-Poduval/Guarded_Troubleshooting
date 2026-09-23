"""Readiness probing for GET /health.

The contract states health returns 200 when the caching layer, model connections and
vector indexes are fully initialized. That is a readiness check, not a liveness check: a
process that is running but whose HNSW index has not been built serves nothing useful, and
reporting it healthy would hide a cold start rather than expose it.

Each component is probed independently so a failure names what is wrong.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.db.indexes import CACHE_INDEX, CATALOG_INDEX

logger = logging.getLogger(__name__)


@dataclass
class Readiness:
    database: bool = False
    embedding_model: bool = False
    vector_indexes: bool = False
    catalog_indexed: bool = False
    cache_ready: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.database
            and self.embedding_model
            and self.vector_indexes
            and self.catalog_indexed
            and self.cache_ready
        )

    @property
    def detail(self) -> str | None:
        return "; ".join(self.problems) if self.problems else None


async def probe(
    pool: AsyncConnectionPool,
    settings: Settings,
    *,
    encoder_loaded: bool,
) -> Readiness:
    state = Readiness()

    state.embedding_model = encoder_loaded
    if not encoder_loaded:
        state.problems.append("embedding model not loaded")

    try:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT 1")
            state.database = (await cur.fetchone())[0] == 1

            cur = await conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname = ANY(%s)",
                ([CATALOG_INDEX, CACHE_INDEX],),
            )
            present = {row[0] for row in await cur.fetchall()}
            missing = {CATALOG_INDEX, CACHE_INDEX} - present
            state.vector_indexes = not missing
            if missing:
                state.problems.append(
                    f"missing vector index: {', '.join(sorted(missing))}"
                )

            cur = await conn.execute(
                "SELECT count(*) FROM deeplink_catalog WHERE embedding IS NOT NULL"
            )
            indexed = (await cur.fetchone())[0]
            state.catalog_indexed = indexed > 0
            if not indexed:
                state.problems.append(
                    "deeplink catalog is empty; run scripts.build_catalog"
                )

            # The cache is queryable as soon as its table and index exist. It is allowed
            # to be empty: an empty cache serves correctly, just slowly.
            cur = await conn.execute(
                "SELECT count(*) FROM plan_cache WHERE cache_version = %s",
                (settings.cache_version,),
            )
            cached = (await cur.fetchone())[0]
            state.cache_ready = state.vector_indexes and state.database
            if state.cache_ready and cached == 0:
                logger.info(
                    "cache is empty at version %s; requests will take the cold path",
                    settings.cache_version,
                )
    except Exception as exc:  # noqa: BLE001 - reported, not raised, so health can answer
        state.problems.append(f"database unreachable: {exc}")
        logger.warning("readiness probe failed: %s", exc)

    return state
