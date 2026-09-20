"""Populate the semantic cache from the canonical query set.

The specification states that when a request omits siis_response the engine answers by
semantic lookup against pre-warmed cache entries. The cache is therefore part of the
serving path, not merely an optimisation, and must not start empty.

    python -m scripts.prewarm_cache

Each canonical query is run through the miss path once: pipeline, validation, then store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import sys
import time

from app.bootstrap import build_pool, build_service
from app.config import get_settings
from app.runtime import use_compatible_event_loop


async def run() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    settings = get_settings()

    queries_path = pathlib.Path(settings.queries_path)
    if not queries_path.exists():
        print(f"Query set not found at {queries_path}", file=sys.stderr)
        return 1

    raw = json.loads(queries_path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        items, synthetic = raw, False
    else:
        items, synthetic = raw.get("queries", []), bool(raw.get("_synthetic"))

    queries = [item["query"] if isinstance(item, dict) else str(item) for item in items]
    print(f"Pre-warming from {queries_path} ({len(queries)} queries)")
    if synthetic:
        print("  *** SYNTHETIC QUERY SET. Not evaluation data. ***")

    pool = build_pool(settings)
    await pool.open()
    try:
        service = build_service(pool, settings)
        started = time.perf_counter()
        cached = 0
        for query in queries:
            # Force the pipeline: each canonical query must get its own plan rather
            # than being absorbed by a neighbour already in the cache.
            outcome = await service.troubleshoot(
                query, force_pipeline=True, origin="prewarm"
            )
            status = "cached" if not outcome.fallback else f"fallback={outcome.fallback}"
            if status == "cached":
                cached += 1
            print(f"  [{outcome.latency_ms:7.1f} ms] {status:<16} {query}")
        elapsed = time.perf_counter() - started
        print(f"\nPre-warmed {cached}/{len(queries)} plans in {elapsed:.1f}s")
    finally:
        await pool.close()
    return 0


def main() -> int:
    use_compatible_event_loop()
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
