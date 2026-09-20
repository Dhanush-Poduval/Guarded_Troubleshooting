"""Purge indexed catalog data and every cached plan derived from it.

Run this when swapping the synthetic fixture catalog for the official dataset.

A cached plan holds deeplink URIs copied from whichever catalog was loaded at the time.
Once the catalog changes, those URIs may not exist any more, and the evaluation treats a
URI absent from the catalog as a fabricated deeplink. So cached plans are purged with the
catalog rather than migrated alongside it.

    python -m scripts.reset_catalog          # show what would be deleted
    python -m scripts.reset_catalog --yes    # actually delete
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from app.config import get_settings
from app.db.indexes import drop_indexes
from app.db.session import connect

COUNT_SQL = """
SELECT
    (SELECT count(*) FROM deeplink_catalog),
    (SELECT count(*) FROM plan_cache),
    (SELECT count(*) FROM plan_cache_vector),
    (SELECT count(*) FROM deeplink_catalog WHERE source = 'synthetic'),
    (SELECT count(*) FROM plan_cache WHERE catalog_source = 'synthetic')
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes", action="store_true", help="perform the deletion instead of a dry run"
    )
    args = parser.parse_args()

    settings = get_settings()

    try:
        with connect(settings, register_vector_types=False) as conn:
            catalog, plans, vectors, syn_catalog, syn_plans = conn.execute(
                COUNT_SQL
            ).fetchone()

            print(f"deeplink_catalog    {catalog:>6}  ({syn_catalog} synthetic)")
            print(f"plan_cache          {plans:>6}  ({syn_plans} built on synthetic)")
            print(f"plan_cache_vector   {vectors:>6}")

            if not args.yes:
                print()
                print("Dry run. Re-run with --yes to delete all of the above.")
                return 0

            with conn.transaction():
                drop_indexes(conn)
                # plan_cache_vector cascades from plan_cache.
                conn.execute("TRUNCATE plan_cache RESTART IDENTITY CASCADE")
                conn.execute("TRUNCATE deeplink_catalog RESTART IDENTITY CASCADE")

            print()
            print("Purged catalog, cached plans and their vectors. HNSW indexes dropped.")
            print("Next: point CATALOG_PATH at the real data, then run "
                  "`python -m scripts.build_catalog`.")
    except psycopg.OperationalError as exc:
        print(f"Could not connect: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
