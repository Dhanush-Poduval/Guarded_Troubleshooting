"""The REST API service.

Run it with:

    uvicorn app.api.main:app --loop asyncio

The --loop flag matters on Windows. uvicorn installs a Proactor event loop by default
there, and psycopg's async mode cannot run on it; the symptom is every database call
failing as a pool timeout, which reads like an outage rather than a loop mismatch.

Handlers stay thin. They translate HTTP to a service call and back, and contain no
retrieval, caching or validation logic of their own.
"""

from __future__ import annotations

import functools
import logging
import pathlib
from contextlib import asynccontextmanager

import anyio
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import readiness
from app.api.schemas import (
    CacheStatsResponse,
    ExampleQuery,
    ExamplesResponse,
    HealthResponse,
    ResponseMeta,
    TroubleshootRequest,
    TroubleshootResponse,
)
from app.api.resolution_routes import router as resolution_router
from app.bootstrap import build_pool, build_service
from app.cache.store import cache_stats
from app.catalog.queries import load_queries
from app.config import Settings, get_settings
from app.db.pool import pool_stats
from app.resolution.service import ResolutionService
from app.service import TroubleshootingService

logger = logging.getLogger(__name__)

# The AnyIO worker threadpool defaults to 40 tokens per event loop. Every request runs a
# CPU-bound encode on that pool, so the default is the ceiling on concurrent requests.
# Raised here rather than at import time, because the limiter belongs to the running loop.
THREADPOOL_TOKENS = 100


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    try:
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = THREADPOOL_TOKENS
    except Exception:  # noqa: BLE001 - a smaller pool still works, just with less headroom
        logger.warning("could not resize the worker thread limiter", exc_info=True)

    pool = build_pool(settings)
    await pool.open()
    app.state.pool = pool
    app.state.settings = settings
    app.state.encoder_loaded = False
    app.state.service = None
    app.state.resolution = None

    try:
        # Loading the model takes seconds. Doing it at startup keeps it off the request
        # path, and keeps /health honest: the service is not ready until this finishes.
        app.state.service = await anyio.to_thread.run_sync(build_service, pool, settings)
        # The resolution loop validates plan snapshots against exactly the catalog set
        # the troubleshooting service validates against, so it reuses that set rather
        # than loading the catalog a second time.
        app.state.resolution = ResolutionService(
            pool=pool,
            permitted_deeplinks=app.state.service.permitted_deeplinks,
        )
        app.state.encoder_loaded = True
        logger.info(
            "service ready; resolution adapter=%s",
            app.state.resolution.adapter_name,
        )
    except Exception:  # noqa: BLE001 - stay up so /health can report why
        logger.exception("startup failed; /health will report not ready")

    try:
        yield
    finally:
        await pool.close()
        logger.info("connection pool closed")


app = FastAPI(
    title="Smart Guided Troubleshooting Engine",
    version="1.0.0",
    summary="Turns unstructured Galaxy device complaints into validated, deeplinked plans.",
    lifespan=lifespan,
)


def get_service(request: Request) -> TroubleshootingService:
    service = getattr(request.app.state, "service", None)
    if service is None:
        # 503 rather than 500: the service is not broken, it is not ready.
        raise ServiceNotReady()
    return service


class ServiceNotReady(Exception):
    pass


@app.exception_handler(ServiceNotReady)
async def _not_ready(request: Request, exc: ServiceNotReady) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "service is still initializing; check GET /health"},
    )


async def _troubleshoot(
    payload: TroubleshootRequest,
    service: TroubleshootingService,
) -> TroubleshootResponse:
    outcome = await service.troubleshoot(payload.query, payload.siis_text())
    return TroubleshootResponse(
        query=outcome.query,
        query_variations=outcome.query_variations,
        response=outcome.response,
        meta=ResponseMeta(
            latency_ms=round(outcome.latency_ms, 2),
            cache_hit=outcome.cache_hit,
            model=outcome.model,
            cost_usd=outcome.cost_usd,
            pipeline_used=outcome.pipeline_invoked,
            request_id=outcome.request_id,
            cache_version=outcome.cache_version,
            similarity=outcome.similarity,
            embed_ms=round(outcome.embed_ms, 2),
            lookup_ms=round(outcome.lookup_ms, 2),
            pipeline_ms=(
                round(outcome.pipeline_ms, 2) if outcome.pipeline_ms is not None else None
            ),
            fallback=outcome.fallback,
            cache_reject_reason=outcome.cache_reject_reason,
        ),
    )


