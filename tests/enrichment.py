import json

import pytest

from app.pipeline.enrichment.enricher import QueryEnricher


@pytest.mark.asyncio
async def test_query_enrichment(monkeypatch):
    enricher = QueryEnricher(
        api_key="test-key",
        model="test-model",
    )

    fake_response = {
        "canonical_query": "Phone battery draining quickly",
        "topic": "Battery",
        "query_variations": [
            "My phone battery drains very quickly",
            "Why is my phone losing charge so fast?",
            "phone battery draining fast",
            "Battery life is suddenly very poor",
            "My phone keeps running out of battery",
            "Battery doesn't last through the day",
            "battry draining too fast",
            "Phone charge drops really quickly",
            "Help with fast battery drain",
            "My battery is dying way too fast",
        ],
    }

    monkeypatch.setattr(
        enricher,
        "_call_gemini",
        lambda system_prompt, user_prompt: json.dumps(fake_response),
    )

    result = await enricher.enrich(
        "bro my battry is dying sooo fast 😭"
    )

    assert result.canonical_query == "Phone battery draining quickly"

    assert result.topic == "Battery"

    assert len(result.query_variations) == 10

    assert len(
        {
            variation.casefold()
            for variation in result.query_variations
        }
    ) == 10


@pytest.mark.asyncio
async def test_empty_query_rejected():
    enricher = QueryEnricher(
        api_key="test-key",
        model="test-model",
    )

    with pytest.raises(ValueError):
        await enricher.enrich("   ")
