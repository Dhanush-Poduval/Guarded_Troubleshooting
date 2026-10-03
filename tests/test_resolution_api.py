"""The resolution loop over HTTP.

Driven in-process over ASGI with the real lifespan running, so the pool, the validation
set and the session store behave as they do under uvicorn. No device and no model call is
involved: plans are built from real catalog entries and the trusted path is exercised by
swapping in the fake adapter after startup.
"""

from __future__ import annotations

import uuid

import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")

import httpx

from app.resolution.service import ResolutionService
from app.verification.fake import FakeTrustedAdapter
from tests.test_resolution import build_plan, catalog_entries  # noqa: F401

pytestmark = pytest.mark.usefixtures("catalog_entries")


@pytest.fixture
async def client(db_connection):
    """An ASGI client with the application lifespan active.

    db_connection is depended on only so the module skips when PostgreSQL is down.
    """
    from app.api.main import app

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=60.0
        ) as http_client:
            yield http_client


@pytest.fixture
def app_state():
    from app.api.main import app

    return app.state


@pytest.fixture
def trusted(client, app_state):
    """Swap the shipped unavailable adapter for a seeded fake, for this test only."""
    original = app_state.resolution
    adapter = FakeTrustedAdapter()
    app_state.resolution = ResolutionService(
        pool=original._pool,
        permitted_deeplinks=original._permitted,
        adapter=adapter,
    )
    try:
        yield adapter
    finally:
        app_state.resolution = original


@pytest.fixture
def plan(catalog_entries):  # noqa: F811
    return build_plan(catalog_entries[:2])


