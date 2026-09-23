"""Loads the catalog into PostgreSQL and builds the HNSW index over it.

Order matters: rows are written first, the index second. pgvector builds an HNSW graph
faster over existing data than it does incrementally through inserts.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import psycopg

from app.catalog.loader import Catalog, load_catalog
from app.config import Settings, get_settings
from app.db.indexes import build_indexes, drop_indexes
from app.embeddings.encoder import Encoder, get_encoder

logger = logging.getLogger(__name__)


@dataclass
class IndexReport:
    entries: int
    source: str
    embed_seconds: float
    write_seconds: float
    index_seconds: float

    @property
    def total_seconds(self) -> float:
        return self.embed_seconds + self.write_seconds + self.index_seconds


def index_catalog(
    conn: psycopg.Connection,
    catalog: Catalog | None = None,
    encoder: Encoder | None = None,
    settings: Settings | None = None,
    *,
    replace: bool = True,
) -> IndexReport:
    settings = settings or get_settings()
    catalog = catalog or load_catalog(settings=settings)
    encoder = encoder or get_encoder()

    texts = [entry.match_text for entry in catalog.entries]

    started = time.perf_counter()
    vectors = encoder.encode_catalog_entries(texts)
    embed_seconds = time.perf_counter() - started

    started = time.perf_counter()
    with conn.transaction():
        if replace:
            # Dropping first means the graph is built once, over the final row set.
            drop_indexes(conn)
            conn.execute("TRUNCATE deeplink_catalog RESTART IDENTITY CASCADE")

        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO deeplink_catalog
                    (deeplink, description, message, qna_description,
                     original_type, match_text, embedding, source,
                     catalog_id, control_type, validation_deeplink, validation_key,
                     validation_result_type, validation_condition, validation_value)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (deeplink) DO UPDATE SET
                    description            = EXCLUDED.description,
                    message                = EXCLUDED.message,
                    qna_description        = EXCLUDED.qna_description,
                    original_type          = EXCLUDED.original_type,
                    match_text             = EXCLUDED.match_text,
                    embedding              = EXCLUDED.embedding,
                    source                 = EXCLUDED.source,
                    catalog_id             = EXCLUDED.catalog_id,
                    control_type           = EXCLUDED.control_type,
                    validation_deeplink    = EXCLUDED.validation_deeplink,
                    validation_key         = EXCLUDED.validation_key,
                    validation_result_type = EXCLUDED.validation_result_type,
                    validation_condition   = EXCLUDED.validation_condition,
                    validation_value       = EXCLUDED.validation_value
                """,
                [
                    (
                        entry.deeplink,
                        entry.description,
                        entry.message,
                        entry.qna_description,
                        entry.original_type,
                        entry.match_text,
                        vector,
                        catalog.source,
                        entry.catalog_id,
                        entry.control_type,
                        entry.validation.deeplink if entry.validation else None,
                        entry.validation.key if entry.validation else None,
                        entry.validation.result_type if entry.validation else None,
                        entry.validation.condition if entry.validation else None,
                        entry.validation.value if entry.validation else None,
                    )
                    for entry, vector in zip(catalog.entries, vectors)
                ],
            )
    write_seconds = time.perf_counter() - started

    started = time.perf_counter()
    with conn.transaction():
        build_indexes(conn, settings)
    index_seconds = time.perf_counter() - started

    report = IndexReport(
        entries=len(catalog.entries),
        source=catalog.source,
        embed_seconds=embed_seconds,
        write_seconds=write_seconds,
        index_seconds=index_seconds,
    )
    logger.info(
        "indexed %d %s catalog entries (embed %.2fs, write %.2fs, index %.2fs)",
        report.entries, report.source,
        report.embed_seconds, report.write_seconds, report.index_seconds,
    )
    return report
