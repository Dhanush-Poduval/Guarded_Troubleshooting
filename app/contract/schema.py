"""The input/output data contract.

RECONSTRUCTED FROM THE PROBLEM STATEMENT. The official dataset ships its own schema.py
defining this contract. This file is a transcription of Appendix A, made so that Phase 3
and Phase 4 could be built before that file was available.

When the official schema.py arrives, replace this module with it and delete this notice.
Field names, casing and defaults below are copied verbatim from the specification,
including the lowercase `actionCategory` class name and the camelCase fields, so that a
swap is a drop-in rather than a rename exercise.

Two details are easy to get wrong and are called out here because the specification is
explicit about both:
  * actionableDeeplink belongs to StepGroup, not to Action.
  * the top level response object is ContextDeeplinkResponse, whose only field is
    `contexts`, a list of Goal.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel


class BaseDeeplink(BaseModel):
    deeplink: str


class Deeplink(BaseDeeplink):
    description: str
    message: Optional[str] = ""
    classes: Optional[Dict[str, str]] = None
    originalType: Optional[str] = None


class Condition(str, Enum):
    greater = "greater"
    equal = "equal"
    less = "less"


class ResultTypes(str, Enum):
    boolean = "boolean"
    intNum = "integer"
    string = "str"
    floatNum = "float"


class actionCategory(str, Enum):  # noqa: N801 - name fixed by the specification
    auto = "auto"
    manual = "manual"
    critical = "critical"


class ValidationDeepLink(BaseDeeplink):
    key: str
    resultType: Optional[ResultTypes] = None
    condition: Optional[Condition] = None
    value: Optional[str] = None


class StepGroup(BaseModel):
    steps: List[str]
    validationDeeplink: Optional[ValidationDeepLink] = None
    actionableDeeplink: Optional[Deeplink] = None


class Action(BaseModel):
    actionName: str
    description: str
    stepGroups: List[StepGroup]
    category: Optional[actionCategory] = actionCategory.manual


class Goal(BaseModel):
    goal: str
    title: str
    actions: List[Action]
    score: float


class ContextDeeplinkResponse(BaseModel):
    """RAG response containing a list of Goal objects."""

    contexts: List[Goal] = []


# The reserved placeholder for a valid Settings screen that is not indexed in the
# catalog. It is a legitimate value even though it will never appear in deeplinks.json,
# so validation must permit it explicitly.
DUMMY_POSITIVE_DEEPLINK = "bixby://dummy_positive"

# The two fallback reasons the specification names.
FALLBACK_NO_MATCH = "no_match"
FALLBACK_NO_SIIS_CONTEXT = "no_siis_context"