async def start(client, plan, query="My Galaxy S24 screen is completely black"):
    response = await client.post(
        "/v1/resolution/sessions",
        json={"query": query, "plan": plan.model_dump(mode="json")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def seed(adapter, plan, index, value):
    from app.resolution.models import walk

    contract = walk(plan)[index].expected
    adapter.seed(contract.deeplink, contract.key, value)
    return contract


# ------------------------------------------------------------ session opening


async def test_starting_a_session_returns_the_first_action(client, plan):
    body = await start(client, plan)

    assert body["status"] == "pending"
    assert body["verification_status"] == "pending"
    assert body["current"]["step_number"] == 1
    assert body["current"]["step_total"] == 2
    assert body["current"]["actionable_deeplink"].startswith("bixby://")
    assert body["has_next"] is True


async def test_the_shipped_deployment_reports_automatic_verification_impossible(
    client, plan
):
    """The console needs to know, before it offers anything, that this deployment
    cannot read device state."""
    body = await start(client, plan)
    assert body["adapter"] == "unavailable"
    assert body["automatic_verification_possible"] is False


async def test_a_plan_with_an_unauthorised_uri_is_refused(client, plan):
    plan.contexts[0].actions[0].stepGroups[0].actionableDeeplink.deeplink = (
        "bixby://attacker/act/deadbeef"
    )
    response = await client.post(
        "/v1/resolution/sessions",
        json={"query": "My screen is black", "plan": plan.model_dump(mode="json")},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "plan_rejected"


async def test_a_blank_query_is_rejected(client, plan):
    response = await client.post(
        "/v1/resolution/sessions",
        json={"query": "   ", "plan": plan.model_dump(mode="json")},
    )
    assert response.status_code == 422


async def test_an_unknown_session_is_404(client):
    response = await client.get(f"/v1/resolution/sessions/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_a_session_can_be_read_back(client, plan):
    opened = await start(client, plan)
    response = await client.get(f"/v1/resolution/sessions/{opened['session_id']}")
    assert response.status_code == 200
    assert response.json()["session_id"] == opened["session_id"]


# ------------------------------------------------------- presenting an action


async def test_marking_presented_changes_no_verification_status(client, plan):
    opened = await start(client, plan)
    response = await client.post(
        f"/v1/resolution/sessions/{opened['session_id']}/presented"
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "in_progress"
    assert body["verification_status"] == "pending", (
        "opening a deeplink must never move the verification status"
    )
    assert body["attempts"] == []


# ------------------------------------------------------------- verification


async def test_verify_on_the_shipped_adapter_is_unavailable(client, plan):
    opened = await start(client, plan)
    response = await client.post(
        f"/v1/resolution/sessions/{opened['session_id']}/verify"
    )
    body = response.json()

    assert body["verification_status"] == "verification_unavailable"
    assert body["attempts"][-1]["reason_code"] == "adapter_unavailable"


async def test_verify_through_a_trusted_adapter_can_system_verify(
    client, plan, trusted
):
    seed(trusted, plan, 0, "True")
    opened = await start(client, plan)

    response = await client.post(
        f"/v1/resolution/sessions/{opened['session_id']}/verify"
    )
    body = response.json()

    assert body["verification_status"] == "system_verified"
    assert body["attempts"][-1]["evidence_source"] == "trusted_adapter"
    assert body["automatic_verification_possible"] is True


async def test_verify_through_a_trusted_adapter_can_fail(client, plan, trusted):
    seed(trusted, plan, 0, "False")
    opened = await start(client, plan)

    response = await client.post(
        f"/v1/resolution/sessions/{opened['session_id']}/verify"
    )
    assert response.json()["verification_status"] == "verification_failed"


# ------------------------------------------------------------ client evidence


async def test_a_client_cannot_declare_its_own_evidence_trusted(client, plan):
    """Even with the fields spelled out in the body, the server assigns provenance."""
    opened = await start(client, plan)
    response = await client.post(
        f"/v1/resolution/sessions/{opened['session_id']}/observations",
        json={
            "observed_value": "True",
            "evidence_source": "trusted_adapter",
            "verification_status": "system_verified",
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body["attempts"][-1]["evidence_source"] == "client_reported"
    assert body["verification_status"] == "inconclusive"
    assert body["verification_status"] != "system_verified"


async def test_an_observation_token_makes_submission_idempotent(client, plan):
    opened = await start(client, plan)
    sid = opened["session_id"]
    token = str(uuid.uuid4())
    payload = {"observed_value": "True", "observation_token": token}

    first = (await client.post(f"/v1/resolution/sessions/{sid}/observations",
                               json=payload)).json()
    second = (await client.post(f"/v1/resolution/sessions/{sid}/observations",
                                json=payload)).json()

    assert first["idempotent_replay"] is False
    assert second["idempotent_replay"] is True
    assert len(second["attempts"]) == 1


# ---------------------------------------------------------- user confirmation


async def test_user_confirmation_resolves_and_is_labelled(client, plan):
    opened = await start(client, plan)
    response = await client.post(
        f"/v1/resolution/sessions/{opened['session_id']}/confirm",
        json={"resolved": True},
    )
    body = response.json()

    assert body["status"] == "resolved"
    assert body["verification_status"] == "user_confirmed"
    assert body["attempts"][-1]["evidence_source"] == "user_confirmation"


# ----------------------------------------------------------------- advancing


async def test_advancing_requires_acknowledgement_for_a_critical_action(
    client, catalog_entries  # noqa: F811
):
    from app.contract.schema import actionCategory

    critical_plan = build_plan(
        catalog_entries[:2],
        categories=[actionCategory.auto, actionCategory.critical],
    )
    opened = await start(client, critical_plan)
    sid = opened["session_id"]
    assert opened["next_is_critical"] is True

    refused = await client.post(
        f"/v1/resolution/sessions/{sid}/advance", json={"acknowledge_critical": False}
    )
    assert refused.status_code == 409

    allowed = await client.post(
        f"/v1/resolution/sessions/{sid}/advance", json={"acknowledge_critical": True}
    )
    assert allowed.status_code == 200
    assert allowed.json()["current"]["is_critical"] is True


async def test_advancing_past_the_last_action_ends_the_session(client, catalog_entries):  # noqa: F811
    single = build_plan(catalog_entries[:1])
    opened = await start(client, single)

    response = await client.post(
        f"/v1/resolution/sessions/{opened['session_id']}/advance", json={}
    )
    assert response.json()["status"] == "unresolved"


async def test_a_finished_session_refuses_further_calls(client, plan):
    opened = await start(client, plan)
    sid = opened["session_id"]
    await client.post(f"/v1/resolution/sessions/{sid}/confirm", json={"resolved": True})

    for path, body in (
        ("presented", None),
        ("verify", None),
        ("observations", {"observed_value": "True"}),
        ("confirm", {"resolved": True}),
        ("advance", {}),
    ):
        response = await client.post(
            f"/v1/resolution/sessions/{sid}/{path}", json=body
        )
        assert response.status_code == 409, path


# ------------------------------------------------------------------- receipt


async def test_no_receipt_before_the_session_finishes(client, plan):
    opened = await start(client, plan)
    response = await client.get(
        f"/v1/resolution/sessions/{opened['session_id']}/receipt"
    )
    assert response.status_code == 409


async def test_a_user_confirmed_receipt_is_marked_as_not_system_verified(client, plan):
    opened = await start(client, plan)
    sid = opened["session_id"]
    await client.post(f"/v1/resolution/sessions/{sid}/confirm", json={"resolved": True})

    receipt = (await client.get(f"/v1/resolution/sessions/{sid}/receipt")).json()

    assert receipt["final_status"] == "resolved"
    assert receipt["verification_method"] == "user_confirmed"
    assert receipt["system_verified"] is False
    assert "did not read the device" in receipt["caveat"]
    assert receipt["completed_at"] is not None


async def test_a_system_verified_receipt_carries_the_observed_state(
    client, plan, trusted
):
    seed(trusted, plan, 0, "True")
    opened = await start(client, plan)
    sid = opened["session_id"]

    await client.post(f"/v1/resolution/sessions/{sid}/verify")
    await client.post(
        f"/v1/resolution/sessions/{sid}/complete", json={"status": "resolved"}
    )

    receipt = (await client.get(f"/v1/resolution/sessions/{sid}/receipt")).json()

    assert receipt["verification_method"] == "system_verified"
    assert receipt["system_verified"] is True
    assert receipt["observed_state"] == "True"
    assert receipt["expected_state"]
    assert receipt["caveat"] is None


async def test_the_two_receipt_kinds_are_distinguishable(client, plan, trusted):
    """A reader must never mistake the user's own account for a reading the server
    took, so the two receipts differ in more than wording."""
    seed(trusted, plan, 0, "True")

    verified_sid = (await start(client, plan))["session_id"]
    await client.post(f"/v1/resolution/sessions/{verified_sid}/verify")
    await client.post(
        f"/v1/resolution/sessions/{verified_sid}/complete", json={"status": "resolved"}
    )
    verified = (await client.get(
        f"/v1/resolution/sessions/{verified_sid}/receipt")).json()

    confirmed_sid = (await start(client, plan))["session_id"]
    await client.post(
        f"/v1/resolution/sessions/{confirmed_sid}/confirm", json={"resolved": True}
    )
    confirmed = (await client.get(
        f"/v1/resolution/sessions/{confirmed_sid}/receipt")).json()

    assert verified["system_verified"] is not confirmed["system_verified"]
    assert verified["verification_method"] != confirmed["verification_method"]
    assert verified["caveat"] is None and confirmed["caveat"]


# ------------------------------------------------------- backward compatibility


async def test_the_existing_endpoints_are_unchanged(client):
    """The resolution loop is additive. Nothing that worked before may have moved."""
    health = await client.get("/health")
    assert health.status_code in (200, 503)
    assert "status" in health.json()

    stats = await client.get("/cache/stats")
    assert stats.status_code == 200
    assert "cached_plans" in stats.json()

    examples = await client.get("/v1/examples")
    assert examples.status_code == 200
    assert examples.json()["count"] >= 1


async def test_troubleshoot_still_answers_without_reference_text(client):
    """The documented no-reference-text behaviour is untouched by the new feature."""
    response = await client.post(
        "/v1/troubleshoot", json={"query": "My Galaxy screen flickers badly"}
    )
    assert response.status_code == 200
    body = response.json()
    assert "response" in body and "meta" in body
    assert "contexts" in body["response"]


async def test_the_openapi_schema_still_documents_the_original_endpoints(client):
    spec = (await client.get("/openapi.json")).json()
    for path in ("/v1/troubleshoot", "/health", "/cache/stats", "/v1/examples"):
        assert path in spec["paths"], path
    assert "/v1/resolution/sessions" in spec["paths"]
