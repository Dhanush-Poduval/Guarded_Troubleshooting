"""The semantic plan cache.

A plan is looked up by meaning rather than by string. Every phrasing of a cached plan gets
its own vector row, so an unseen rewording can match. Indexing only one canonical key is
the classic failure here: paraphrases miss, the expensive pipeline runs again, and both
latency and cost go up.

Only validated plans are stored, and a plan read back out is re-validated before use,
because the rules that admitted it may have tightened since. cache_version exists so that
a rule change can invalidate every stored plan at once instead of leaving stale ones to be
discovered one request at a time.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import numpy as np
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from app.config import Settings, get_settings
from app.contract.schema import ContextDeeplinkResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheHit:
    plan_id: int
    similarity: float
    matched_text: str
    variation_kind: str
    canonical_query: str
    plan: ContextDeeplinkResponse
    query_variations: list[str]
    cache_version: int
    catalog_source: str
    # Best similarity achieved by any OTHER plan, and the gap to it. None when only one
    # plan is cached, in which case there is nothing to be ambiguous with.
    runner_up_similarity: float | None = None

    @property
    def margin(self) -> float | None:
        if self.runner_up_similarity is None:
            return None
        return self.similarity - self.runner_up_similarity


@dataclass(frozen=True)
class CacheMiss:
    """Why a lookup did not produce a usable hit, for metrics and for debugging."""

    reason: str  # 'empty' | 'below_threshold' | 'ambiguous'
    best_similarity: float | None = None
    runner_up_similarity: float | None = None


async def lookup(
    conn: AsyncConnection,
    query_vector: np.ndarray,
    settings: Settings | None = None,
    *,
    threshold: float | None = None,
) -> CacheHit | None:
    """Find the best cached plan, or None.

    Two conditions must hold. The best match must clear the similarity threshold, and it
    must beat the best match belonging to any other plan by the configured margin.

    The margin exists because on this dataset a threshold alone is not discriminative:
    every query concerns the screen, so two genuinely different problems can be phrased
    almost identically. When two plans are near-equally close, answering from either is a
    guess, and a wrong plan is worse than a slow one.
    """
    hit, _ = await lookup_detailed(conn, query_vector, settings, threshold=threshold)
    return hit


async def lookup_detailed(
    conn: AsyncConnection,
    query_vector: np.ndarray,
    settings: Settings | None = None,
    *,
    threshold: float | None = None,
) -> tuple[CacheHit | None, CacheMiss | None]:
    """lookup(), but also reporting why a miss happened."""
    settings = settings or get_settings()
    threshold = settings.cache_similarity_threshold if threshold is None else threshold

    async with conn.transaction():
        # Cache lookup is failure-safe: a missed neighbour degrades to a cache miss and
        # the pipeline runs, so the default candidate list is adequate here.
        await conn.execute(
            "SELECT set_config('hnsw.ef_search', %s, true)",
            (str(int(settings.hnsw_ef_search_cache)),),
        )
        cur = await conn.execute(
            """
            SELECT p.id,
                   1 - (v.embedding <=> %s) AS similarity,
                   v.variation_text,
                   v.variation_kind,
                   p.canonical_query,
                   p.plan,
                   p.query_variations,
                   p.cache_version,
                   p.catalog_source
            FROM plan_cache_vector v
            JOIN plan_cache p ON p.id = v.plan_id
            WHERE p.cache_version = %s
            ORDER BY v.embedding <=> %s, p.id, v.id
            LIMIT %s
            """,
            (
                query_vector,
                settings.cache_version,
                query_vector,
                int(settings.cache_candidate_pool),
            ),
        )
        rows = await cur.fetchall()

    if not rows:
        return None, CacheMiss(reason="empty")

    best = rows[0]
    best_similarity = float(best[1])

    # The best row belonging to a different plan. Rows are already distance-ordered, so
    # the first one that appears is that plan's best.
    runner = next((row for row in rows if row[0] != best[0]), None)
    runner_similarity = float(runner[1]) if runner is not None else None

    if best_similarity < threshold:
        return None, CacheMiss(
            reason="below_threshold",
            best_similarity=best_similarity,
            runner_up_similarity=runner_similarity,
        )

    if (
        runner_similarity is not None
        and best_similarity - runner_similarity < settings.cache_ambiguity_margin
    ):
        logger.info(
            "cache lookup ambiguous: best %.4f vs runner-up %.4f from another plan "
            "(margin %.4f < %.4f), falling through to the pipeline",
            best_similarity, runner_similarity,
            best_similarity - runner_similarity, settings.cache_ambiguity_margin,
        )
        return None, CacheMiss(
            reason="ambiguous",
            best_similarity=best_similarity,
            runner_up_similarity=runner_similarity,
        )

    return (
        CacheHit(
            plan_id=best[0],
            similarity=best_similarity,
            matched_text=best[2],
            variation_kind=best[3],
            canonical_query=best[4],
            plan=ContextDeeplinkResponse.model_validate(best[5]),
            query_variations=list(best[6] or []),
            cache_version=best[7],
            catalog_source=best[8],
            runner_up_similarity=runner_similarity,
        ),
        None,
    )


async def record_hit(conn: AsyncConnection, plan_id: int) -> None:
    await conn.execute(
        "UPDATE plan_cache SET hit_count = hit_count + 1, last_hit_at = now() "
        "WHERE id = %s",
        (plan_id,),
    )


async def store_plan(
    conn: AsyncConnection,
    *,
    canonical_query: str,
    original_query: str,
    plan: ContextDeeplinkResponse,
    query_variations: list[str],
    vectors: dict[str, np.ndarray],
    score: float | None,
    catalog_source: str,
    pipeline_model: str | None,
    pipeline_cost_usd: float,
    origin: str = "runtime",
    settings: Settings | None = None,
) -> int:
    """Write a validated plan and one vector per searchable phrasing.

    `vectors` maps phrase text to its embedding. The caller embeds, so that a single
    batched encode covers the canonical form, the original query and every paraphrase.

    Re-storing the same canonical query at the same cache_version replaces the existing
    entry rather than duplicating it.
    """
    settings = settings or get_settings()

    async with conn.transaction():
        cur = await conn.execute(
            """
            INSERT INTO plan_cache
                (canonical_query, original_query, plan, query_variations, score,
                 cache_version, catalog_source, pipeline_model, pipeline_cost_usd, origin)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (canonical_query, cache_version) DO UPDATE SET
                original_query    = EXCLUDED.original_query,
                plan              = EXCLUDED.plan,
                query_variations  = EXCLUDED.query_variations,
                score             = EXCLUDED.score,
                catalog_source    = EXCLUDED.catalog_source,
                pipeline_model    = EXCLUDED.pipeline_model,
                pipeline_cost_usd = EXCLUDED.pipeline_cost_usd,
                origin            = EXCLUDED.origin,
                validated_at      = now()
            RETURNING id
            """,
            (
                canonical_query,
                original_query,
                Jsonb(plan.model_dump(mode="json")),
                Jsonb(query_variations),
                score,
                settings.cache_version,
                catalog_source,
                pipeline_model,
                pipeline_cost_usd,
                origin,
            ),
        )
        plan_id = (await cur.fetchone())[0]

        # Replace vectors wholesale: a re-stored plan may have different paraphrases.
        await conn.execute("DELETE FROM plan_cache_vector WHERE plan_id = %s", (plan_id,))

        rows = []
        for text, vector in vectors.items():
            if text == canonical_query:
                kind = "canonical"
            elif text == original_query:
                kind = "original"
            else:
                kind = "paraphrase"
            rows.append((plan_id, text, kind, vector))

        async with conn.cursor() as write_cur:
            await write_cur.executemany(
                "INSERT INTO plan_cache_vector "
                "(plan_id, variation_text, variation_kind, embedding) "
                "VALUES (%s, %s, %s, %s)",
                rows,
            )

    logger.info(
        "cached plan %d (%s) with %d searchable phrasings", plan_id, canonical_query, len(rows)
    )
    return plan_id


async def record_metrics(
    conn: AsyncConnection,
    *,
    request_id: str,
    query: str,
    cache_hit: bool,
    similarity: float | None,
    matched_plan_id: int | None,
    pipeline_invoked: bool,
    validation_passed: bool | None,
    fallback: str | None,
    latency_ms: float,
    embed_ms: float | None,
    lookup_ms: float | None,
    pipeline_ms: float | None,
    cost_usd: float,
    cache_version: int,
    runner_up_similarity: float | None = None,
    cache_reject_reason: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO request_metrics
            (request_id, query, cache_hit, similarity, matched_plan_id, pipeline_invoked,
             validation_passed, fallback, latency_ms, embed_ms, lookup_ms, pipeline_ms,
             cost_usd, cache_version, runner_up_similarity, cache_reject_reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            request_id, query, cache_hit, similarity, matched_plan_id, pipeline_invoked,
            validation_passed, fallback, latency_ms, embed_ms, lookup_ms, pipeline_ms,
            cost_usd, cache_version, runner_up_similarity, cache_reject_reason,
        ),
    )


async def cache_stats(conn: AsyncConnection, settings: Settings | None = None) -> dict:
    """Aggregates backing the stats endpoint. All measured, never estimated."""
    settings = settings or get_settings()

    cur = await conn.execute(
        """
        SELECT count(*), coalesce(sum(hit_count), 0),
               count(*) FILTER (WHERE origin = 'prewarm'),
               count(*) FILTER (WHERE catalog_source = 'synthetic')
        FROM plan_cache WHERE cache_version = %s
        """,
        (settings.cache_version,),
    )
    plans, hits, prewarmed, synthetic = await cur.fetchone()

    cur = await conn.execute("SELECT count(*) FROM plan_cache_vector")
    vectors = (await cur.fetchone())[0]

    cur = await conn.execute(
        """
        SELECT count(*) FILTER (WHERE cache_reject_reason = 'below_threshold'),
               count(*) FILTER (WHERE cache_reject_reason = 'ambiguous'),
               count(*) FILTER (WHERE cache_reject_reason = 'failed_revalidation')
        FROM request_metrics
        """
    )
    below, ambiguous, revalidation = await cur.fetchone()

    cur = await conn.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE cache_hit),
               percentile_disc(0.5) WITHIN GROUP (ORDER BY latency_ms)
                   FILTER (WHERE cache_hit),
               percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms)
                   FILTER (WHERE cache_hit),
               percentile_disc(0.5) WITHIN GROUP (ORDER BY latency_ms)
                   FILTER (WHERE NOT cache_hit),
               percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms)
                   FILTER (WHERE NOT cache_hit),
               coalesce(sum(cost_usd), 0)
        FROM request_metrics
        """
    )
    total, hit_count, hit_p50, hit_p95, miss_p50, miss_p95, cost = await cur.fetchone()

    return {
        "cache_version": settings.cache_version,
        "similarity_threshold": settings.cache_similarity_threshold,
        "cached_plans": plans,
        "cached_vectors": vectors,
        "prewarmed_plans": prewarmed,
        "plans_from_synthetic_catalog": synthetic,
        "lifetime_plan_hits": hits,
        "requests_total": total,
        "requests_cache_hit": hit_count,
        "hit_rate": (hit_count / total) if total else None,
        "hit_latency_p50_ms": float(hit_p50) if hit_p50 is not None else None,
        "hit_latency_p95_ms": float(hit_p95) if hit_p95 is not None else None,
        "miss_latency_p50_ms": float(miss_p50) if miss_p50 is not None else None,
        "miss_latency_p95_ms": float(miss_p95) if miss_p95 is not None else None,
        "total_cost_usd": float(cost),
        "ambiguity_margin": settings.cache_ambiguity_margin,
        "rejected_below_threshold": below,
        "rejected_ambiguous": ambiguous,
        "rejected_failed_revalidation": revalidation,
    }
