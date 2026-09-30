"""Checkpoint 4 verification: the semantic cache end to end.

Covers test plan items 7 to 10 and 16: cache miss, cache population, cache hit, paraphrase
semantic hit, and the Phase 0-2 adapter integration.

These tests exercise the real service against the real database, because the interesting
failures here are integration failures: a vector that does not round-trip, a plan that
validates on write but not on read, a threshold that admits the wrong neighbour. A mocked
cache would test none of that.

Each test isolates itself by bumping cache_version, so cached state from one test cannot
satisfy another and produce a false pass.
"""

from __future__ import annotations

import itertools

import pytest

from app.bootstrap import build_pool, build_service
from app.cache.store import cache_stats
from app.config import Settings
from app.contract.schema import ContextDeeplinkResponse
from app.validation.rules import validate_plan

pytest.importorskip("fastembed")

from tests.conftest import TEST_CACHE_VERSION_FLOOR

_version_counter = itertools.count(TEST_CACHE_VERSION_FLOOR)

# The real Phase 0-2 pipeline derives plans from SIIS reference text and returns the
# specified "no_siis_context" fallback with no contexts when none is supplied. That is
# correct behaviour, so any test that expects a plan to be produced and cached must pass
# reference text. These tests were originally written against the mock, which produced a
# plan regardless, and that assumption no longer holds.
SIIS_TEXT = (
    "Display and power troubleshooting for Galaxy devices. "
    "Step 1: Check the screen brightness and adaptive brightness settings, since a very "
    "low brightness level can make the display look blank. "
    "Step 2: Open Device care and review battery usage by app to find software draining "
    "power in the background. "
    "Step 3: Restrict background usage for apps you rarely use, then restart the device "
    "so the changes take effect. "
    "Step 4: If the screen still does not respond, perform a forced restart by holding "
    "the power and volume down buttons together. "
    "Step 5: If the problem continues after a restart, back up your data and contact "
    "Samsung support or visit an authorised service centre for a hardware inspection."
)


async def solve(service, query, **kwargs):
    """Run a query with reference text, so the pipeline can produce a plan.

    Tests that specifically exercise the no-reference-text path call troubleshoot
    directly instead of going through this helper.
    """
    kwargs.setdefault("siis_response", SIIS_TEXT)
    return await service.troubleshoot(query, **kwargs)


@pytest.fixture
def isolated_settings(settings, clean_test_cache_versions) -> Settings:
    """A settings object on its own cache_version, so each test starts with an empty
    cache without truncating tables another test may be using.

    Depends on clean_test_cache_versions so the counter always starts against a wiped
    range; it restarts at the floor on every run and would otherwise collide with rows
    left by the previous one."""
    return settings.model_copy(update={"cache_version": next(_version_counter)})


@pytest.fixture
async def service_and_pool(isolated_settings, db_connection):
    """db_connection is depended on only to skip the whole module when Postgres is down."""
    pool = build_pool(isolated_settings)
    await pool.open()
    try:
        service = build_service(pool, isolated_settings)
        yield service, pool
    finally:
        await pool.close()


# --------------------------------------------------------------------------
# Miss, populate, hit
# --------------------------------------------------------------------------

async def test_cold_query_is_a_miss_and_invokes_the_pipeline(service_and_pool):
    service, _ = service_and_pool
    outcome = await solve(service, "My battery is draining very quickly")

    assert outcome.cache_hit is False
    assert outcome.pipeline_invoked is True
    assert outcome.similarity is None
    assert outcome.response.contexts, "expected the mock to produce a plan"


async def test_miss_populates_the_cache_with_every_phrasing(service_and_pool):
    service, pool = service_and_pool
    await solve(service, "Screen is too dim outdoors")

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM plan_cache WHERE cache_version = %s",
            (service._settings.cache_version,),
        )
        plans = (await cur.fetchone())[0]
        cur = await conn.execute(
            """
            SELECT count(*) FROM plan_cache_vector v
            JOIN plan_cache p ON p.id = v.plan_id
            WHERE p.cache_version = %s
            """,
            (service._settings.cache_version,),
        )
        vectors = (await cur.fetchone())[0]

    assert plans == 1
    # Canonical form, original wording and the paraphrases, not a single key.
    assert vectors > 1


