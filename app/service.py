"""The troubleshooting service: cache-first request handling.

    embed query
      -> semantic cache lookup
           hit  -> re-validate -> serve, pipeline never invoked
           miss -> run Phase 0-2 -> validate -> cache -> serve
      -> record metrics either way

Two rules shape everything here.

Validation is never bypassed. A cached plan is re-checked before it is served, not merely
when it is written, because the rules may have tightened and because a plan built against
a different catalog can hold URIs that no longer exist. A cached plan that fails
re-validation is discarded and treated as a miss rather than served.

Embedding is CPU-bound and synchronous, so it runs on a worker thread. Leaving it on the
event loop would block every other in-flight request for the duration of the encode.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

import anyio
import numpy as np
from psycopg_pool import AsyncConnectionPool

from app.cache import store
from app.config import Settings, get_settings
from app.contract.schema import ContextDeeplinkResponse
from app.embeddings.encoder import Encoder
from app.pipeline.port import PipelinePort, PipelineRequest
from app.validation.rules import validate_plan

logger = logging.getLogger(__name__)


@dataclass
class TroubleshootOutcome:
    """One handled request, with the operational metadata the response envelope needs."""

    request_id: str
    query: str
    query_variations: list[str]
    response: ContextDeeplinkResponse
    cache_hit: bool
    pipeline_invoked: bool
    similarity: float | None
    latency_ms: float
    embed_ms: float
    lookup_ms: float
    pipeline_ms: float | None
    cost_usd: float
    model: str | None
    cache_version: int
    fallback: str | None = None
    validation_errors: list[str] = field(default_factory=list)


class TroubleshootingService:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        encoder: Encoder,
        pipeline: PipelinePort,
        permitted_deeplinks: frozenset[str],
        catalog_source: str,
        settings: Settings | None = None,
    ) -> None:
        self._pool = pool
        self._encoder = encoder
        # The only binding to Phase 0-2. Swapping the mock for the real implementation
        # happens where this service is constructed, not inside it.
        self._pipeline = pipeline
        self._permitted = permitted_deeplinks
        self._catalog_source = catalog_source
        self._settings = settings or get_settings()

    async def _embed_one(self, text: str) -> np.ndarray:
        return await anyio.to_thread.run_sync(self._encoder.encode_one_for_cache, text)

    async def _embed_many(self, texts: list[str]) -> np.ndarray:
        return await anyio.to_thread.run_sync(self._encoder.encode_for_cache, texts)

    async def troubleshoot(
        self,
        query: str,
        siis_response: str | None = None,
        *,
        force_pipeline: bool = False,
        origin: str = "runtime",
    ) -> TroubleshootOutcome:
        """Handle one request.

        force_pipeline skips the cache lookup and always runs Phase 0-2. It exists for
        pre-warming: the canonical query set defines the plan inventory, so each canonical
        query must produce its own plan. Consulting the cache while pre-warming lets one
        canonical query absorb its neighbours, leaving fewer distinct plans than there are
        canonical queries and making later lookups resolve to a near-miss plan rather than
        the specific one.

        origin records how a cached plan came to exist, so pre-warmed coverage can be
        distinguished from plans accumulated by live traffic.
        """
        request_id = str(uuid.uuid4())
        started = time.perf_counter()

        embed_started = time.perf_counter()
        query_vector = await self._embed_one(query)
        embed_ms = (time.perf_counter() - embed_started) * 1000

        lookup_ms = 0.0
        async with self._pool.connection() as conn:
            hit = None
            if not force_pipeline:
                lookup_started = time.perf_counter()
                hit = await store.lookup(conn, query_vector, self._settings)
                lookup_ms = (time.perf_counter() - lookup_started) * 1000

            if hit is not None:
                # A cached plan is not trusted on the strength of having been cached.
                report = validate_plan(hit.plan, self._permitted)
                if report.ok:
                    await store.record_hit(conn, hit.plan_id)
                    latency_ms = (time.perf_counter() - started) * 1000
                    outcome = TroubleshootOutcome(
                        request_id=request_id,
                        query=query,
                        query_variations=hit.query_variations,
                        response=hit.plan,
                        cache_hit=True,
                        pipeline_invoked=False,
                        similarity=hit.similarity,
                        latency_ms=latency_ms,
                        embed_ms=embed_ms,
                        lookup_ms=lookup_ms,
                        pipeline_ms=None,
                        cost_usd=0.0,
                        model=None,
                        cache_version=hit.cache_version,
                    )
                    await self._record(conn, outcome, matched_plan_id=hit.plan_id,
                                       validation_passed=True)
                    return outcome

                logger.warning(
                    "cached plan %d failed re-validation, treating as a miss: %s",
                    hit.plan_id, report.summary(),
                )

        # --- miss path -------------------------------------------------------
        pipeline_started = time.perf_counter()
        result = await self._pipeline.run(
            PipelineRequest(query=query, siis_response=siis_response)
        )
        pipeline_ms = (time.perf_counter() - pipeline_started) * 1000

        report = validate_plan(result.response, self._permitted)
        validation_passed = report.ok
        response = result.response
        fallback = result.fallback

        if not validation_passed:
            # An invalid plan is never served and never cached. Falling back to an empty
            # contexts list is the specified behaviour for "no viable solution".
            logger.error(
                "pipeline produced an invalid plan, serving fallback: %s", report.summary()
            )
            response = ContextDeeplinkResponse(contexts=[])
            fallback = fallback or "no_match"

        async with self._pool.connection() as conn:
            if validation_passed and response.contexts:
                await self._cache(conn, query, result, origin=origin)

            latency_ms = (time.perf_counter() - started) * 1000
            outcome = TroubleshootOutcome(
                request_id=request_id,
                query=query,
                query_variations=result.query_variations,
                response=response,
                cache_hit=False,
                pipeline_invoked=True,
                similarity=None,
                latency_ms=latency_ms,
                embed_ms=embed_ms,
                lookup_ms=lookup_ms,
                pipeline_ms=pipeline_ms,
                cost_usd=result.meta.cost_usd,
                model=result.meta.model,
                cache_version=self._settings.cache_version,
                fallback=fallback,
                validation_errors=[str(v) for v in report.violations],
            )
            await self._record(conn, outcome, matched_plan_id=None,
                               validation_passed=validation_passed)

        return outcome

    async def _cache(self, conn, query: str, result, *, origin: str = "runtime") -> None:
        """Store the validated plan under every phrasing that should reach it."""
        phrases = self._searchable_phrases(query, result)
        vectors = await self._embed_many(phrases)
        score = result.response.contexts[0].score if result.response.contexts else None

        await store.store_plan(
            conn,
            canonical_query=result.canonical_query,
            original_query=query,
            plan=result.response,
            query_variations=result.query_variations,
            vectors=dict(zip(phrases, vectors)),
            score=score,
            catalog_source=self._catalog_source,
            pipeline_model=result.meta.model,
            pipeline_cost_usd=result.meta.cost_usd,
            origin=origin,
            settings=self._settings,
        )

    @staticmethod
    def _searchable_phrases(query: str, result) -> list[str]:
        """Canonical form, the original wording, and every paraphrase, de-duplicated.

        Order is preserved so the canonical and original forms are classified correctly
        when stored.
        """
        phrases: list[str] = []
        for text in [result.canonical_query, query, *result.query_variations]:
            cleaned = text.strip()
            if cleaned and cleaned not in phrases:
                phrases.append(cleaned)
        return phrases

    async def _record(self, conn, outcome: TroubleshootOutcome, *,
                      matched_plan_id: int | None, validation_passed: bool | None) -> None:
        try:
            await store.record_metrics(
                conn,
                request_id=outcome.request_id,
                query=outcome.query,
                cache_hit=outcome.cache_hit,
                similarity=outcome.similarity,
                matched_plan_id=matched_plan_id,
                pipeline_invoked=outcome.pipeline_invoked,
                validation_passed=validation_passed,
                fallback=outcome.fallback,
                latency_ms=outcome.latency_ms,
                embed_ms=outcome.embed_ms,
                lookup_ms=outcome.lookup_ms,
                pipeline_ms=outcome.pipeline_ms,
                cost_usd=outcome.cost_usd,
                cache_version=outcome.cache_version,
            )
        except Exception:
            # Metrics are observability, not correctness. A failure to record must never
            # fail the request that was otherwise served successfully.
            logger.exception("failed to record request metrics")
