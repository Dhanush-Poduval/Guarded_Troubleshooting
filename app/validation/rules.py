"""Deterministic validation of troubleshooting plans.

Every rule here is enforced programmatically rather than by asking a model to behave. The
specification is explicit that prompt-level constraints are unreliable and that word
counts, URL stripping and category rules must be checked in the application layer.

This module is the gate in front of the cache: only a plan that passes is ever stored, and
a plan read back out of the cache is re-checked before being served, because the rules may
have tightened since it was written.

Nothing here touches the database or the network, so it is cheap and fully unit-testable.
"""

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

# Absolute prohibition on web URLs. Models inject support links from pretraining memory,
# so this is checked against every string that reaches the output, not just steps.
_URL_PATTERNS = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\bwww\.", re.IGNORECASE),
    re.compile(r"\[[^\]]*\]\([^)]*\)"),          # markdown link
    re.compile(r"\b[a-z0-9-]+\.(com|org|net|io|co)\b", re.IGNORECASE),
)

# "Follow these steps to perform this <Topic> Troubleshooting" or "<Topic> Configuration".
_GOAL_PATTERN = re.compile(
    r"^Follow these steps to perform this .+ (Troubleshooting|Configuration)$"
)

_WORD = re.compile(r"[A-Za-z0-9']+")

# Short words that stay lowercase inside a Title Case name.
_TITLE_CASE_EXCEPTIONS = frozenset(
    {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or",
     "the", "to", "with"}
)


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    path: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message} [{self.code}]"


@dataclass
class ValidationReport:
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def add(self, code: str, message: str, path: str) -> None:
        self.violations.append(Violation(code=code, message=message, path=path))

    def codes(self) -> set[str]:
        return {v.code for v in self.violations}

    def summary(self) -> str:
        if self.ok:
            return "valid"
        return "; ".join(str(v) for v in self.violations)


def words(text: str) -> list[str]:
    return _WORD.findall(text)


def contains_url(text: str) -> bool:
    return any(pattern.search(text) for pattern in _URL_PATTERNS)


def is_title_case(text: str) -> bool:
    tokens = words(text)
    if not tokens:
        return False
    for index, token in enumerate(tokens):
        if token.isupper():          # acronyms such as HD, QHD
            continue
        if index > 0 and token.lower() in _TITLE_CASE_EXCEPTIONS:
            continue
        if not token[0].isupper():
            return False
    return True


def is_sentence_case(text: str) -> bool:
    tokens = words(text)
    if not tokens:
        return False
    if not tokens[0][0].isupper():
        return False
    # Subsequent words lowercase, except acronyms.
    return all(tok.islower() or tok.isupper() for tok in tokens[1:])


def _check_strings_for_urls(report: ValidationReport, text: str, path: str) -> None:
    if contains_url(text):
        report.add("url_leak", "contains a web URL or markdown link", path)


def validate_action(
    action: Action,
    permitted_deeplinks: frozenset[str],
    report: ValidationReport,
    path: str,
    *,
    min_words: int = 5,
    max_words: int = 15,
) -> None:
    if not is_title_case(action.actionName):
        report.add("action_name_case", "actionName must be Title Case", f"{path}.actionName")
    _check_strings_for_urls(report, action.actionName, f"{path}.actionName")

    # Word count bounds come from settings; see Settings.description_max_words for why
    # the upper bound follows the official sample rather than the written rule.
    description_words = words(action.description)
    if not action.description.startswith("It will"):
        report.add(
            "description_prefix",
            'description must start with "It will"',
            f"{path}.description",
        )
    if not min_words <= len(description_words) <= max_words:
        report.add(
            "description_length",
            f"description must be {min_words} to {max_words} words, "
            f"got {len(description_words)}",
            f"{path}.description",
        )
    _check_strings_for_urls(report, action.description, f"{path}.description")

    if not action.stepGroups:
        report.add("no_step_groups", "action has no stepGroups", f"{path}.stepGroups")

    for group_index, group in enumerate(action.stepGroups):
        group_path = f"{path}.stepGroups[{group_index}]"

        if not group.steps:
            report.add("no_steps", "stepGroup has no steps", f"{group_path}.steps")
        for step_index, step in enumerate(group.steps):
            step_path = f"{group_path}.steps[{step_index}]"
            _check_strings_for_urls(report, step, step_path)
            if not step.strip():
                report.add("empty_step", "step is empty", step_path)

        link = group.actionableDeeplink
        if link is not None:
            # A manual action is a physical intervention and cannot open a screen.
            if action.category == actionCategory.manual:
                report.add(
                    "manual_with_deeplink",
                    "a manual action cannot carry an actionableDeeplink",
                    f"{group_path}.actionableDeeplink",
                )
            if link.deeplink not in permitted_deeplinks:
                report.add(
                    "unknown_deeplink",
                    f"{link.deeplink!r} is not in the catalog",
                    f"{group_path}.actionableDeeplink.deeplink",
                )
            _check_strings_for_urls(
                report, link.description, f"{group_path}.actionableDeeplink.description"
            )
            if link.message:
                _check_strings_for_urls(
                    report, link.message, f"{group_path}.actionableDeeplink.message"
                )


