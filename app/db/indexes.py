"""HNSW index creation and per-query search tuning.

Indexes live here rather than in a migration because their parameters are configurable
and because pgvector builds the graph faster once the rows already exist. Creating them
in the migration would mean building an empty graph and then inserting into it.

Distance metric is cosine (vector_cosine_ops). The embedding model emits L2-normalised
vectors, so cosine and inner product rank identically; cosine is chosen because its
distance is bounded to [0, 2], which makes the similarity threshold interpretable as
`similarity = 1 - distance`.
"""

from __future__ import annotations

import logging

import psycopg

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

CATALOG_INDEX = "deeplink_catalog_embedding_hnsw"
CACHE_INDEX = "plan_cache_vector_embedding_hnsw"


def _create_hnsw(
    conn: psycopg.Connection,
    *,
    index_name: str,
    table: str,
    column: str,
    m: int,
    ef_construction: int,
) -> None:
    # HNSW build options cannot be passed as bound parameters, so they are coerced to int
    # here and never interpolated as free text. Settings already bounds their range.
    m = int(m)
    ef_construction = int(ef_construction)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} "
        f"USING hnsw ({column} vector_cosine_ops) "
        f"WITH (m = {m}, ef_construction = {ef_construction})"
    )
    logger.info(
        "ensured %s on %s.%s (m=%d, ef_construction=%d)",
        index_name, table, column, m, ef_construction,
    )


def build_indexes(
    conn: psycopg.Connection,
    settings: Settings | None = None,
) -> list[str]:
    """Create both HNSW indexes if absent. Returns the index names ensured."""
    settings = settings or get_settings()

    _create_hnsw(
        conn,
        index_name=CATALOG_INDEX,
        table="deeplink_catalog",
        column="embedding",
        m=settings.hnsw_m,
        ef_construction=settings.hnsw_ef_construction,
    )
    _create_hnsw(
        conn,
        index_name=CACHE_INDEX,
        table="plan_cache_vector",
        column="embedding",
        m=settings.hnsw_m,
        ef_construction=settings.hnsw_ef_construction,
    )
    return [CATALOG_INDEX, CACHE_INDEX]


def drop_indexes(conn: psycopg.Connection) -> None:
    """Used before a bulk reload, since building after the load is faster."""
    for name in (CATALOG_INDEX, CACHE_INDEX):
        conn.execute(f"DROP INDEX IF EXISTS {name}")


def set_ef_search(conn: psycopg.Connection, ef_search: int) -> None:
    """Set hnsw.ef_search for the current transaction.

    Must run inside a transaction: SET LOCAL is scoped to it. A higher value widens the
    candidate list, trading query time for recall.
    """
    conn.execute("SELECT set_config('hnsw.ef_search', %s, true)", (str(int(ef_search)),))


def index_exists(conn: psycopg.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = %s",
        (index_name,),
    ).fetchone()
    return row is not None


def index_definition(conn: psycopg.Connection, index_name: str) -> str | None:
    row = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = %s",
        (index_name,),
    ).fetchone()
    return row[0] if row else None
