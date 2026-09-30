"""Tests for Phase 1 grounded structure extraction."""


import pytest

from app.pipeline.enrichment.schema import EnrichedQuery
from app.pipeline.extraction.extractor import (
    StructureExtractionError,
    StructureExtractor,
)


@pytest.mark.asyncio
async def test_grounded_structure_extraction(
    monkeypatch,
):
    extractor = StructureExtractor(
        api_key="test-key",
        model="test-model",
    )

    enriched = EnrichedQuery(
        canonical_query="Phone battery draining quickly",
        topic="Battery",
        query_variations=[
            "Battery drains quickly",
            "Phone losing battery fast",
            "Why is my battery draining?",
            "battery drain issue",
            "Battery life is very poor",
            "My phone battery dies quickly",
            "battry draining fast",
            "Phone charge dropping quickly",
            "Help with battery drain",
            "Battery doesn't last long",
        ],
    )

    reference = (
        "Open Settings and tap Battery. "
        "Review which applications are using the most battery. "
        "If the problem continues, restart the device."
    )

    fake_response = {
        "topic": "Battery",
        "title": "Fast battery drain",
        "actions": [
            {
                "action_name": "Battery Usage",
                "target_screen": "Battery usage settings",
                "steps": [
                    "Open Settings.",
                    "Tap Battery.",
                    "Review applications using the most battery.",
                ],
                "category": "auto",
                "source_text": (
                    "Open Settings and tap Battery. "
                    "Review which applications are using "
                    "the most battery."
                ),
            },
            {
                "action_name": "Restart Device",
                "target_screen": None,
                "steps": [
                    "Restart the device.",
                ],
                "category": "critical",
                "source_text": (
                    "If the problem continues, "
                    "restart the device."
                ),
            },
        ],
        "confidence": 0.95,
    }

    monkeypatch.setattr(
        extractor,
        "_call_gemini",
        # _call_gemini now returns the parsed payload, so that a reply which arrives
        # intact but does not parse can fall back to the secondary model.
        lambda system_prompt, user_prompt: fake_response,
    )

    result = await extractor.extract(
        enriched=enriched,
        siis_reference=reference,
    )

    assert result.topic == "Battery"
    assert result.title == "Fast battery drain"
    assert result.confidence == 0.95

    assert len(result.actions) == 2

    first_action = result.actions[0]

    assert first_action.action_name == "Battery Usage"
    assert (
        first_action.target_screen
        == "Battery usage settings"
    )
    assert first_action.category.value == "auto"

    assert first_action.steps == [
        "Open Settings.",
        "Tap Battery.",
        "Review applications using the most battery.",
    ]

    last_action = result.actions[-1]

    assert last_action.action_name == "Restart Device"
    assert last_action.category.value == "critical"

    normalized_reference = (
        extractor._normalise_for_grounding(
            reference
        )
    )

    for action in result.actions:
        normalized_evidence = (
            extractor._normalise_for_grounding(
                action.source_text
            )
        )

        assert (
            normalized_evidence
            in normalized_reference
        )


@pytest.mark.asyncio
async def test_fake_source_text_is_rejected(
    monkeypatch,
):
    extractor = StructureExtractor(
        api_key="test-key",
        model="test-model",
    )

    enriched = EnrichedQuery(
        canonical_query="Phone battery draining quickly",
        topic="Battery",
        query_variations=[
            "Battery drains quickly",
            "Phone losing battery fast",
            "Why is my battery draining?",
            "battery drain issue",
            "Battery life is very poor",
            "My phone battery dies quickly",
            "battry draining fast",
            "Phone charge dropping quickly",
            "Help with battery drain",
            "Battery doesn't last long",
        ],
    )

    reference = (
        "Open Settings and tap Battery. "
        "Review which applications are using "
        "the most battery."
    )

    fake_response = {
        "topic": "Battery",
        "title": "Fast battery drain",
        "actions": [
            {
                "action_name": "Factory Reset",
                "target_screen": "Factory data reset",
                "steps": [
                    "Perform a factory reset.",
                ],
                "category": "critical",
                "source_text": (
                    "Perform a factory reset "
                    "to resolve the issue."
                ),
            },
        ],
        "confidence": 0.80,
    }

    monkeypatch.setattr(
        extractor,
        "_call_gemini",
        # _call_gemini now returns the parsed payload, so that a reply which arrives
        # intact but does not parse can fall back to the secondary model.
        lambda system_prompt, user_prompt: fake_response,
    )

    with pytest.raises(
        StructureExtractionError,
        match="grounding",
    ):
        await extractor.extract(
            enriched=enriched,
            siis_reference=reference,
        )
