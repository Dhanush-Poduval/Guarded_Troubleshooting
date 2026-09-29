"""Application dependency assembly.

Production wiring:

    API
      -> TroubleshootingService
          -> semantic cache
          -> RealPipeline
              -> Phase 0 Query Enrichment
              -> Phase 1 Structure Extraction
              -> Phase 2 Deeplink Resolution
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


def load_catalog_state(
    settings: Settings,
) -> tuple[
    Catalog,
    BM25Index,
    frozenset[str],
]:
    """
    Load catalog, BM25 index and every catalog-authorized
    actionable/validation deeplink.
    """

    catalog = load_catalog(
        settings=settings
    )

    with connect(settings) as conn:

        bm25 = load_bm25_index(
            conn
        )

        # IMPORTANT:
        #
        # Both URI columns belong to the official catalog.
        #
        # `deeplink` is used for actionableDeeplink.
        # `validation_deeplink` is used for validationDeeplink.
        #
        # Validation receives one permitted URI collection, so
        # that collection must contain BOTH types.
        rows = conn.execute(
            """
            SELECT
                deeplink,
                validation_deeplink
            FROM deeplink_catalog
            """
        ).fetchall()

    permitted: set[str] = set()

    for (
        actionable_deeplink,
        validation_deeplink,
    ) in rows:

        if actionable_deeplink:
            permitted.add(
                actionable_deeplink
            )

        if validation_deeplink:
            permitted.add(
                validation_deeplink
            )

    permitted_deeplinks = frozenset(
        permitted
    )

    if not permitted_deeplinks:
        raise RuntimeError(
            "No deeplinks indexed. "
            "Run: python -m scripts.build_catalog"
        )

    logger.info(
        "Loaded %d permitted actionable/validation deeplinks",
        len(permitted_deeplinks),
    )

    return (
        catalog,
        bm25,
        permitted_deeplinks,
    )


def build_pipeline(
    settings: Settings,
    encoder: Encoder,
    bm25: BM25Index,
) -> PipelinePort:
    """Build the real Phase 0-2 pipeline."""

    # connection_factory currently lives beside the mock,
    # but it is only a DB connection factory.
    from app.pipeline.mock.mock_pipeline import (
        connection_factory,
    )

    from app.pipeline.real.pipeline import (
        RealPipeline,
    )

    logger.info(
        "Using REAL Phase 0-2 troubleshooting pipeline."
    )

    return RealPipeline(
        conn_factory=connection_factory(
            settings
        ),
        encoder=encoder,
        bm25_index=bm25,
        settings=settings,
    )


def build_service(
    pool: AsyncConnectionPool,
    settings: Settings | None = None,
) -> TroubleshootingService:
    """Build production troubleshooting service."""

    settings = (
        settings
        or get_settings()
    )

    encoder = get_encoder()

    (
        catalog,
        bm25,
        permitted,
    ) = load_catalog_state(
        settings
    )

    pipeline = build_pipeline(
        settings,
        encoder,
        bm25,
    )

    service = TroubleshootingService(
        pool=pool,
        encoder=encoder,
        pipeline=pipeline,
        permitted_deeplinks=permitted,
        catalog_source=catalog.source,
        settings=settings,
    )

    logger.info(
        "TroubleshootingService initialized "
        "with pipeline=%s",
        pipeline.name,
    )

    return service


def build_pool(
    settings: Settings | None = None,
) -> AsyncConnectionPool:
    """Build PostgreSQL connection pool."""

    return create_pool(
        settings
        or get_settings()
    )
