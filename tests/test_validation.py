"""Checkpoint 4 verification: deterministic validation rules.

Covers test plan items 11 and 12. These are pure functions over the contract objects, so
they need no database and run in milliseconds.

Each test states the rule it enforces, because these rules come from the specification
rather than from general good practice, and a future reader needs to know that changing
one is changing the contract.
"""

from __future__ import annotations

import pytest

from app.contract.schema import (
    DUMMY_POSITIVE_DEEPLINK,
    Action,
    ContextDeeplinkResponse,
    Deeplink,
    Goal,
    StepGroup,
    actionCategory,
)
from app.validation.rules import (
    contains_url,
    is_sentence_case,
    is_title_case,
    validate_plan,
    validate_query_variations,
)

CATALOG = frozenset({"bixby://masked/act/9001", "bixby://masked/act/9308"})


def make_action(
    *,
    name="Battery Usage by App",
    description="It will show battery usage clearly",
    steps=("Navigate to and open Settings.", "Tap on Battery."),
    deeplink="bixby://masked/act/9001",
    category=actionCategory.auto,
) -> Action:
    link = (
        Deeplink(deeplink=deeplink, description="Open battery settings", message="")
        if deeplink
        else None
    )
    return Action(
        actionName=name,
        description=description,
        stepGroups=[StepGroup(steps=list(steps), actionableDeeplink=link)],
        category=category,
    )


def make_goal(**kwargs) -> Goal:
    return Goal(
        goal=kwargs.get(
            "goal", "Follow these steps to perform this Battery Troubleshooting"
        ),
        title=kwargs.get("title", "Battery fast drain"),
        actions=kwargs.get("actions", [make_action()]),
        score=kwargs.get("score", 0.9),
    )


def plan(**kwargs) -> ContextDeeplinkResponse:
    return ContextDeeplinkResponse(contexts=[make_goal(**kwargs)])


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Visit https://samsung.com/support",
        "Go to http://example.org",
        "See www.samsung.com for help",
        "Read [the guide](https://example.com)",
        "Check samsung.com for details",
    ],
)
def test_contains_url_detects_leaks(text):
    assert contains_url(text) is True


@pytest.mark.parametrize(
    "text",
    ["Navigate to and open Settings.", "Tap on Battery.", "Select QHD resolution."],
)
def test_contains_url_allows_clean_steps(text):
    assert contains_url(text) is False


def test_title_case_helper():
    assert is_title_case("Battery Usage by App")
    assert is_title_case("Configure Navigation Bar Settings")
    assert not is_title_case("battery usage by app")
    assert not is_title_case("Battery usage by App")


def test_sentence_case_helper():
    assert is_sentence_case("Battery fast drain")
    assert not is_sentence_case("Battery Fast Drain")
    assert not is_sentence_case("battery fast drain")


# --------------------------------------------------------------------------
# Structural rules
# --------------------------------------------------------------------------

def test_valid_plan_passes():
    assert validate_plan(plan(), CATALOG).ok


def test_empty_contexts_is_valid():
    """An empty list is the specified representation of "no viable solution", not an
    error."""
    assert validate_plan(ContextDeeplinkResponse(contexts=[]), CATALOG).ok


def test_goal_must_use_the_exact_sentence():
    report = validate_plan(plan(goal="Fix your battery"), CATALOG)
    assert "goal_syntax" in report.codes()


def test_goal_accepts_configuration_variant():
    assert validate_plan(
        plan(goal="Follow these steps to perform this Display Configuration"), CATALOG
    ).ok


@pytest.mark.parametrize("title", ["Battery", "Battery fast drain problem today"])
def test_title_must_be_two_or_three_words(title):
    assert "title_length" in validate_plan(plan(title=title), CATALOG).codes()


def test_title_must_be_sentence_case():
    assert "title_case" in validate_plan(plan(title="Battery Fast Drain"), CATALOG).codes()


@pytest.mark.parametrize("score", [-0.1, 1.5])
def test_score_must_be_within_range(score):
    assert "score_range" in validate_plan(plan(score=score), CATALOG).codes()


def test_description_must_start_with_it_will():
    action = make_action(description="Shows battery usage for apps")
    assert "description_prefix" in validate_plan(plan(actions=[action]), CATALOG).codes()


@pytest.mark.parametrize("description", ["It will help", "It will " + "word " * 20])
def test_description_length_is_bounded(description):
    action = make_action(description=description)
    assert "description_length" in validate_plan(plan(actions=[action]), CATALOG).codes()


def test_description_bounds_are_configurable():
    """The written rule is 5 to 7 words, but the official sample_output.json uses 9 and
    12, so the default upper bound follows the sample. Tightening to the written rule must
    stay a configuration change, not a code change."""
    action = make_action(description="It will facilitate secure data transfer between your devices")

    assert validate_plan(plan(actions=[action]), CATALOG, max_words=15).ok
    strict = validate_plan(plan(actions=[action]), CATALOG, max_words=7)
    assert "description_length" in strict.codes()


