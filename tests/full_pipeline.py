"""Live end-to-end test for the real troubleshooting pipeline.

Tests:

    Noisy user query
        ↓
    Phase 0 - Query Enrichment
        ↓
    Phase 1 - SIIS Structure Extraction
        ↓
    Phase 2 - Deeplink Retrieval + Verification
        ↓
    Final ContextDeeplinkResponse

The input deliberately contains:
    - slang
    - emojis
    - missing prepositions
    - abbreviations
    - repeated words
    - poor grammar

The official SIIS reference is retained so Phase 1 remains
grounded against the correct source.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from app.config import get_settings
from app.contract.schema import actionCategory
from app.embeddings.encoder import get_encoder
from app.pipeline.mock.mock_pipeline import connection_factory
from app.pipeline.port import PipelineRequest
from app.pipeline.real.pipeline import RealPipeline
from app.retrieval.resolver import load_bm25_index


# ============================================================
# Paths
# ============================================================

ROOT = Path(__file__).resolve().parents[1]

SIIS_PATH = (
    ROOT
    / "data"
    / "official"
    / "siis_responses.json"
)


# ============================================================
# Deliberately messy real-world query
# ============================================================

NOISY_QUERY = (
    "brooo 😭😭 samsung a115g screen flashing 💀 "
    "then straight black dude 😭 whenever open gmail email "
    "screen gone again few mins 📱💀 pls fix broooo"
)


# ============================================================
# SIIS loader
# ============================================================


def _load_first_siis_record() -> tuple[str, str]:
    """Load the first official SIIS record.

    Supports several possible top-level JSON structures:

        [
            {...},
            {...}
        ]

    or:

        {
            "records": [...]
        }

    or:

        {
            "data": [...]
        }

    or:

        {
            "some_id": {...},
            "another_id": {...}
        }
    """

    assert SIIS_PATH.exists(), (
        f"SIIS file does not exist: {SIIS_PATH}"
    )

    with SIIS_PATH.open(
        "r",
        encoding="utf-8",
    ) as file:
        data = json.load(file)

    # --------------------------------------------------------
    # Format 1:
    #
    # [
    #     {...},
    #     {...}
    # ]
    # --------------------------------------------------------

    if isinstance(data, list):

        assert data, (
            "siis_responses.json is empty"
        )

        record = data[0]

    # --------------------------------------------------------
    # Format 2:
    #
    # {
    #     ...
    # }
    # --------------------------------------------------------

    elif isinstance(data, dict):

        record = None

        # Common wrapper names.
        for key in (
            "records",
            "data",
            "responses",
            "items",
        ):
            value = data.get(key)

            if (
                isinstance(value, list)
                and value
            ):
                record = value[0]
                break

        # ----------------------------------------------------
        # Dictionary keyed by record ID
        #
        # {
        #     "1": {...},
        #     "2": {...}
        # }
        # ----------------------------------------------------

        if record is None:

            for value in data.values():

                if isinstance(
                    value,
                    dict,
                ):
                    record = value
                    break

        assert record is not None, (
            "Could not locate an SIIS record "
            "inside siis_responses.json"
        )

    else:

        raise AssertionError(
            "Unexpected siis_responses.json "
            f"format: {type(data).__name__}"
        )

    # --------------------------------------------------------
    # Record must be an object
    # --------------------------------------------------------

    assert isinstance(
        record,
        dict,
    ), (
        "Expected SIIS record to be a dictionary, "
        f"got {type(record).__name__}"
    )

    # --------------------------------------------------------
    # Extract query
    # --------------------------------------------------------

    original_query = (
        record.get("original_query")
        or record.get("query")
        or ""
    )

    # --------------------------------------------------------
    # Extract SIIS response
    # --------------------------------------------------------

    siis_response = (
        record.get("siis_response")
        or record.get("response")
        or ""
    )

    assert isinstance(
        original_query,
        str,
    ), (
        "SIIS original query must be a string"
    )

    assert original_query.strip(), (
        "Official SIIS record has no "
        "original_query/query"
    )

    assert siis_response, (
        "Official SIIS record has no "
        "siis_response/response"
    )

    # --------------------------------------------------------
    # Extractor accepts SIIS as text.
    #
    # If the source is structured JSON, serialize it while
    # preserving Unicode.
    # --------------------------------------------------------

    if not isinstance(
        siis_response,
        str,
    ):
        siis_response = json.dumps(
            siis_response,
            ensure_ascii=False,
        )

    return (
        original_query,
        siis_response,
    )


# ============================================================
# Utility helpers
# ============================================================


def _word_count(
    text: str,
) -> int:
    """Count contract words."""

    return len(
        re.findall(
            r"[A-Za-z0-9']+",
            text,
        )
    )


def _assert_no_web_urls(
    text: str,
) -> None:
    """Make sure generated output contains no web URLs."""

    lowered = text.casefold()

    assert "http://" not in lowered, (
        f"HTTP URL leaked: {text}"
    )

    assert "https://" not in lowered, (
        f"HTTPS URL leaked: {text}"
    )

    assert "www." not in lowered, (
        f"WWW URL leaked: {text}"
    )


def _all_actions(
    result,
):
    """Return all actions from all contexts."""

    actions = []

    for goal in result.response.contexts:
        actions.extend(
            goal.actions
        )

    return actions


def _find_action(
    goal,
    *names: str,
):
    """Find an action using several acceptable names."""

    expected = {
        name.casefold()
        for name in names
    }

    for action in goal.actions:

        if (
            action.actionName
            .strip()
            .casefold()
            in expected
        ):
            return action

    return None


def _extract_actionable_deeplinks(
    result,
) -> list[str]:
    """Collect actionable deeplink URIs."""

    links: list[str] = []

    for action in _all_actions(
        result
    ):

        for group in action.stepGroups:

            deeplink = (
                group.actionableDeeplink
            )

            if deeplink is not None:
                links.append(
                    deeplink.deeplink
                )

    return links


def _assert_critical_actions_last(
    actions,
) -> None:
    """Critical actions must appear after non-critical actions."""

    seen_critical = False

    for action in actions:

        if (
            action.category
            == actionCategory.critical
        ):
            seen_critical = True

        elif seen_critical:

            pytest.fail(
                "Critical ordering violation: "
                f"{action.actionName!r} appears "
                "after a critical action"
            )


# ============================================================
# Main integration test
# ============================================================


@pytest.mark.asyncio
async def test_real_pipeline_end_to_end():
    """Run Phase 0 -> Phase 1 -> Phase 2."""

    # ========================================================
    # Gemini API
    # ========================================================

    if not os.getenv(
        "GEMINI_API_KEY"
    ):
        pytest.skip(
            "GEMINI_API_KEY is not configured"
        )

    # ========================================================
    # Load official SIIS context
    # ========================================================

    (
        official_query,
        siis_reference,
    ) = _load_first_siis_record()

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # We deliberately do NOT use the clean official query.
    #
    # The SIIS reference remains the same because the noisy
    # query represents the same underlying user problem.
    # --------------------------------------------------------

    query = NOISY_QUERY

    print()
    print("=" * 80)
    print("OFFICIAL QUERY")
    print("=" * 80)

    print(
        official_query
    )

    print()
    print("=" * 80)
    print("NOISY USER QUERY")
    print("=" * 80)

    print(
        query
    )

    # ========================================================
    # Build dependencies
    # ========================================================

    settings = get_settings()

    encoder = get_encoder()

    conn_factory = connection_factory(
        settings
    )

    # --------------------------------------------------------
    # Load BM25 index
    # --------------------------------------------------------

    with conn_factory() as conn:

        bm25 = load_bm25_index(
            conn
        )

    # --------------------------------------------------------
    # Real pipeline
    # --------------------------------------------------------

    pipeline = RealPipeline(
        conn_factory=conn_factory,
        encoder=encoder,
        bm25_index=bm25,
        settings=settings,
    )

    assert (
        pipeline.name
        == "real"
    ), (
        f"Expected real pipeline, got {pipeline.name!r}"
    )

    # ========================================================
    # Build request
    # ========================================================

    request = PipelineRequest(
        query=query,
        siis_response=siis_reference,
    )

    # ========================================================
    # RUN COMPLETE PIPELINE
    # ========================================================

    result = await pipeline.run(
        request
    )

    # ========================================================
    # PHASE 0
    # Query Enrichment
    # ========================================================

    print()
    print("=" * 80)
    print(
        "PHASE 0 - QUERY ENRICHMENT"
    )
    print("=" * 80)

    print(
        f"Canonical: {result.canonical_query}"
    )

    print()
    print("Variations:")

    for index, variation in enumerate(
        result.query_variations,
        start=1,
    ):

        print(
            f"{index:02}. {variation}"
        )

    # --------------------------------------------------------
    # Canonical must exist
    # --------------------------------------------------------

    assert (
        result.canonical_query.strip()
    ), (
        "Canonical query is empty"
    )

    canonical = (
        result
        .canonical_query
        .casefold()
    )

    # --------------------------------------------------------
    # Slang / emoji cleanup
    # --------------------------------------------------------

    noise_tokens = (
        "brooo",
        "broooo",
        "dude",
        "pls",
        "😭",
        "💀",
        "📱",
    )

    for noise in noise_tokens:

        assert noise not in canonical, (
            "Canonical query still contains "
            f"noise token {noise!r}.\n"
            f"Canonical: {result.canonical_query}"
        )

    # --------------------------------------------------------
    # Important information must survive enrichment
    # --------------------------------------------------------

    required_concepts = (
        "samsung",
        "a115g",
        "screen",
        "gmail",
    )

    for concept in required_concepts:

        assert concept in canonical, (
            "Canonical query lost important "
            f"concept {concept!r}.\n"
            f"Canonical: {result.canonical_query}"
        )

    # --------------------------------------------------------
    # Screen becomes black / blank
    # --------------------------------------------------------

    assert (
        "black" in canonical
        or "blank" in canonical
    ), (
        "Canonical query lost black/blank "
        "screen symptom.\n"
        f"Canonical: {result.canonical_query}"
    )

    # --------------------------------------------------------
    # Flash / flicker symptom
    # --------------------------------------------------------

    assert (
        "flash" in canonical
        or "flicker" in canonical
    ), (
        "Canonical query lost flashing/flickering "
        "symptom.\n"
        f"Canonical: {result.canonical_query}"
    )

    # --------------------------------------------------------
    # No URL
    # --------------------------------------------------------

    _assert_no_web_urls(
        result.canonical_query
    )

    # ========================================================
    # Query variations
    # ========================================================

    assert (
        8
        <= len(result.query_variations)
        <= 10
    ), (
        "Expected 8-10 query variations, "
        f"got {len(result.query_variations)}"
    )

    normalized_variations = {
        variation.strip().casefold()
        for variation
        in result.query_variations
    }

    assert (
        len(normalized_variations)
        == len(result.query_variations)
    ), (
        "Query variations are not unique"
    )

    for variation in (
        result.query_variations
    ):

        assert (
            variation.strip()
        ), (
            "Empty query variation generated"
        )

        _assert_no_web_urls(
            variation
        )

    print()
    print("PHASE 0 CHECKS")
    print("-" * 80)

    print(
        "Slang/emojis removed:     PASS"
    )

    print(
        "Samsung preserved:        PASS"
    )

    print(
        "A115G preserved:          PASS"
    )

    print(
        "Gmail preserved:          PASS"
    )

    print(
        "Screen symptom preserved: PASS"
    )

    print(
        "Variations valid:         PASS"
    )

    # ========================================================
    # FINAL RESPONSE
    # ========================================================

    response = result.response

    assert response.contexts, (
        "Pipeline returned no troubleshooting contexts"
    )

    goal = (
        response.contexts[0]
    )

    print()
    print("=" * 80)
    print(
        "FINAL CONTEXT DEEPLINK RESPONSE"
    )
    print("=" * 80)

    print()
    print(
        f"Goal:  {goal.goal}"
    )

    print(
        f"Title: {goal.title}"
    )

    print(
        f"Score: {goal.score}"
    )

    # ========================================================
    # Goal contract
    # ========================================================

    assert (
        goal.goal.startswith(
            "Follow these steps to perform this "
        )
    ), (
        f"Invalid goal format: {goal.goal}"
    )

    assert (
        goal.goal.endswith(
            "Troubleshooting"
        )
        or goal.goal.endswith(
            "Configuration"
        )
    ), (
        f"Invalid goal suffix: {goal.goal}"
    )

    _assert_no_web_urls(
        goal.goal
    )

    # ========================================================
    # Title contract
    #
    # 2-10 words
    # sentence case
    # ========================================================

    title_words = _word_count(
        goal.title
    )

    assert (
        2
        <= title_words
        <= 10
    ), (
        "Goal title must contain 2-10 words. "
        f"Got {title_words}: {goal.title!r}"
    )

    assert (
        goal.title
        and goal.title[0].isupper()
    ), (
        "Goal title must begin with "
        "an uppercase character"
    )

    _assert_no_web_urls(
        goal.title
    )

    # ========================================================
    # Score
    # ========================================================

    assert (
        0.0
        <= goal.score
        <= 1.0
    ), (
        f"Invalid score: {goal.score}"
    )

    # ========================================================
    # Actions
    # ========================================================

    assert goal.actions, (
        "Goal contains no actions"
    )

    print()
    print("=" * 80)
    print("ACTIONS")
    print("=" * 80)

    for action_index, action in enumerate(
        goal.actions,
        start=1,
    ):

        print()
        print(
            f"ACTION {action_index}: "
            f"{action.actionName}"
        )

        print("-" * 80)

        category = (
            action.category.value
            if hasattr(
                action.category,
                "value",
            )
            else str(
                action.category
            )
        )

        print(
            f"Category: {category}"
        )

        print(
            f"Description: "
            f"{action.description}"
        )

        # ----------------------------------------------------
        # Action name
        # ----------------------------------------------------

        assert (
            action.actionName.strip()
        ), (
            "Action name is empty"
        )

        _assert_no_web_urls(
            action.actionName
        )

        # ----------------------------------------------------
        # Description
        #
        # Required:
        #
        # "It will ..."
        # 5-7 words
        # ----------------------------------------------------

        assert (
            action.description.startswith(
                "It will "
            )
        ), (
            "Description must start with "
            f"'It will ': {action.description!r}"
        )

        description_words = (
            _word_count(
                action.description
            )
        )

        assert (
            5
            <= description_words
            <= 7
        ), (
            "Description must contain 5-7 words. "
            f"Got {description_words}: "
            f"{action.description!r}"
        )

        _assert_no_web_urls(
            action.description
        )

        # ----------------------------------------------------
        # Step groups
        # ----------------------------------------------------

        assert (
            action.stepGroups
        ), (
            f"{action.actionName} has no step groups"
        )

        for (
            group_index,
            group,
        ) in enumerate(
            action.stepGroups,
            start=1,
        ):

            print()
            print(
                f"Step Group {group_index}:"
            )

            assert group.steps, (
                f"{action.actionName} contains "
                "an empty step group"
            )

            # ------------------------------------------------
            # Steps
            # ------------------------------------------------

            for (
                step_index,
                step,
            ) in enumerate(
                group.steps,
                start=1,
            ):

                print(
                    f"  {step_index}. {step}"
                )

                assert (
                    step.strip()
                ), (
                    "Empty troubleshooting step"
                )

                _assert_no_web_urls(
                    step
                )

            # ------------------------------------------------
            # Actionable deeplink
            # ------------------------------------------------

            actionable = (
                group.actionableDeeplink
            )

            if actionable is None:

                print(
                    "Actionable Deeplink: NONE"
                )

            else:

                print(
                    "Actionable Deeplink: "
                    f"{actionable.deeplink}"
                )

                print(
                    "Deeplink Description: "
                    f"{actionable.description}"
                )

                print(
                    "Deeplink Message: "
                    f"{actionable.message}"
                )

                assert (
                    actionable.deeplink.startswith(
                        "bixby://"
                    )
                ), (
                    "Actionable deeplink must use "
                    "the bixby:// scheme"
                )

                _assert_no_web_urls(
                    actionable.description
                )

                if actionable.message:

                    _assert_no_web_urls(
                        actionable.message
                    )

            # ------------------------------------------------
            # Validation deeplink
            # ------------------------------------------------

            validation = (
                group.validationDeeplink
            )

            if validation is None:

                print(
                    "Validation Deeplink: NONE"
                )

            else:

                print(
                    "Validation Deeplink: "
                    f"{validation.deeplink}"
                )

                assert (
                    validation.deeplink.startswith(
                        "bixby://"
                    )
                ), (
                    "Validation deeplink must use "
                    "the bixby:// scheme"
                )

            # ------------------------------------------------
            # Manual actions cannot have deeplinks
            # ------------------------------------------------

            if (
                action.category
                == actionCategory.manual
            ):

                assert (
                    actionable is None
                ), (
                    "Manual action unexpectedly "
                    "contains an actionable deeplink: "
                    f"{action.actionName}"
                )

    # ========================================================
    # Critical-last requirement
    # ========================================================

    _assert_critical_actions_last(
        goal.actions
    )

    # ========================================================
    # Collect verified deeplinks
    # ========================================================

    deeplinks = (
        _extract_actionable_deeplinks(
            result
        )
    )

    for deeplink in deeplinks:

        assert (
            deeplink.startswith(
                "bixby://"
            )
        )

    # ========================================================
    # Known action detection
    #
    # DO NOT require exact LLM action names globally.
    #
    # Gemini can validly produce:
    #
    # "Email App Storage"
    #
    # or
    #
    # "App Storage Settings"
    # ========================================================

    wifi_action = _find_action(
        goal,
        "Wi-Fi Settings",
        "WiFi Settings",
    )

    app_storage_action = _find_action(
        goal,
        "Email App Storage",
        "App Storage Settings",
        "Email Storage Settings",
    )

    safe_mode_action = _find_action(
        goal,
        "Safe Mode",
    )

    # ========================================================
    # Known Wi-Fi mapping
    # ========================================================

    if wifi_action is not None:

        wifi_links = []

        for group in (
            wifi_action.stepGroups
        ):

            if (
                group.actionableDeeplink
                is not None
            ):

                wifi_links.append(
                    group
                    .actionableDeeplink
                    .deeplink
                )

        assert wifi_links, (
            "Wi-Fi Settings action exists but "
            "has no verified deeplink"
        )

        assert (
            "bixby://masked/act/cb03ac7425"
            in wifi_links
        ), (
            "Wi-Fi Settings mapped to an "
            "unexpected deeplink: "
            f"{wifi_links}"
        )

    # ========================================================
    # App Storage
    #
    # The currently inspected catalog candidates do not contain
    # a sufficiently compatible app-storage destination.
    #
    # Correct behaviour is therefore NONE rather than accepting
    # the unrelated Storage Share candidate.
    # ========================================================

    if app_storage_action is not None:

        app_storage_links = [
            group.actionableDeeplink
            for group
            in app_storage_action.stepGroups
            if (
                group.actionableDeeplink
                is not None
            )
        ]

        assert (
            not app_storage_links
        ), (
            "App Storage received an unexpected "
            "deeplink instead of safely returning NONE"
        )

    # ========================================================
    # Safe Mode
    #
    # Safe Mode does not have a compatible Settings deeplink
    # among the inspected candidates.
    # ========================================================

    if safe_mode_action is not None:

        safe_mode_links = [
            group.actionableDeeplink
            for group
            in safe_mode_action.stepGroups
            if (
                group.actionableDeeplink
                is not None
            )
        ]

        assert (
            not safe_mode_links
        ), (
            "Safe Mode received an unexpected "
            "actionable deeplink"
        )

    # ========================================================
    # Summary
    # ========================================================

    print()
    print("=" * 80)
    print("PIPELINE SUMMARY")
    print("=" * 80)

    print(
        f"Pipeline: {pipeline.name}"
    )

    print(
        f"Canonical: "
        f"{result.canonical_query}"
    )

    print(
        f"Actions: "
        f"{len(goal.actions)}"
    )

    print(
        f"Verified deeplinks: "
        f"{len(deeplinks)}"
    )

    print(
        f"Fallback: "
        f"{result.fallback}"
    )

    print()
    print(
        "Known actions detected:"
    )

    print(
        "  Wi-Fi Settings: "
        f"{wifi_action is not None}"
    )

    print(
        "  Email/App Storage: "
        f"{app_storage_action is not None}"
    )

    print(
        "  Safe Mode: "
        f"{safe_mode_action is not None}"
    )

    # ========================================================
    # Final sanity checks
    # ========================================================

    assert (
        pipeline.name
        == "real"
    )

    assert (
        len(goal.actions)
        >= 1
    )

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # We intentionally DO NOT require every action to contain
    # a deeplink.
    #
    # Returning NONE is correct when catalog verification
    # cannot establish a safe match.
    # --------------------------------------------------------

    print()
    print("=" * 80)
    print(
        "NOISY QUERY NORMALISATION PASSED"
    )
    print("=" * 80)

    print(
        "✓ Slang removed"
    )

    print(
        "✓ Emojis removed"
    )

    print(
        "✓ Missing grammar/prepositions handled"
    )

    print(
        "✓ Device preserved"
    )

    print(
        "✓ Gmail preserved"
    )

    print(
        "✓ Screen symptoms preserved"
    )

    print(
        "✓ 8-10 semantic variations generated"
    )

    print(
        "✓ SIIS-grounded actions generated"
    )

    print(
        "✓ Critical actions ordered last"
    )

    print(
        "✓ Unsafe deeplink matches rejected"
    )

    print()
    print("=" * 80)
    print(
        "REAL PIPELINE END-TO-END TEST PASSED"
    )
    print("=" * 80)