async def test_identical_repeat_query_hits_the_cache(service_and_pool):
    service, _ = service_and_pool
    query = "Running out of storage space"
    await solve(service, query)
    second = await solve(service, query)

    assert second.cache_hit is True
    assert second.pipeline_invoked is False
    assert second.cost_usd == 0.0


async def test_unseen_paraphrase_hits_the_cache(service_and_pool):
    """The behaviour the whole design exists for: a rewording the system has never seen
    must reach the cached plan without invoking the pipeline."""
    service, _ = service_and_pool
    await solve(service, "My battery is draining very quickly")
    outcome = await solve(service, "My phone battery dies really fast")

    assert outcome.cache_hit is True
    assert outcome.pipeline_invoked is False
    assert outcome.similarity >= service._settings.cache_similarity_threshold


async def test_unrelated_query_does_not_hit_a_cached_plan(service_and_pool):
    """A false positive here would serve battery advice to a camera question."""
    service, _ = service_and_pool
    await solve(service, "My battery is draining very quickly")
    outcome = await solve(service, "How do I change the camera resolution")

    assert outcome.cache_hit is False
    assert outcome.pipeline_invoked is True


async def test_cache_hit_is_faster_than_the_miss_that_created_it(service_and_pool):
    service, _ = service_and_pool
    miss = await solve(service, "Colours look washed out on my screen")
    hit = await solve(service, "Colours look washed out on my screen")

    assert hit.cache_hit and not miss.cache_hit
    assert hit.latency_ms < miss.latency_ms


# --------------------------------------------------------------------------
# Threshold behaviour
# --------------------------------------------------------------------------

async def test_raising_the_threshold_turns_a_hit_into_a_miss(
    isolated_settings, db_connection
):
    """Guards against the threshold being ignored: at 0.99 almost nothing should match."""
    strict = isolated_settings.model_copy(update={"cache_similarity_threshold": 0.99})
    pool = build_pool(strict)
    await pool.open()
    try:
        service = build_service(pool, strict)
        await solve(service, "My battery is draining very quickly")
        outcome = await solve(service, "My phone battery dies really fast")
        assert outcome.cache_hit is False
    finally:
        await pool.close()


async def test_cache_versions_are_isolated(service_and_pool, isolated_settings):
    """A version bump must invalidate everything at once rather than leaking old plans."""
    service, _ = service_and_pool
    query = "Apps keep closing on their own"
    await solve(service, query)

    bumped = isolated_settings.model_copy(
        update={"cache_version": isolated_settings.cache_version + 500}
    )
    pool = build_pool(bumped)
    await pool.open()
    try:
        other = build_service(pool, bumped)
        outcome = await solve(other, query)
        assert outcome.cache_hit is False
    finally:
        await pool.close()


# --------------------------------------------------------------------------
# Validation is never bypassed
# --------------------------------------------------------------------------

async def test_served_plan_always_validates(service_and_pool):
    """Whether served from the pipeline or from the cache, output must be rule-compliant."""
    service, _ = service_and_pool
    for query in ("My battery is draining very quickly", "My phone battery dies really fast"):
        outcome = await solve(service, query)
        assert validate_plan(outcome.response, service._permitted).ok


async def test_every_served_deeplink_exists_in_the_catalog(service_and_pool):
    service, _ = service_and_pool
    outcome = await solve(service, "Swipe gestures go the wrong way after an app")
    for goal in outcome.response.contexts:
        for action in goal.actions:
            for group in action.stepGroups:
                if group.actionableDeeplink is not None:
                    assert group.actionableDeeplink.deeplink in service._permitted


async def test_invalid_pipeline_output_is_neither_served_nor_cached(service_and_pool):
    """If the pipeline returns something non-compliant, the specified fallback is an
    empty contexts list, and nothing may be written to the cache."""
    service, pool = service_and_pool

    from app.contract.schema import Action, Goal, StepGroup, actionCategory
    from app.pipeline.port import PipelineMeta, PipelineResult

    class BrokenPipeline:
        name = "broken"

        async def run(self, request):
            bad = Goal(
                goal="just fix it",                      # wrong sentence
                title="Way Too Many Words In This Title",
                actions=[
                    Action(
                        actionName="lowercase name",
                        description="nope",
                        stepGroups=[StepGroup(steps=["Visit https://samsung.com"])],
                        category=actionCategory.auto,
                    )
                ],
                score=5.0,                               # out of range
            )
            return PipelineResult(
                canonical_query="broken query",
                query_variations=["a", "b"],
                response=ContextDeeplinkResponse(contexts=[bad]),
                meta=PipelineMeta(model="broken"),
            )

    service._pipeline = BrokenPipeline()
    outcome = await solve(service, "trigger the broken pipeline")

    assert outcome.response.contexts == []
    assert outcome.fallback == "no_match"
    assert outcome.validation_errors

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM plan_cache WHERE cache_version = %s",
            (service._settings.cache_version,),
        )
        assert (await cur.fetchone())[0] == 0


