"""Checkpoint 5 verification: the REST API surface.

Covers test plan items 13, 14 and 15. The app is driven in-process over ASGI with its real
lifespan running, so the pool, the embedding model and the readiness probe all behave as
they do under uvicorn rather than being stubbed.
"""

from __future__ import annotations

import json
import uuid

import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")

import httpx

from app.contract.schema import ContextDeeplinkResponse


@pytest.fixture(autouse=True)
def keep_the_cache_clean(db_connection):
    """Remove plans these tests cause to be cached.

    Unlike the service tests, the API exercises the real application, which runs on the
    production cache_version. Without cleanup the first run leaves plans behind and the
    next run sees a hit where it expected a miss, so the suite would pass once and then
    fail. Pre-warmed plans are left alone; only runtime plans created here are removed.
    """
    before = {
        row[0]
        for row in db_connection.execute(
            "SELECT id FROM plan_cache WHERE origin = 'runtime'"
        ).fetchall()
    }
    yield
    with db_connection.transaction():
        db_connection.execute(
            "DELETE FROM plan_cache WHERE origin = 'runtime' AND NOT (id = ANY(%s))",
            (list(before),),
        )


@pytest.fixture
async def client(db_connection):
    """An ASGI client with the application lifespan active.

    db_connection is depended on only so the whole module skips when Postgres is down.
    """
    from app.api.main import app

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=60.0
        ) as http_client:
            yield http_client


# --------------------------------------------------------------------------
# /health
# --------------------------------------------------------------------------

async def test_health_reports_ready(client):
    """The contract ties a 200 to the caching layer, model and vector indexes all being
    initialized, so this is a readiness check rather than a liveness stub."""
    response = await client.get("/health")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] is True
    assert body["embedding_model"] is True
    assert body["vector_indexes"] is True
    assert body["catalog_indexed"] is True
    assert body["cache_ready"] is True


async def test_health_names_every_component(client):
    """A failure must say which component is not ready, not just that something is."""
    body = (await client.get("/health")).json()
    for key in ("database", "embedding_model", "vector_indexes", "catalog_indexed",
                "cache_ready"):
        assert key in body


# --------------------------------------------------------------------------
# /v1/troubleshoot
# --------------------------------------------------------------------------

async def test_troubleshoot_returns_the_documented_envelope(client):
    response = await client.post(
        "/v1/troubleshoot", json={"query": "My Galaxy S24 screen is completely black"}
    )
    assert response.status_code == 200, response.text

    body = response.json()
    # query and response appear exactly as the official sample shows them.
    assert set(body) == {"query", "query_variations", "response", "meta"}
    assert body["query"] == "My Galaxy S24 screen is completely black"
    assert "contexts" in body["response"]


async def test_response_conforms_to_the_data_contract(client):
    """The payload must parse back into the schema the dataset ships."""
    body = (
        await client.post("/v1/troubleshoot", json={"query": "My Galaxy screen cracked"})
    ).json()
    parsed = ContextDeeplinkResponse.model_validate(body["response"])
    assert isinstance(parsed.contexts, list)


async def test_meta_carries_the_operational_fields(client):
    body = (
        await client.post(
            "/v1/troubleshoot", json={"query": "My Galaxy A17 display looks distorted"}
        )
    ).json()
    meta = body["meta"]
    for key in ("latency_ms", "cache_hit", "model", "cost_usd"):
        assert key in meta
    assert isinstance(meta["cache_hit"], bool)
    assert meta["latency_ms"] > 0
    assert meta["pipeline_used"] is not meta["cache_hit"] or meta["fallback"] is not None


async def test_repeat_request_reports_a_cache_hit(client):
    """Asking the same thing twice must be answered from cache, with no pipeline run.

    Only the second call is asserted on. Whether the first was a miss depends on what is
    pre-warmed, and a unique suffix does not make it cold: appending a random token to a
    long query barely moves its embedding, so it still matches the pre-warmed plan. That
    is correct behaviour, not a bug. The genuine miss-to-hit transition is covered in
    tests/test_cache.py, where each test gets an isolated cache version.
    """
    query = f"My Galaxy Z Flip 7 inner screen shows no image, ref {uuid.uuid4()}"
    await client.post("/v1/troubleshoot", json={"query": query})
    second = (await client.post("/v1/troubleshoot", json={"query": query})).json()

    assert second["meta"]["cache_hit"] is True
    assert second["meta"]["pipeline_used"] is False
    assert second["meta"]["cost_usd"] == 0.0
    assert second["meta"]["similarity"] is not None


async def test_cache_hit_latency_is_within_the_target(client):
    """The stated fast-path budget is 300 ms at P95. Asserted against the budget rather
    than against another measurement, which would be comparing two noisy numbers."""
    query = f"My Galaxy S22 screen inputs are delayed and laggy, case {uuid.uuid4()}"
    await client.post("/v1/troubleshoot", json={"query": query})

    latencies = []
    for _ in range(5):
        body = (await client.post("/v1/troubleshoot", json={"query": query})).json()
        assert body["meta"]["cache_hit"] is True
        latencies.append(body["meta"]["latency_ms"])

    assert max(latencies) < 300, latencies