def test_the_official_sample_output_validates():
    """Regression guard on the decision above.

    sample_output.json is the reference artifact shipped with the dataset. If our
    validator rejects it, either the validator or that decision is wrong, and this test is
    where that shows up.
    """
    import json as _json
    import pathlib as _pathlib

    sample_path = _pathlib.Path("data/official/sample_output.json")
    catalog_path = _pathlib.Path("data/official/deeplinks.json")
    if not sample_path.exists() or not catalog_path.exists():
        pytest.skip("official kit not present")

    from app.catalog.loader import load_catalog
    from app.contract.schema import ContextDeeplinkResponse

    payload = _json.loads(sample_path.read_text(encoding="utf-8"))
    response = ContextDeeplinkResponse.model_validate(payload["response"])
    permitted = load_catalog(catalog_path).deeplink_set()

    report = validate_plan(response, permitted)
    assert report.ok, report.summary()


def test_action_name_must_be_title_case():
    action = make_action(name="battery usage by app")
    assert "action_name_case" in validate_plan(plan(actions=[action]), CATALOG).codes()


def test_goal_with_no_actions_is_rejected():
    assert "no_actions" in validate_plan(plan(actions=[]), CATALOG).codes()


# --------------------------------------------------------------------------
# Deeplink integrity
# --------------------------------------------------------------------------

def test_deeplink_outside_the_catalog_is_rejected():
    """The single most important rule: a URI that is not in the catalog is fabricated."""
    action = make_action(deeplink="bixby://masked/act/hallucinated")
    assert "unknown_deeplink" in validate_plan(plan(actions=[action]), CATALOG).codes()


def test_dummy_positive_placeholder_is_permitted():
    """A reserved placeholder for a real screen that is simply not indexed."""
    action = make_action(deeplink=DUMMY_POSITIVE_DEEPLINK)
    assert validate_plan(plan(actions=[action]), CATALOG).ok


def test_dummy_positive_can_be_disallowed():
    action = make_action(deeplink=DUMMY_POSITIVE_DEEPLINK)
    report = validate_plan(plan(actions=[action]), CATALOG, allow_dummy_positive=False)
    assert "unknown_deeplink" in report.codes()


def test_manual_action_cannot_carry_a_deeplink():
    """A manual action is a physical intervention, so it has no screen to open."""
    action = make_action(category=actionCategory.manual)
    assert "manual_with_deeplink" in validate_plan(plan(actions=[action]), CATALOG).codes()


def test_manual_action_without_deeplink_is_fine():
    action = make_action(category=actionCategory.manual, deeplink=None)
    assert validate_plan(plan(actions=[action]), CATALOG).ok


# --------------------------------------------------------------------------
# URL leakage
# --------------------------------------------------------------------------

def test_url_in_a_step_is_rejected():
    action = make_action(steps=("Navigate to Settings.", "Visit https://samsung.com/support"))
    assert "url_leak" in validate_plan(plan(actions=[action]), CATALOG).codes()


def test_url_in_the_title_is_rejected():
    assert "url_leak" in validate_plan(plan(title="See www.x.com"), CATALOG).codes()


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------

def test_critical_action_must_come_last():
    """Destructive operations are ordered after safe ones."""
    actions = [
        make_action(name="Restart the Device", category=actionCategory.critical,
                    deeplink="bixby://masked/act/9308"),
        make_action(name="Battery Usage by App", category=actionCategory.auto),
    ]
    assert "critical_ordering" in validate_plan(plan(actions=actions), CATALOG).codes()


def test_critical_last_is_accepted():
    actions = [
        make_action(name="Battery Usage by App", category=actionCategory.auto),
        make_action(name="Restart the Device", category=actionCategory.critical,
                    deeplink="bixby://masked/act/9308"),
    ]
    assert validate_plan(plan(actions=actions), CATALOG).ok


def test_multiple_criticals_at_the_end_are_accepted():
    actions = [
        make_action(name="Battery Usage by App", category=actionCategory.auto),
        make_action(name="Restart the Device", category=actionCategory.critical,
                    deeplink="bixby://masked/act/9308"),
        make_action(name="Restart in Safe Mode", category=actionCategory.critical,
                    deeplink="bixby://masked/act/9308"),
    ]
    assert validate_plan(plan(actions=actions), CATALOG).ok


# --------------------------------------------------------------------------
# Query variations
# --------------------------------------------------------------------------

def test_query_variations_must_number_eight_to_ten():
    assert "variation_count" in validate_query_variations(["a", "b", "c"]).codes()
    assert validate_query_variations([f"phrasing {i}" for i in range(9)]).ok


def test_query_variations_must_be_distinct():
    variations = ["same"] * 9
    assert "variation_duplicates" in validate_query_variations(variations).codes()


def test_query_variations_must_not_leak_urls():
    variations = [f"phrasing {i}" for i in range(8)] + ["see https://x.com"]
    assert "url_leak" in validate_query_variations(variations).codes()


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def test_report_collects_every_violation_not_just_the_first():
    """A caller fixing output needs the whole list, not one error at a time."""
    action = make_action(name="lowercase name", description="Too short")
    report = validate_plan(plan(actions=[action], title="Bad Title Case Here"), CATALOG)
    assert len(report.violations) >= 3
    assert "valid" not in report.summary()