@app.post("/v1/troubleshoot", response_model=TroubleshootResponse)
async def troubleshoot(
    payload: TroubleshootRequest,
    service: TroubleshootingService = Depends(get_service),
) -> TroubleshootResponse:
    """Process a customer complaint and return an actionable plan."""
    return await _troubleshoot(payload, service)


@app.post("/troubleshoot", response_model=TroubleshootResponse, include_in_schema=False)
async def troubleshoot_alias(
    payload: TroubleshootRequest,
    service: TroubleshootingService = Depends(get_service),
) -> TroubleshootResponse:
    """Unversioned alias. The documented path is /v1/troubleshoot."""
    return await _troubleshoot(payload, service)


@app.get("/health", response_model=HealthResponse)
async def health(request: Request) -> JSONResponse:
    """200 only once the caching layer, the model and the vector indexes are ready."""
    pool = getattr(request.app.state, "pool", None)
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()

    if pool is None:
        body = HealthResponse(
            status="not_ready", database=False, embedding_model=False,
            vector_indexes=False, catalog_indexed=False, cache_ready=False,
            detail="connection pool not initialized",
        )
        return JSONResponse(status_code=503, content=body.model_dump())

    state = await readiness.probe(
        pool, settings, encoder_loaded=getattr(request.app.state, "encoder_loaded", False)
    )
    body = HealthResponse(
        status="ok" if state.ok else "not_ready",
        database=state.database,
        embedding_model=state.embedding_model,
        vector_indexes=state.vector_indexes,
        catalog_indexed=state.catalog_indexed,
        cache_ready=state.cache_ready,
        detail=state.detail,
    )
    return JSONResponse(status_code=200 if state.ok else 503, content=body.model_dump())


@app.get("/cache/stats", response_model=CacheStatsResponse)
async def cache_statistics(request: Request) -> CacheStatsResponse:
    """Measured cache behaviour: size, hit rate, latency percentiles and rejections."""
    pool = getattr(request.app.state, "pool", None)
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    if pool is None:
        raise ServiceNotReady()

    async with pool.connection() as conn:
        stats = await cache_stats(conn, settings)
    stats["pool"] = pool_stats(pool)
    return CacheStatsResponse(**stats)


@functools.lru_cache(maxsize=1)
def _examples() -> ExamplesResponse:
    """The official query set, read once.

    Reading it per request would be wasted work on a file that cannot change while the
    process runs, and a failure here must not take the API down: the demo UI degrades to
    a blank example list, which is cosmetic, while /v1/troubleshoot is unaffected.
    """
    try:
        records = load_queries(settings=get_settings())
    except Exception:  # noqa: BLE001 - an unreadable query set is not an API outage
        logger.exception("could not load the example query set")
        return ExamplesResponse(count=0, examples=[])

    examples = [
        ExampleQuery(
            id=record.id,
            query=record.query,
            siis_title=record.siis.title if record.siis else "",
            siis_response=record.siis.as_text() if record.siis else "",
        )
        for record in records
        if record.query
    ]
    return ExamplesResponse(count=len(examples), examples=examples)


@app.get("/v1/examples", response_model=ExamplesResponse)
def examples() -> ExamplesResponse:
    """The official complaints and their SIIS reference text, for the demo client."""
    return _examples()


# Registered before the static mount, since a mount at "/" claims every path no earlier
# route matched.
app.include_router(resolution_router)


# The web client is mounted last and on purpose. A Starlette mount at "/" matches any path
# that no earlier route claimed, so registering it before the handlers above would shadow
# them. The API keeps working when the directory is absent, which is why this is guarded
# rather than assumed.
WEB_DIR = pathlib.Path(__file__).resolve().parents[2] / "web"

if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
else:
    logger.warning("web client directory not found at %s; serving the API only", WEB_DIR)
