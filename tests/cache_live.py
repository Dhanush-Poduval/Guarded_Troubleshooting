"""Live semantic-cache performance test.

Proves:

1. First request may invoke the real pipeline.
2. Result is stored in semantic cache.
3. Repeated request hits cache.
4. Cache hit does NOT invoke Gemini / RealPipeline.
5. Cached response completes in <300 ms.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.bootstrap import (
    build_pool,
    build_service,
)
from app.config import get_settings


# ============================================================
# TEST QUERY
# ============================================================

QUERY = (
    "My Samsung A115G tablet screen flashes and then goes "
    "completely blank whenever I tap to open an email in Gmail, "
    "and after it works for a short time it goes blank again."
)


# ============================================================
# SIIS LOADER
# ============================================================

SIIS_PATH = Path(
    "data/official/siis_responses.json"
)


def _load_siis_records() -> list[dict]:
    """Load official SIIS dataset regardless of wrapper shape."""

    data = json.loads(
        SIIS_PATH.read_text(
            encoding="utf-8"
        )
    )

    if isinstance(data, list):
        return data

    if isinstance(data, dict):

        for key in (
            "records",
            "data",
            "responses",
            "items",
        ):
            value = data.get(key)

            if isinstance(
                value,
                list,
            ):
                return value

        # Handle:
        #
        # {
        #   "1": {...},
        #   "2": {...}
        # }

        values = list(
            data.values()
        )

        if (
            values
            and all(
                isinstance(
                    item,
                    dict,
                )
                for item
                in values
            )
        ):
            return values

    raise RuntimeError(
        "Unsupported SIIS JSON structure"
    )


def load_siis_response() -> str:
    """Find the SIIS response corresponding to the test query."""

    records = _load_siis_records()

    # --------------------------------------------------------
    # Prefer exact/near Samsung A115G record.
    # --------------------------------------------------------

    for record in records:

        original_query = str(
            record.get(
                "original_query",
                "",
            )
        )

        if (
            "A115G"
            in original_query
            and "Gmail"
            in original_query
        ):

            siis = record.get(
                "siis_response"
            )

            if siis is None:
                continue

            if isinstance(
                siis,
                str,
            ):
                return siis

            return json.dumps(
                siis,
                ensure_ascii=False,
            )

    raise RuntimeError(
        "Could not find Samsung A115G/Gmail "
        "case in official SIIS dataset"
    )


# ============================================================
# TEST
# ============================================================


@pytest.mark.asyncio
async def test_semantic_cache_under_300ms():

    print()
    print("=" * 80)
    print("SEMANTIC CACHE PERFORMANCE TEST")
    print("=" * 80)

    settings = get_settings()

    siis_response = (
        load_siis_response()
    )

    # --------------------------------------------------------
    # Build real production dependencies.
    # --------------------------------------------------------

    pool = build_pool(
        settings
    )

    await pool.open()

    try:

        service = build_service(
            pool,
            settings,
        )

        print()
        print(
            "Pipeline:",
            service._pipeline.name,
        )

        assert (
            service._pipeline.name
            == "real"
        ), (
            "Cache test must use "
            "RealPipeline"
        )

        # ====================================================
        # FIRST REQUEST
        #
        # force_pipeline=True guarantees this request executes
        # Phase 0-2 and stores a fresh validated plan.
        # ====================================================

        print()
        print("=" * 80)
        print("FIRST REQUEST - CACHE WARMING")
        print("=" * 80)

        first_started = (
            time.perf_counter()
        )

        first = await service.troubleshoot(
            QUERY,
            siis_response,
            force_pipeline=True,
            origin="runtime",
        )

        first_wall_ms = (
            time.perf_counter()
            - first_started
        ) * 1000

        print(
            f"Wall time:       "
            f"{first_wall_ms:.2f} ms"
        )

        print(
            f"Service latency: "
            f"{first.latency_ms:.2f} ms"
        )

        print(
            "Cache hit:       ",
            first.cache_hit,
        )

        print(
            "Pipeline invoked:",
            first.pipeline_invoked,
        )

        print(
            "Pipeline time:   ",
            (
                f"{first.pipeline_ms:.2f} ms"
                if first.pipeline_ms
                is not None
                else "N/A"
            ),
        )

        print(
            "Fallback:        ",
            first.fallback,
        )

        print(
            "Contexts:        ",
            len(
                first.response.contexts
            ),
        )

        # Because force_pipeline=True:
        assert (
            first.pipeline_invoked
            is True
        )

        assert (
            first.cache_hit
            is False
        )

        assert (
            first.response.contexts
        ), (
            "First request produced no valid "
            "contexts, so nothing could be cached."
        )

        # ====================================================
        # SECOND REQUEST
        #
        # Same query.
        #
        # This MUST come from cache.
        # ====================================================

        print()
        print("=" * 80)
        print("SECOND REQUEST - EXACT CACHE HIT")
        print("=" * 80)

        second_started = (
            time.perf_counter()
        )

        second = await service.troubleshoot(
            QUERY,
            siis_response,
        )

        second_wall_ms = (
            time.perf_counter()
            - second_started
        ) * 1000

        print(
            f"Wall time:       "
            f"{second_wall_ms:.2f} ms"
        )

        print(
            f"Service latency: "
            f"{second.latency_ms:.2f} ms"
        )

        print(
            f"Embedding time:  "
            f"{second.embed_ms:.2f} ms"
        )

        print(
            f"Lookup time:     "
            f"{second.lookup_ms:.2f} ms"
        )

        print(
            "Cache hit:       ",
            second.cache_hit,
        )

        print(
            "Pipeline invoked:",
            second.pipeline_invoked,
        )

        print(
            "Similarity:      ",
            second.similarity,
        )

        print(
            "Pipeline time:   ",
            second.pipeline_ms,
        )

        # ----------------------------------------------------
        # Core cache correctness checks
        # ----------------------------------------------------

        assert (
            second.cache_hit
            is True
        ), (
            "Second identical request did NOT "
            "hit semantic cache."
        )

        assert (
            second.pipeline_invoked
            is False
        ), (
            "Pipeline was invoked during a cache hit. "
            "Fast-path caching is not working."
        )

        assert (
            second.pipeline_ms
            is None
        )

        assert (
            second.response.contexts
        )

        # ====================================================
        # SLA
        # ====================================================

        print()
        print("=" * 80)
        print("CACHE SLA")
        print("=" * 80)

        print(
            f"Measured cache hit: "
            f"{second_wall_ms:.2f} ms"
        )

        print(
            "Required:           "
            "< 300.00 ms"
        )

        if second_wall_ms < 300:

            print(
                "RESULT:             PASS ✓"
            )

        else:

            print(
                "RESULT:             FAIL ✗"
            )

        assert (
            second_wall_ms
            < 300.0
        ), (
            "\n"
            "Cached request exceeded 300 ms.\n"
            f"Measured: {second_wall_ms:.2f} ms\n"
            f"Embed:    {second.embed_ms:.2f} ms\n"
            f"Lookup:   {second.lookup_ms:.2f} ms\n"
        )

        # ====================================================
        # RESPONSE EQUALITY
        # ====================================================

        first_json = (
            first.response.model_dump(
                mode="json"
            )
        )

        second_json = (
            second.response.model_dump(
                mode="json"
            )
        )

        assert (
            first_json
            == second_json
        ), (
            "Cached plan differs from "
            "the plan that was stored."
        )

        print()
        print("=" * 80)
        print(
            "SEMANTIC CACHE FAST PATH PASSED"
        )
        print("=" * 80)

        print(
            "✓ RealPipeline used for cache warming"
        )

        print(
            "✓ Valid plan cached"
        )

        print(
            "✓ Second request hit cache"
        )

        print(
            "✓ Pipeline bypassed on cache hit"
        )

        print(
            "✓ Cached plan revalidated"
        )

        print(
            "✓ Cached response identical"
        )

        print(
            f"✓ Cache response "
            f"{second_wall_ms:.2f} ms < 300 ms"
        )

    finally:

        await pool.close()