def validate_goal(
    goal: Goal,
    permitted_deeplinks: frozenset[str],
    report: ValidationReport,
    path: str,
    *,
    min_words: int = 5,
    max_words: int = 15,
) -> None:
    if not _GOAL_PATTERN.match(goal.goal):
        report.add(
            "goal_syntax",
            "goal must read 'Follow these steps to perform this <Topic> "
            "Troubleshooting' or '<Topic> Configuration'",
            f"{path}.goal",
        )
    _check_strings_for_urls(report, goal.goal, f"{path}.goal")

    title_words = words(goal.title)
    if not 2 <= len(title_words) <= 3:
        report.add(
            "title_length",
            f"title must be 2 to 3 words, got {len(title_words)}",
            f"{path}.title",
        )
    if not is_sentence_case(goal.title):
        report.add("title_case", "title must be sentence case", f"{path}.title")
    _check_strings_for_urls(report, goal.title, f"{path}.title")

    if not 0.0 <= goal.score <= 1.0:
        report.add(
            "score_range", f"score must be between 0.0 and 1.0, got {goal.score}",
            f"{path}.score",
        )

    if not goal.actions:
        report.add("no_actions", "goal has no actions", f"{path}.actions")

    for index, action in enumerate(goal.actions):
        validate_action(
            action, permitted_deeplinks, report, f"{path}.actions[{index}]",
            min_words=min_words, max_words=max_words,
        )

    # Destructive operations must come last, so no non-critical action may follow a
    # critical one.
    seen_critical = False
    for index, action in enumerate(goal.actions):
        if action.category == actionCategory.critical:
            seen_critical = True
        elif seen_critical:
            report.add(
                "critical_ordering",
                "a non-critical action follows a critical one; "
                "critical actions must be ordered last",
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
    """Validate a complete response.

    permitted_deeplinks is the set of URIs from the catalog. The reserved placeholder is
    added unless explicitly disallowed: it denotes a valid screen that simply is not
    indexed, so it is legitimate output rather than a hallucination.

    An empty contexts list is valid. It is the specified representation of "no viable
    solution found" and must not be treated as a failure.
    """
    permitted = frozenset(permitted_deeplinks)
    if allow_dummy_positive:
        permitted = permitted | {DUMMY_POSITIVE_DEEPLINK}

    if min_words is None or max_words is None:
        settings = get_settings()
        min_words = settings.description_min_words if min_words is None else min_words
        max_words = settings.description_max_words if max_words is None else max_words

    report = ValidationReport()
    for index, goal in enumerate(response.contexts):
        validate_goal(
            goal, permitted, report, f"contexts[{index}]",
            min_words=min_words, max_words=max_words,
        )
    return report


def validate_query_variations(variations: list[str]) -> ValidationReport:
    """8 to 10 distinct paraphrases, none of which may leak a URL."""
    report = ValidationReport()
    if not 8 <= len(variations) <= 10:
        report.add(
            "variation_count",
            f"expected 8 to 10 query variations, got {len(variations)}",
            "query_variations",
        )
    seen = {v.strip().lower() for v in variations}
    if len(seen) != len(variations):
        report.add(
            "variation_duplicates", "query variations must be distinct", "query_variations"
        )
    for index, variation in enumerate(variations):
        _check_strings_for_urls(report, variation, f"query_variations[{index}]")
    return report
