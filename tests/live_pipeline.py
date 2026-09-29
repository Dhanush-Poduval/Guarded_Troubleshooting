from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from app.pipeline.enrichment.enricher import QueryEnricher
from app.pipeline.extraction.extractor import StructureExtractor


# Explicitly load the project's .env file.
load_dotenv(".env")

pytestmark = pytest.mark.asyncio


def load_real_siis_case() -> tuple[str, str]:
    """
    Load one real query + SIIS reference from the official dataset.
    """

    path = Path("data/official/siis_responses.json")

    if not path.exists():
        pytest.skip(f"SIIS dataset not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    # --------------------------------------------------
    # Find the first record
    # --------------------------------------------------

    if isinstance(data, list):
        if not data:
            pytest.skip("SIIS dataset is empty")

        record = data[0]

    elif isinstance(data, dict):
        record = None

        # Support datasets wrapped inside a top-level key.
        for key in (
            "data",
            "responses",
            "queries",
            "items",
        ):
            value = data.get(key)

            if isinstance(value, list) and value:
                record = value[0]
                break

        if record is None:
            pytest.fail(
                "Could not determine SIIS JSON structure. "
                "Open data/official/siis_responses.json "
                "and adjust the loader."
            )

    else:
        pytest.fail(
            "Unexpected SIIS JSON structure"
        )

    if not isinstance(record, dict):
        pytest.fail(
            "Expected SIIS record to be a JSON object"
        )

    # --------------------------------------------------
    # Extract query
    # --------------------------------------------------

    query = (
        record.get("original_query")
        or record.get("query")
        or record.get("user_query")
        or record.get("input")
    )

    # --------------------------------------------------
    # Extract SIIS reference
    # --------------------------------------------------

    siis = (
        record.get("siis_response")
        or record.get("siis")
        or record.get("response")
        or record.get("reference")
    )

    if not query:
        pytest.fail(
            "Could not find query field. "
            f"Available fields: {list(record.keys())}"
        )

    if not siis:
        pytest.fail(
            "Could not find SIIS field. "
            f"Available fields: {list(record.keys())}"
        )

    # SIIS may itself be structured JSON.
    if not isinstance(siis, str):
        siis = json.dumps(
            siis,
            indent=2,
            ensure_ascii=False,
        )

    return str(query), siis


async def test_live_enrichment_and_extraction():
    """
    LIVE integration test.

    This test makes real Gemini API requests.

    Flow:

        Real query
            -> QueryEnricher
            -> Gemini
            -> EnrichedQuery
            -> StructureExtractor
            -> Gemini
            -> ExtractedPlan
            -> Grounding validation
            -> Critical-last validation
    """

    # --------------------------------------------------
    # Environment checks
    # --------------------------------------------------

    if not os.getenv("GEMINI_API_KEY"):
        pytest.skip(
            "GEMINI_API_KEY is not configured"
        )

    query, siis_reference = load_real_siis_case()

    enricher = QueryEnricher()
    extractor = StructureExtractor()

    # ==================================================
    # Phase 0: Query Enrichment
    # ==================================================

    enriched = await enricher.enrich(query)

    print()
    print("=" * 80)
    print("ORIGINAL QUERY")
    print("=" * 80)
    print(query)

    print()
    print("=" * 80)
    print("QUERY ENRICHMENT RESULT")
    print("=" * 80)

    print(
        f"Canonical query: "
        f"{enriched.canonical_query}"
    )

    print(
        f"Topic:           "
        f"{enriched.topic}"
    )

    print()
    print("Query variations:")

    for index, variation in enumerate(
        enriched.query_variations,
        start=1,
    ):
        print(
            f"{index:02}. {variation}"
        )

    # --------------------------------------------------
    # Enrichment contract checks
    # --------------------------------------------------

    assert enriched.canonical_query.strip()
    assert enriched.topic.strip()

    assert (
        8
        <= len(enriched.query_variations)
        <= 10
    )

    normalized_variations = {
        variation.casefold().strip()
        for variation
        in enriched.query_variations
    }

    assert (
        len(normalized_variations)
        == len(enriched.query_variations)
    ), "Query variations must be distinct"

    # ==================================================
    # Phase 1: Structure Extraction
    # ==================================================

    extracted = await extractor.extract(
        enriched=enriched,
        siis_reference=siis_reference,
    )

    print()
    print("=" * 80)
    print("STRUCTURE EXTRACTION RESULT")
    print("=" * 80)

    print(
        f"Topic:      "
        f"{extracted.topic}"
    )

    print(
        f"Title:      "
        f"{extracted.title}"
    )

    print(
        f"Confidence: "
        f"{extracted.confidence}"
    )

    print()
    print("Actions:")

    for action_index, action in enumerate(
        extracted.actions,
        start=1,
    ):
        print()
        print(
            f"Action {action_index}"
        )

        print("-" * 60)

        print(
            f"Name:          "
            f"{action.action_name}"
        )

        print(
            f"Target screen: "
            f"{action.target_screen}"
        )

        print(
            f"Category:      "
            f"{action.category.value}"
        )

        print("Steps:")

        for step_index, step in enumerate(
            action.steps,
            start=1,
        ):
            print(
                f"  {step_index}. {step}"
            )

        print(
            f"Evidence: "
            f"{action.source_text}"
        )

    # ==================================================
    # Extraction contract checks
    # ==================================================

    assert extracted.topic.strip()

    assert extracted.title.strip()

    assert extracted.actions

    assert (
        0
        <= extracted.confidence
        <= 1
    )

    # --------------------------------------------------
    # Validate every extracted action
    # --------------------------------------------------

    critical_seen = False

    for index, action in enumerate(
        extracted.actions,
        start=1,
    ):
        # ----------------------------------------------
        # Required fields
        # ----------------------------------------------

        assert (
            action.action_name.strip()
        ), (
            f"Action {index} has "
            "an empty action name"
        )

        assert (
            action.steps
        ), (
            f"Action {index} has "
            "no troubleshooting steps"
        )

        assert (
            action.source_text.strip()
        ), (
            f"Action {index} has "
            "no grounding evidence"
        )

        # ----------------------------------------------
        # Critical-last rule
        # ----------------------------------------------

        if action.category.value == "critical":
            critical_seen = True

        elif critical_seen:
            pytest.fail(
                "Non-critical action appeared "
                "after a critical action: "
                f"{action.action_name}"
            )

        # ----------------------------------------------
        # SIIS grounding
        # ----------------------------------------------
        #
        # StructureExtractor owns the grounding policy.
        # Reuse the same check here instead of creating
        # a second, incompatible normalization rule.
        # ----------------------------------------------

        grounded = extractor._is_grounded(
            action.source_text,
            siis_reference,
        )

        assert grounded, (
            f"Action {index} was not grounded "
            "in the SIIS reference.\n"
            f"Action: {action.action_name}\n"
            f"Evidence: {action.source_text!r}"
        )

    # --------------------------------------------------
    # Final diagnostic output
    # --------------------------------------------------

    print()
    print("=" * 80)
    print("LIVE PIPELINE VALIDATION PASSED")
    print("=" * 80)

    print(
        f"Primary model:  "
        f"{extractor.model}"
    )

    print(
        f"Fallback model: "
        f"{extractor.fallback_model}"
    )

    print(
        f"Actions:        "
        f"{len(extracted.actions)}"
    )
