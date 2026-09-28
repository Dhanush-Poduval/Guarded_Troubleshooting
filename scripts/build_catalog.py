"""Embed the deeplink catalog into PostgreSQL and build the HNSW index.

    docker compose up -d
    python -m scripts.init_db
    python -m scripts.build_catalog

Safe to re-run: the catalog is replaced wholesale each time.
"""

from __future__ import annotations

import logging
import sys

import psycopg

from app.catalog.indexer import index_catalog
from app.catalog.loader import load_catalog
from app.config import get_settings
from app.db.indexes import CACHE_INDEX, CATALOG_INDEX, index_definition
from app.db.session import connect


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = get_settings()

    catalog = load_catalog(settings=settings)
    print(f"Catalog: {catalog.path} ({len(catalog.entries)} entries, {catalog.source})")
    if catalog.is_synthetic:
        print()
        print("  *** SYNTHETIC DATA. Results are not meaningful for evaluation. ***")
        print()

    print(f"Embedding with {settings.embedding_model} ({settings.embedding_dim}d)")

    try:
        with connect(settings) as conn:
            report = index_catalog(conn, catalog=catalog, settings=settings)
            for name in (CATALOG_INDEX, CACHE_INDEX):
                print(f"  {index_definition(conn, name)}")
    except psycopg.OperationalError as exc:
        print(f"Could not connect: {exc}", file=sys.stderr)
        print("Is the database running? Try: docker compose up -d", file=sys.stderr)
        return 1

    print(
        f"Indexed {report.entries} entries in {report.total_seconds:.2f}s "
        f"(embed {report.embed_seconds:.2f}s, write {report.write_seconds:.2f}s, "
        f"index {report.index_seconds:.2f}s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