async def test_unversioned_alias_is_accepted(client):
    response = await client.post("/troubleshoot", json={"query": "screen is blank"})
    assert response.status_code == 200


async def test_siis_response_accepts_a_plain_string(client):
    """The written contract shows a raw string."""
    response = await client.post(
        "/v1/troubleshoot",
        json={"query": "screen flickers", "siis_response": "some reference text"},
    )
    assert response.status_code == 200


async def test_siis_response_accepts_the_dataset_object(client):
    """siis_responses.json carries {title, content} rather than a string."""
    response = await client.post(
        "/v1/troubleshoot",
        json={
            "query": "screen flickers",
            "siis_response": {"title": "Screen issue", "content": "Reference steps here"},
        },
    )
    assert response.status_code == 200


@pytest.mark.parametrize("payload", [{"query": ""}, {"query": "   "}, {}])
async def test_invalid_requests_are_rejected(client, payload):
    response = await client.post("/v1/troubleshoot", json=payload)
    assert response.status_code == 422


async def test_response_is_pure_json_without_markdown_wrapping(client):
    """Responses must never be fenced or prefixed with prose."""
    response = await client.post("/v1/troubleshoot", json={"query": "screen is blank"})
    raw = response.text.strip()
    assert raw.startswith("{")
    assert raw.endswith("}")
    assert "```" not in raw
    json.loads(raw)
    assert response.headers["content-type"].startswith("application/json")


async def test_served_deeplinks_are_always_in_the_catalog(client):
    """The API must never emit a URI the catalog does not contain."""
    from app.catalog.loader import load_catalog

    permitted = load_catalog().deeplink_set() | {"bixby://dummy_positive"}
    body = (
        await client.post(
            "/v1/troubleshoot",
            json={"query": "My Galaxy tablet screen stays blank during Smart Switch"},
        )
    ).json()

    for goal in body["response"]["contexts"]:
        for action in goal["actions"]:
            for group in action["stepGroups"]:
                link = group.get("actionableDeeplink")
                if link:
                    assert link["deeplink"] in permitted


async def test_no_web_urls_leak_into_a_response(client):
    """Zero URL leakage is an absolute rule, so it is asserted on the wire format."""
    raw = (
        await client.post("/v1/troubleshoot", json={"query": "My Galaxy screen is cracked"})
    ).text.lower()
    for marker in ("http://", "https://", "www."):
        assert marker not in raw


# --------------------------------------------------------------------------
# /cache/stats
# --------------------------------------------------------------------------

async def test_cache_stats_returns_measured_values(client):
    response = await client.get("/cache/stats")
    assert response.status_code == 200

    body = response.json()
    for key in ("cache_version", "similarity_threshold", "ambiguity_margin",
                "cached_plans", "cached_vectors", "requests_total",
                "requests_cache_hit"):
        assert key in body
    assert body["cached_plans"] >= 0
    assert body["cached_vectors"] >= body["cached_plans"]


async def test_cache_stats_exposes_rejection_reasons(client):
    """So the ambiguity margin can be tuned from data rather than guessed."""
    body = (await client.get("/cache/stats")).json()
    for key in ("rejected_below_threshold", "rejected_ambiguous",
                "rejected_failed_revalidation"):
        assert isinstance(body[key], int)


async def test_cache_stats_reports_pool_state(client):
    body = (await client.get("/cache/stats")).json()
    assert body["pool"] is not None
    assert body["pool"]["pool_max"] >= body["pool"]["pool_min"]


# --------------------------------------------------------------------------
# Contract surface
# --------------------------------------------------------------------------

async def test_documented_routes_exist(client):
    schema = (await client.get("/openapi.json")).json()
    assert "/v1/troubleshoot" in schema["paths"]
    assert "/health" in schema["paths"]
    assert "/cache/stats" in schema["paths"]


async def test_health_reports_503_when_the_model_is_not_loaded(client):
    """The contract makes 200 conditional on the model being initialized. A service that
    is listening but has not finished loading must say so, not report ok."""
    from app.api.main import app

    original = app.state.encoder_loaded
    app.state.encoder_loaded = False
    try:
        response = await client.get("/health")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["embedding_model"] is False
        assert "embedding model" in (body["detail"] or "")
    finally:
        app.state.encoder_loaded = original


async def test_troubleshoot_returns_503_before_the_service_is_built(client):
    """Requests that arrive during startup get 503, not a 500 from a missing dependency."""
    from app.api.main import app

    original = app.state.service
    app.state.service = None
    try:
        response = await client.post("/v1/troubleshoot", json={"query": "screen blank"})
        assert response.status_code == 503
        assert "initializing" in response.json()["detail"]
    finally:
        app.state.service = original


async def test_readiness_probe_survives_an_unreachable_database():
    """A dead database must produce a reported not-ready state rather than an exception
    escaping into a 500."""
    from app.api import readiness
    from app.config import get_settings
    from app.db.pool import create_pool

    settings = get_settings().model_copy(update={"postgres_port": 1})
    pool = create_pool(settings, min_size=0, max_size=1)
    state = await readiness.probe(pool, settings, encoder_loaded=True)

    assert state.ok is False
    assert state.database is False
    assert state.detail
