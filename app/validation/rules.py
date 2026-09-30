"""Deterministic validation of troubleshooting plans."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from app.config import get_settings
from app.contract.schema import (
    DUMMY_POSITIVE_DEEPLINK,
    Action,
    ContextDeeplinkResponse,
    Goal,
    actionCategory,
)


_URL_PATTERNS = (
    re.compile(
        r"https?://",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwww\.",
        re.IGNORECASE,
    ),
    re.compile(
        r"\[[^\]]*\]\([^)]*\)"
    ),
    re.compile(
        r"\b[a-z0-9-]+\."
        r"(com|org|net|io|co)\b",
        re.IGNORECASE,
    ),
)


_GOAL_PATTERN = re.compile(
    r"^Follow these steps to perform this "
    r".+ "
    r"(Troubleshooting|Configuration)$"
)


_WORD = re.compile(
    r"[A-Za-z0-9']+"
)


_TITLE_CASE_EXCEPTIONS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)


@dataclass(
    frozen=True
)
class Violation:
    code: str
    message: str
    path: str

    def __str__(
        self,
    ) -> str:
        return (
            f"{self.path}: "
            f"{self.message} "
            f"[{self.code}]"
        )


@dataclass
class ValidationReport:
    violations: list[
        Violation
    ] = field(
        default_factory=list
    )

    @property
    def ok(
        self,
    ) -> bool:
        return not self.violations

    def add(
        self,
        code: str,
        message: str,
        path: str,
    ) -> None:
        self.violations.append(
            Violation(
                code=code,
                message=message,
                path=path,
            )
        )

    def codes(
        self,
    ) -> set[str]:
        return {
            violation.code
            for violation
            in self.violations
        }

    def summary(
        self,
    ) -> str:
        if self.ok:
            return "valid"

        return "; ".join(
            str(violation)
            for violation
            in self.violations
        )


def words(
    text: str,
) -> list[str]:
    return _WORD.findall(
        text
    )


def contains_url(
    text: str,
) -> bool:
    return any(
        pattern.search(
            text
        )
        for pattern
        in _URL_PATTERNS
    )


def is_title_case(
    text: str,
) -> bool:

    tokens = words(
        text
    )

    if not tokens:
        return False

    for index, token in enumerate(
        tokens
    ):

        if token.isupper():
            continue

        if (
            index > 0
            and token.lower()
            in _TITLE_CASE_EXCEPTIONS
        ):
            continue

        if not token[0].isupper():
            return False

    return True


def is_sentence_case(
    text: str,
) -> bool:
    """Sentence case, without rejecting embedded product names.

    Titles legitimately contain proper nouns: "Smart Switch failure" and "Gmail blank
    screen" are sentence case even though a word other than the first is capitalised. A
    rule that demands every later word be lowercase rejects them.

    Testing only the first character is the opposite failure: it accepts "Battery Fast
    Drain", which is Title Case, and the rule then catches nothing. So the test is that
    the first word is capitalised and the title is not entirely capitalised, which
    separates a sentence containing a product name from a Title Cased string.
    """

    tokens = words(
        text
    )

    if not tokens:
        return False

    if not tokens[0][0].isupper():
        return False

    if len(tokens) == 1:
        return True

    # Acronyms may be fully upper (QHD, HD); they do not make a title Title Case.
    considered = [
        token
        for token in tokens[1:]
        if not token.isupper()
    ]

    if not considered:
        return True

    return any(
        token[0].islower()
        for token in considered
    )


def _check_strings_for_urls(
    report: ValidationReport,
    text: str,
    path: str,
) -> None:

    if contains_url(
        text
    ):
        report.add(
            "url_leak",
            "contains a web URL or markdown link",
            path,
        )


def validate_action(
    action: Action,
    permitted_deeplinks: frozenset[str],
    report: ValidationReport,
    path: str,
    *,
    min_words: int = 5,
    max_words: int = 15,
) -> None:

    if not is_title_case(
        action.actionName
    ):
        report.add(
            "action_name_case",
            "actionName must be Title Case",
            f"{path}.actionName",
        )

    _check_strings_for_urls(
        report,
        action.actionName,
        f"{path}.actionName",
    )

    description_words = words(
        action.description
    )

    if not action.description.startswith(
        "It will"
    ):
        report.add(
            "description_prefix",
            'description must start with "It will"',
            f"{path}.description",
        )

    if not (
        min_words
        <= len(description_words)
        <= max_words
    ):
        report.add(
            "description_length",
            (
                "description must be "
                f"{min_words} to "
                f"{max_words} words, "
                f"got "
                f"{len(description_words)}"
            ),
            f"{path}.description",
        )

    _check_strings_for_urls(
        report,
        action.description,
        f"{path}.description",
    )

    if not action.stepGroups:
        report.add(
            "no_step_groups",
            "action has no stepGroups",
            f"{path}.stepGroups",
        )

    for group_index, group in enumerate(
        action.stepGroups
    ):

        group_path = (
            f"{path}."
            f"stepGroups[{group_index}]"
        )

        if not group.steps:
            report.add(
                "no_steps",
                "stepGroup has no steps",
                f"{group_path}.steps",
            )

        for step_index, step in enumerate(
            group.steps
        ):

            step_path = (
                f"{group_path}."
                f"steps[{step_index}]"
            )

            if not step.strip():
                report.add(
                    "empty_step",
                    "step is empty",
                    step_path,
                )

            _check_strings_for_urls(
                report,
                step,
                step_path,
            )

        actionable = (
            group.actionableDeeplink
        )

        if actionable is not None:

            if (
                action.category
                == actionCategory.manual
            ):
                report.add(
                    "manual_with_deeplink",
                    (
                        "a manual action cannot "
                        "carry an actionableDeeplink"
                    ),
                    (
                        f"{group_path}."
                        "actionableDeeplink"
                    ),
                )

            if (
                actionable.deeplink
                not in permitted_deeplinks
            ):
                report.add(
                    "unknown_deeplink",
                    (
                        f"{actionable.deeplink!r} "
                        "is not in the catalog"
                    ),
                    (
                        f"{group_path}."
                        "actionableDeeplink."
                        "deeplink"
                    ),
                )

            _check_strings_for_urls(
                report,
                actionable.description,
                (
                    f"{group_path}."
                    "actionableDeeplink."
                    "description"
                ),
            )

            if actionable.message:
                _check_strings_for_urls(
                    report,
                    actionable.message,
                    (
                        f"{group_path}."
                        "actionableDeeplink."
                        "message"
                    ),
                )

        validation = (
            group.validationDeeplink
        )

        if validation is not None:
            if (
                validation.deeplink
                not in permitted_deeplinks
            ):
                report.add(
                    "unknown_validation_deeplink",
                    (
                        f"{validation.deeplink!r} "
                        "is not in the catalog"
                    ),
                    (
                        f"{group_path}."
                        "validationDeeplink."
                        "deeplink"
                    ),
                )


def validate_goal(
    goal: Goal,
    permitted_deeplinks: frozenset[str],
    report: ValidationReport,
    path: str,
    *,
    min_words: int = 5,
    max_words: int = 7,
) -> None:

    if not _GOAL_PATTERN.match(
        goal.goal
    ):
        report.add(
            "goal_syntax",
            (
                "goal must read "
                "'Follow these steps to perform this "
                "<Topic> Troubleshooting' or "
                "'<Topic> Configuration'"
            ),
            f"{path}.goal",
        )

    _check_strings_for_urls(
        report,
        goal.goal,
        f"{path}.goal",
    )

    title_words = words(
        goal.title
    )

    # The specification states 2 to 3 words, and the official sample_output.json title
    # ("Screen display damage") is 3. An earlier edit widened this to 2-10 citing the
    # specification, which does not say that; the wider bound reports compliance the
    # evaluation would not credit.
    if not (
        2
        <= len(title_words)
        <= 3
    ):
        report.add(
            "title_length",
            (
                "title must be 2 to 3 words, "
                f"got {len(title_words)}"
            ),
            f"{path}.title",
        )

    if not is_sentence_case(
        goal.title
    ):
        report.add(
            "title_case",
            "title must be sentence case",
            f"{path}.title",
        )

    _check_strings_for_urls(
        report,
        goal.title,
        f"{path}.title",
    )

    if not (
        0.0
        <= goal.score
        <= 1.0
    ):
        report.add(
            "score_range",
            (
                "score must be between "
                "0.0 and 1.0, "
                f"got {goal.score}"
            ),
            f"{path}.score",
        )

    if not goal.actions:
        report.add(
            "no_actions",
            "goal has no actions",
            f"{path}.actions",
        )

    for index, action in enumerate(
        goal.actions
    ):
        validate_action(
            action,
            permitted_deeplinks,
            report,
            f"{path}.actions[{index}]",
            min_words=min_words,
            max_words=max_words,
        )

    # Critical/disruptive actions must be last.
    seen_critical = False

    for index, action in enumerate(
        goal.actions
    ):

        if (
            action.category
            == actionCategory.critical
        ):
            seen_critical = True

        elif seen_critical:
            report.add(
                "critical_ordering",
                (
                    "a non-critical action follows "
                    "a critical one; critical actions "
                    "must be ordered last"
                ),
                f"{path}.actions[{index}]",
            )

            break


def validate_plan(
    response: ContextDeeplinkResponse,
    permitted_deeplinks: Iterable[str],
    *,
    allow_dummy_positive: bool = True,
    min_words: int | None = None,
    max_words: int | None = None,
) -> ValidationReport:
    """Validate complete troubleshooting output."""

    permitted = frozenset(
        permitted_deeplinks
    )

    if allow_dummy_positive:
        permitted = (
            permitted
            | {
                DUMMY_POSITIVE_DEEPLINK
            }
        )

    if (
        min_words is None
        or max_words is None
    ):
        settings = get_settings()

        if min_words is None:
            min_words = (
                settings.description_min_words
            )

        if max_words is None:
            max_words = (
                settings.description_max_words
            )

    report = ValidationReport()

    for index, goal in enumerate(
        response.contexts
    ):
        validate_goal(
            goal,
            permitted,
            report,
            f"contexts[{index}]",
            min_words=min_words,
            max_words=max_words,
        )

    return report


def validate_query_variations(
    variations: list[str],
) -> ValidationReport:
    """Check the paraphrase set the pipeline produces for a query.

    The specification requires 8 to 10 distinct paraphrases per query. They are not
    decoration: each one becomes a searchable phrasing of the cached plan, so a set that
    is too small narrows what the semantic cache can match, and duplicates inflate the
    count without adding reach.

    Restored after being dropped in the Phase 0-2 merge; without it this rule was not
    enforced anywhere, and `scripts/benchmark.py` could not import.
    """
    report = ValidationReport()

    if not 8 <= len(variations) <= 10:
        report.add(
            "variation_count",
            f"expected 8 to 10 query variations, got {len(variations)}",
            "query_variations",
        )

    seen = {
        variation.strip().lower()
        for variation in variations
    }
    if len(seen) != len(variations):
        report.add(
            "variation_duplicates",
            "query variations must be distinct",
            "query_variations",
        )

    for index, variation in enumerate(variations):
        _check_strings_for_urls(
            report,
            variation,
            f"query_variations[{index}]",
        )

    return report
