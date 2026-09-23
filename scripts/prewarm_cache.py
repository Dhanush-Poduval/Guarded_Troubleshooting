"""Populate the semantic cache from the canonical query set.

The specification states that when a request omits siis_response the engine answers by
semantic lookup against pre-warmed cache entries. The cache is therefore part of the
serving path, not merely an optimisation, and must not start empty.

    python -m scripts.prewarm_cache

Each canonical query is run through the miss path once: pipeline, validation, then store.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from app.bootstrap import build_pool, build_service
from app.catalog.queries import load_queries
from app.config import get_settings
from app.runtime import use_compatible_event_loop


async def run() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    settings = get_settings()

    try:
        records = load_queries(settings=settings)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Pre-warming from {settings.queries_path} ({len(records)} queries)")
    with_text = sum(1 for r in records if r.siis)
    print(f"  {with_text} carry SIIS reference text")

    pool = build_pool(settings)
    await pool.open()
    try:
        service = build_service(pool, settings)
        started = time.perf_counter()
        cached = 0
        for record in records:
            # Force the pipeline: each canonical query must get its own plan rather
            # than being absorbed by a neighbour already in the cache.
            outcome = await service.troubleshoot(
                record.query,
                siis_response=record.siis.as_text() if record.siis else None,
                force_pipeline=True,
                origin="prewarm",
            )
            status = "cached" if not outcome.fallback else f"fallback={outcome.fallback}"
            if status == "cached":
                cached += 1
            print(f"  [{outcome.latency_ms:7.1f} ms] {status:<16} {record.query[:64]}")
        elapsed = time.perf_counter() - started
        print()
        print(f"Pre-warmed {cached}/{len(records)} plans in {elapsed:.1f}s")
    finally:
        await pool.close()
    return 0


def main() -> int:
    use_compatible_event_loop()
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