# --------------------------------------------------------------------------
# Adapter and metrics
# --------------------------------------------------------------------------

async def test_pipeline_satisfies_the_port_protocol(service_and_pool):
    """The mock must remain swappable for the real Phase 0-2 implementation."""
    from app.pipeline.port import PipelinePort

    service, _ = service_and_pool
    assert isinstance(service._pipeline, PipelinePort)


async def test_pipeline_returns_eight_to_ten_variations(service_and_pool):
    from app.pipeline.port import PipelineRequest
    from app.validation.rules import validate_query_variations

    service, _ = service_and_pool
    result = await service._pipeline.run(PipelineRequest(query="My battery drains fast"))
    assert validate_query_variations(result.query_variations).ok


async def test_metrics_are_recorded_for_hits_and_misses(service_and_pool):
    service, pool = service_and_pool
    query = "Device feels laggy when switching apps"
    miss = await solve(service, query)
    hit = await solve(service, query)

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT cache_hit, pipeline_invoked, latency_ms FROM request_metrics "
            "WHERE request_id = ANY(%s) ORDER BY created_at",
            ([miss.request_id, hit.request_id],),
        )
        rows = await cur.fetchall()

    assert len(rows) == 2
    assert [row[0] for row in rows] == [False, True]
    assert [row[1] for row in rows] == [True, False]
    assert all(row[2] > 0 for row in rows)


async def test_cache_stats_reports_measured_values(service_and_pool):
    service, pool = service_and_pool
    await solve(service, "Phone takes forever to charge")

    async with pool.connection() as conn:
        stats = await cache_stats(conn, service._settings)

    assert stats["cached_plans"] >= 1
    assert stats["cached_vectors"] >= stats["cached_plans"]
    assert stats["similarity_threshold"] == service._settings.cache_similarity_threshold
    assert stats["cache_version"] == service._settings.cache_version


async def test_synthetic_provenance_is_recorded(service_and_pool):
    """Plans built on fixture deeplinks must be identifiable so they can be purged."""
    service, pool = service_and_pool
    # The query has to be answerable from SIIS_TEXT. The pipeline is required not to
    # invent steps the reference text does not contain, so an unrelated complaint yields
    # no actions, nothing is cached, and this test would fail for a reason that has
    # nothing to do with provenance.
    await solve(service, "My Galaxy screen goes completely blank and will not come back")

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT catalog_source FROM plan_cache WHERE cache_version = %s",
            (service._settings.cache_version,),
        )
        sources = {row[0] for row in await cur.fetchall()}

    assert sources == {service._catalog_source}


async def test_force_pipeline_skips_the_cache(service_and_pool):
    """Pre-warming must give each canonical query its own plan. Without this, a canonical
    query can be absorbed by a neighbour already cached, leaving fewer distinct plans than
    there are canonical queries."""
    service, _ = service_and_pool
    query = "Screen turns off too fast"
    await solve(service, query)

    forced = await solve(service, query, force_pipeline=True)
    assert forced.cache_hit is False
    assert forced.pipeline_invoked is True


async def test_prewarm_origin_is_recorded(service_and_pool, ):
    """Pre-warmed coverage must be distinguishable from plans accumulated by live traffic."""
    service, pool = service_and_pool
    await solve(service, 
        "Battery percentage drops suddenly overnight", force_pipeline=True, origin="prewarm"
    )

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT origin FROM plan_cache WHERE cache_version = %s",
            (service._settings.cache_version,),
        )
        assert {row[0] for row in await cur.fetchall()} == {"prewarm"}


