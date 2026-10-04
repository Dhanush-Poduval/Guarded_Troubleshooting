"""The auto invariant: an auto action must carry a verified catalog deeplink.

"auto" tells the client the action can be carried out by opening a setting. An auto action
with nothing to open is a promise the response cannot keep, so the pipeline downgrades it
to manual once deeplink resolution comes back empty, and validation refuses one that
reaches a user by any other route — including out of the semantic cache, where a plan
validated under an earlier rule set is re-checked on every read.

The three actions that failed the benchmark — Email App Storage, Open Smart Switch App and
Phone Aspect Ratio — have no destination in the official catalog. That was verified against
data/official/deeplinks.json rather than assumed; see test_the_three_benchmark_actions_
have_no_catalog_destination below, which is the evidence for downgrading them rather than
improving the matching.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.contract.schema import (
    Action,
    ContextDeeplinkResponse,
    Deeplink,
    Goal,
    StepGroup,
    ValidationDeepLink,
    actionCategory,
)
from app.validation.rules import validate_action, validate_plan, ValidationReport

CATALOG = pathlib.Path("data/official/deeplinks.json")

REAL_DEEPLINK = "bixby://masked/act/b3ed3ed663"
REAL_VALIDATION = "bixby://masked/val/266037d0c5"
PERMITTED = frozenset({REAL_DEEPLINK, REAL_VALIDATION})


def build_action(category, *, deeplink=REAL_DEEPLINK, groups=1):
    """One action, optionally carrying a catalog deeplink."""
    return Action(
        actionName="Back Up Phone Data",
        description="It will facilitate secure data transfer between your devices",
        stepGroups=[
            StepGroup(
                steps=["Navigate to and open Settings.", "Tap on Accounts and backup."],
                actionableDeeplink=(
                    Deeplink(
                        deeplink=deeplink,
                        description="Enables data backup to Samsung Cloud.",
                        message="Enable Back up data",
                    )
                    if deeplink and index == 0
                    else None
                ),
            )
            for index in range(groups)
        ],
        category=category,
    )


def check(action) -> list[str]:
    report = ValidationReport()
    validate_action(action, PERMITTED, report, "contexts[0].actions[0]")
    return [v.code for v in report.violations]


def wrap(action) -> ContextDeeplinkResponse:
    return ContextDeeplinkResponse(
        contexts=[
            Goal(
                goal="Follow these steps to perform this Screen Troubleshooting",
                title="Screen display damage",
                actions=[action],
                score=0.9,
            )
        ]
    )


# ------------------------------------------------------- catalog evidence


def test_the_three_benchmark_actions_have_no_catalog_destination():
    """The evidence for downgrading rather than improving the matching.

    If a future catalog does gain one of these destinations this test fails, which is the
    signal to route that action back to auto instead of leaving it manual.
    """
    entries = json.loads(CATALOG.read_text(encoding="utf-8"))["deeplinks"]

    def mentions(term: str) -> int:
        term = term.lower()
        return sum(
            1
            for e in entries
            if term
            in " ".join(
                str(e.get(k, "") or "")
                for k in ("description", "message", "qna_description")
            ).lower()
        )

    # Email App Storage: no per-app storage or cache screen exists in the catalog.
    assert mentions("email") == 0
    assert mentions("app storage") == 0
    assert mentions("clear cache") == 0

    # Open Smart Switch App: the catalog covers device Settings screens, not applications.
    assert mentions("smart switch") == 0
    assert mentions("transfer data") == 0

    # Phone Aspect Ratio: no aspect-ratio or display-size screen. "Screen zoom" exists but
    # scales UI elements rather than changing how an app fills a tall display, so
    # attaching it would be the loosely related match this rule exists to forbid.
    assert mentions("aspect") == 0
    assert mentions("display size") == 0
    assert mentions("full screen apps") == 0


# ----------------------------------------------------------- the rule


def test_an_auto_action_with_a_catalog_deeplink_passes():
    assert "auto_action_missing_deeplink" not in check(build_action(actionCategory.auto))


def test_an_auto_action_without_a_deeplink_is_rejected():
    codes = check(build_action(actionCategory.auto, deeplink=None))
    assert "auto_action_missing_deeplink" in codes


def test_a_manual_action_without_a_deeplink_is_fine():
    """Manual is exactly the category for an action with nothing to open."""
    codes = check(build_action(actionCategory.manual, deeplink=None))
    assert "auto_action_missing_deeplink" not in codes


def test_a_critical_action_without_a_deeplink_is_preserved():
    """critical describes how disruptive an action is, not how it is carried out, so it
    is never required to carry a deeplink and is never downgraded."""
    codes = check(build_action(actionCategory.critical, deeplink=None))
    assert "auto_action_missing_deeplink" not in codes


def test_an_auto_action_satisfies_the_rule_from_any_step_group():
    """An action may split across step groups; one catalog deeplink keeps the promise."""
    action = build_action(actionCategory.auto, groups=3)
    assert action.stepGroups[1].actionableDeeplink is None
    assert "auto_action_missing_deeplink" not in check(action)


def test_a_fabricated_uri_does_not_satisfy_the_rule():
    """A URI outside the catalog is not a destination. The action is reported both for
    the unknown URI and for failing the auto invariant, so neither can be papered over by
    inventing a link."""
    codes = check(
        build_action(actionCategory.auto, deeplink="bixby://invented/act/deadbeef")
    )
    assert "unknown_deeplink" in codes
    assert "auto_action_missing_deeplink" in codes


def test_an_empty_deeplink_string_does_not_count():
    action = build_action(actionCategory.auto)
    action.stepGroups[0].actionableDeeplink = Deeplink(
        deeplink="", description="nothing", message=""
    )
    assert "auto_action_missing_deeplink" in check(action)


# ------------------------------------------------- cached-plan revalidation


def test_a_legacy_cached_plan_fails_revalidation():
    """The cache re-checks every plan on read, so a plan stored before this rule existed
    is caught there rather than being served. It is then discarded and treated as a miss,
    which is what regenerates it under the current rules."""
    legacy = wrap(build_action(actionCategory.auto, deeplink=None))
    report = validate_plan(legacy, PERMITTED)

    assert not report.ok
    assert "auto_action_missing_deeplink" in {v.code for v in report.violations}


def test_the_same_plan_passes_once_the_action_is_downgraded():
    """The repair the pipeline performs: keep the grounded steps, change the category."""
    repaired = wrap(build_action(actionCategory.manual, deeplink=None))
    report = validate_plan(repaired, PERMITTED)

    assert report.ok, report.summary()
    # The steps survive the downgrade; only the promise about how to act changes.
    assert repaired.contexts[0].actions[0].stepGroups[0].steps


def test_a_plan_whose_auto_action_is_linked_passes_end_to_end():
    report = validate_plan(wrap(build_action(actionCategory.auto)), PERMITTED)
    assert report.ok, report.summary()


# --------------------------------------------------- the pipeline downgrade


class _Extracted:
    """The shape _build_action consumes, without running Phase 0-2."""

    def __init__(self, name, category, target_screen, steps=("Open Settings.",)):
        self.action_name = name
        self.category = category
        self.target_screen = target_screen
        self.steps = list(steps)
        self.source_text = "Open Settings and review the option."


@pytest.fixture
def pipeline():
    from app.pipeline.real.pipeline import RealPipeline

    return RealPipeline.__new__(RealPipeline)


@pytest.mark.parametrize(
    "name,target",
    [
        ("Email App Storage", "Email application storage settings"),
        ("Open Smart Switch App", "Smart Switch application"),
        ("Phone Aspect Ratio", "Phone aspect ratio display settings"),
    ],
)
def test_an_unresolvable_auto_action_is_downgraded_not_linked(
    pipeline, monkeypatch, name, target
):
    """The three benchmark failures, driven through the real construction path with
    resolution returning nothing. The action must come back manual with its steps, and
    with no deeplink attached."""
    monkeypatch.setattr(
        pipeline, "_resolve_verified_deeplink", lambda **kw: None, raising=False
    )

    built = pipeline._build_action(
        conn=None,
        extracted_action=_Extracted(name, actionCategory.auto, target),
    )

    assert built.category is actionCategory.manual
    assert built.stepGroups[0].actionableDeeplink is None
    assert built.stepGroups[0].steps, "the grounded steps must survive the downgrade"


def test_a_critical_action_is_not_downgraded_when_unresolvable(pipeline, monkeypatch):
    monkeypatch.setattr(
        pipeline, "_resolve_verified_deeplink", lambda **kw: None, raising=False
    )

    built = pipeline._build_action(
        conn=None,
        extracted_action=_Extracted(
            "Force Restart Device", actionCategory.critical, "Restart"
        ),
    )

    assert built.category is actionCategory.critical


def test_an_auto_action_keeps_its_category_when_a_match_is_verified(
    pipeline, monkeypatch
):
    from app.retrieval.resolver import CatalogMatch

    match = CatalogMatch(
        catalog_id=1,
        deeplink=REAL_DEEPLINK,
        description="Enables data backup to Samsung Cloud.",
        message="Enable Back up data",
        qna_description="",
        original_type="onURL",
        validation_deeplink=None,
        validation_key=None,
        validation_result_type=None,
        validation_condition=None,
        validation_value=None,
        cosine_similarity=0.9,
        vector_rank=1,
        bm25_rank=1,
        rrf_score=0.9,
    )
    monkeypatch.setattr(
        pipeline, "_resolve_verified_deeplink", lambda **kw: match, raising=False
    )

    built = pipeline._build_action(
        conn=None,
        extracted_action=_Extracted(
            "Back Up Phone Data", actionCategory.auto, "Backup settings"
        ),
    )

    assert built.category is actionCategory.auto
    assert built.stepGroups[0].actionableDeeplink.deeplink == REAL_DEEPLINK
