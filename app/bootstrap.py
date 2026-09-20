"""Assembles the service and its dependencies.

This is the one place Phase 0-2 is bound. Replacing the mock with the real pipeline is a
change to `build_pipeline` and nothing else; no caller of TroubleshootingService knows
which implementation it holds.
"""

from __future__ import annotations

import logging

from psycopg_pool import AsyncConnectionPool

from app.catalog.loader import Catalog, load_catalog
from app.config import Settings, get_settings
from app.db.pool import create_pool
from app.db.session import connect
from app.embeddings.encoder import Encoder, get_encoder
from app.pipeline.port import PipelinePort
from app.retrieval.resolver import BM25Index, load_bm25_index
from app.service import TroubleshootingService

logger = logging.getLogger(__name__)


def load_catalog_state(settings: Settings) -> tuple[Catalog, BM25Index, frozenset[str]]:
    """Read the catalog once at startup.

    The permitted deeplink set is read from the database rather than the JSON file, so
    validation authorises exactly what was indexed. If the file and the database have
    drifted, the indexed rows are what the system can actually resolve.
    """
    catalog = load_catalog(settings=settings)
    with connect(settings) as conn:
        bm25 = load_bm25_index(conn)
        rows = conn.execute("SELECT deeplink FROM deeplink_catalog").fetchall()
    permitted = frozenset(row[0] for row in rows)

    if not permitted:
        raise RuntimeError(
            "No deeplinks indexed. Run: python -m scripts.build_catalog"
        )
    return catalog, bm25, permitted


def build_pipeline(
    settings: Settings, encoder: Encoder, bm25: BM25Index
) -> PipelinePort:
    """TEMPORARY: returns the mock. Swap this for the real Phase 0-2 pipeline."""
    from app.pipeline.mock.mock_pipeline import MockPipeline, connection_factory

    logger.warning(
        "Using the MOCK Phase 0-2 pipeline. Plans it produces are structural stand-ins, "
        "not real troubleshooting advice."
    )
    return MockPipeline(
        conn_factory=connection_factory(settings),
        encoder=encoder,
        bm25_index=bm25,
        settings=settings,
    )


def build_service(
    pool: AsyncConnectionPool,
    settings: Settings | None = None,
) -> TroubleshootingService:
    settings = settings or get_settings()
    encoder = get_encoder()
    catalog, bm25, permitted = load_catalog_state(settings)
    pipeline = build_pipeline(settings, encoder, bm25)

    return TroubleshootingService(
        pool=pool,
        encoder=encoder,
        pipeline=pipeline,
        permitted_deeplinks=permitted,
        catalog_source=catalog.source,
        settings=settings,
    )


def build_pool(settings: Settings | None = None) -> AsyncConnectionPool:
    return create_pool(settings or get_settings())