async def test_a_paraphrase_can_match_a_stored_paraphrase(service_and_pool):
    """The reason every phrasing is indexed rather than one canonical key: a reworded
    query may be closer to a stored paraphrase than to the canonical form."""
    service, pool = service_and_pool
    # Both phrasings describe the same problem, and it is a problem SIIS_TEXT actually
    # covers. A pair drawn from a different topic would fail for want of a matching plan
    # rather than for want of paraphrase indexing, which is what this test is about.
    await solve(service, "My Galaxy screen keeps going blank while I am using it")

    outcome = await solve(service, "The display on my phone keeps going dark by itself")
    assert outcome.cache_hit is True

    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT count(*) FROM plan_cache_vector v
            JOIN plan_cache p ON p.id = v.plan_id
            WHERE p.cache_version = %s AND v.variation_kind = 'paraphrase'
            """,
            (service._settings.cache_version,),
        )
        assert (await cur.fetchone())[0] > 0


# --------------------------------------------------------------------------
# Ambiguity margin
# --------------------------------------------------------------------------

async def test_near_tie_between_two_plans_is_rejected_as_ambiguous(service_and_pool):
    """A similarity threshold alone cannot separate a paraphrase of one problem from a
    different problem phrased similarly. Measured on the official query set, 34 of 190
    pairs of genuinely distinct problems exceed the configured threshold, so when two
    plans are near-equally close the answer would be an arbitrary pick between them.

    Rejecting costs one pipeline run. Serving the wrong plan is a correctness failure.
    """
    from app.cache.store import lookup_detailed

    service, pool = service_and_pool
    await solve(service, 
        "My Galaxy Z Flip 7 screen went completely black, so I cannot interact with it",
        force_pipeline=True,
    )
    await solve(service, 
        "My Galaxy S24 screen goes completely blank, just a dark screen with scrolling",
        force_pipeline=True,
    )

    probe = await service._embed_one("my Galaxy screen is completely black and blank")

    wide = service._settings.model_copy(update={"cache_ambiguity_margin": 0.5})
    async with pool.connection() as conn:
        hit, miss = await lookup_detailed(conn, probe, wide)

    assert hit is None
    assert miss.reason == "ambiguous"
    assert miss.runner_up_similarity is not None
    assert miss.best_similarity - miss.runner_up_similarity < 0.5


async def test_a_clear_winner_is_not_rejected(service_and_pool):
    """The margin must not suppress legitimate hits. Measured on the official query set,
    the smallest margin for an exact query is 0.1258, comfortably above the default."""
    from app.cache.store import lookup_detailed

    service, pool = service_and_pool
    query = "My Galaxy S22 screen inputs are delayed and touch responsiveness is laggy"
    await solve(service, query, force_pipeline=True)
    await solve(service, 
        "My Galaxy phone screen is completely cracked and unusable", force_pipeline=True
    )

    probe = await service._embed_one(query)
    async with pool.connection() as conn:
        hit, miss = await lookup_detailed(conn, probe, service._settings)

    assert hit is not None, miss
    assert hit.margin is None or hit.margin >= service._settings.cache_ambiguity_margin


async def test_single_cached_plan_has_no_runner_up(service_and_pool):
    """With one plan cached there is nothing to be ambiguous against, so the margin check
    must not reject on a missing runner-up."""
    from app.cache.store import lookup_detailed

    service, pool = service_and_pool
    query = "My Galaxy A17 screen looks distorted right after I received the phone"
    await solve(service, query, force_pipeline=True)

    probe = await service._embed_one(query)
    async with pool.connection() as conn:
        hit, _ = await lookup_detailed(conn, probe, service._settings)

    assert hit is not None
    assert hit.runner_up_similarity is None
    assert hit.margin is None


async def test_rejection_reason_is_recorded_for_analysis(service_and_pool):
    """The margin should be tunable from data, so what the check saw is persisted."""
    service, pool = service_and_pool
    await solve(service, "My Galaxy S25 has a floating circle hovering on screen")

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM request_metrics WHERE cache_version = %s",
            (service._settings.cache_version,),
        )
        assert (await cur.fetchone())[0] >= 1

        cur = await conn.execute(
            """
            SELECT cache_reject_reason FROM request_metrics
            WHERE cache_version = %s AND cache_reject_reason IS NOT NULL
            """,
            (service._settings.cache_version,),
        )
        for (reason,) in await cur.fetchall():
            assert reason in {"below_threshold", "ambiguous", "failed_revalidation"}
