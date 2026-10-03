"""Deterministic comparison of an observed device value against a validation contract.

This module is the whole of the verification decision. No language model participates:
given the same contract and the same observed text it always returns the same outcome,
which is what makes a verification result evidence rather than an opinion.

Three ideas shape it.

A contract is only usable when it is complete. The catalog carries a validation deeplink
for many entries but only a subset also carry resultType, condition and value. Comparing
against a partial contract would mean inventing the missing half, so an incomplete
contract yields VERIFICATION_UNAVAILABLE rather than a guess.

Coercion is refused rather than attempted. "yes" is not an integer, "1.5" is not an
integer, and "maybe" is not a boolean. Every one of those is INCONCLUSIVE with a reason
code, because quietly turning unparseable text into a value is how a verification system
starts reporting results it has not earned.

Not every condition applies to every type. Ordering two booleans or two arbitrary strings
has no agreed meaning, so `greater` and `less` are rejected for those types instead of
being given one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.contract.schema import Condition, ResultTypes, ValidationDeepLink


class VerificationStatus(str, Enum):
    """The outcome vocabulary shared by the comparator, the session and the API."""

    SYSTEM_VERIFIED = "system_verified"
    USER_CONFIRMED = "user_confirmed"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_UNAVAILABLE = "verification_unavailable"
    INCONCLUSIVE = "inconclusive"
    PENDING = "pending"


class EvidenceSource(str, Enum):
    """Where an observed value came from.

    The distinction is a security boundary, not a label. Only TRUSTED_ADAPTER evidence is
    obtained by the server itself, so only TRUSTED_ADAPTER evidence can produce
    SYSTEM_VERIFIED. A client cannot nominate its own evidence as trusted: the source is
    assigned by the server according to which code path produced the reading.
    """

    TRUSTED_ADAPTER = "trusted_adapter"
    CLIENT_REPORTED = "client_reported"
    USER_CONFIRMATION = "user_confirmation"


class ReasonCode(str, Enum):
    """Why an outcome was reached. Stored, surfaced, and asserted on in tests."""

    MATCHED = "matched"
    VALUE_MISMATCH = "value_mismatch"

    NO_VALIDATION_DEEPLINK = "no_validation_deeplink"
    INCOMPLETE_CONTRACT = "incomplete_contract"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"

    KEY_MISMATCH = "key_mismatch"
    OBSERVED_NOT_BOOLEAN = "observed_not_boolean"
    OBSERVED_NOT_INTEGER = "observed_not_integer"
    OBSERVED_NOT_FLOAT = "observed_not_float"
    EXPECTED_NOT_BOOLEAN = "expected_not_boolean"
    EXPECTED_NOT_INTEGER = "expected_not_integer"
    EXPECTED_NOT_FLOAT = "expected_not_float"
    UNSUPPORTED_COMPARISON = "unsupported_comparison"
    UNSUPPORTED_RESULT_TYPE = "unsupported_result_type"
    EMPTY_OBSERVATION = "empty_observation"

    USER_CONFIRMED_RESOLVED = "user_confirmed_resolved"
    USER_REPORTED_UNRESOLVED = "user_reported_unresolved"


# Boolean literals accepted on both sides of the comparison. Deliberately a closed set:
# anything outside it is refused rather than coerced, so "maybe" and "2" never silently
# become False.
_TRUE_LITERALS = frozenset({"true", "yes", "on", "1", "enabled"})
_FALSE_LITERALS = frozenset({"false", "no", "off", "0", "disabled"})

_ORDERABLE = frozenset({ResultTypes.intNum, ResultTypes.floatNum})


@dataclass(frozen=True)
class ExpectedContract:
    """A validation deeplink that carries everything needed to decide an outcome."""

    deeplink: str
    key: str
    result_type: ResultTypes
    condition: Condition
    value: str

    def describe(self) -> str:
        """Human-readable expectation, for the receipt and the console."""
        symbol = {
            Condition.equal: "=",
            Condition.greater: ">",
            Condition.less: "<",
        }[self.condition]
        return f"{self.key} {symbol} {self.value}"

    def as_dict(self) -> dict[str, str]:
        return {
            "deeplink": self.deeplink,
            "key": self.key,
            "resultType": self.result_type.value,
            "condition": self.condition.value,
            "value": self.value,
        }


@dataclass(frozen=True)
class Observation:
    """One reading of device state, with the provenance that decides how far it counts."""

    key: str
    value: str
    source: EvidenceSource


@dataclass(frozen=True)
class ComparisonOutcome:
    status: VerificationStatus
    reason: ReasonCode
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is VerificationStatus.SYSTEM_VERIFIED


def contract_from(validation: ValidationDeepLink | None) -> ExpectedContract | None:
    """Return a usable contract, or None when the catalog entry is only partial.

    A missing field is not an error in the catalog: most entries legitimately carry only
    a deeplink and a key. It simply means automatic verification is not available for
    that step, which the caller reports as VERIFICATION_UNAVAILABLE.
    """
    if validation is None:
        return None
    if not validation.deeplink or not validation.key:
        return None
    if validation.resultType is None or validation.condition is None:
        return None
    if validation.value is None or str(validation.value).strip() == "":
        return None

    return ExpectedContract(
        deeplink=validation.deeplink,
        key=validation.key,
        result_type=validation.resultType,
        condition=validation.condition,
        value=str(validation.value),
    )


def _parse_boolean(text: str) -> bool | None:
    lowered = text.strip().casefold()
    if lowered in _TRUE_LITERALS:
        return True
    if lowered in _FALSE_LITERALS:
        return False
    return None


def _parse_integer(text: str) -> int | None:
    candidate = text.strip()
    if not candidate:
        return None
    # int() accepts underscores ("1_0") and unicode digits; neither belongs in a device
    # readback, so the shape is checked before conversion.
    sign = ""
    if candidate[0] in "+-":
        sign, candidate = candidate[0], candidate[1:]
    if not candidate.isascii() or not candidate.isdigit():
        return None
    return int(sign + candidate)


def _parse_float(text: str) -> float | None:
    candidate = text.strip()
    if not candidate:
        return None
    try:
        parsed = float(candidate)
    except (TypeError, ValueError):
        return None
    # nan compares false against everything and inf compares true against almost
    # everything. Neither is a meaningful device reading, so both are refused.
    if not math.isfinite(parsed):
        return None
    return parsed


def _inconclusive(reason: ReasonCode, detail: str = "") -> ComparisonOutcome:
    return ComparisonOutcome(VerificationStatus.INCONCLUSIVE, reason, detail)


def compare(contract: ExpectedContract, observation: Observation) -> ComparisonOutcome:
    """Decide one verification, deterministically.

    SYSTEM_VERIFIED is returned only for a satisfied comparison backed by trusted
    evidence. The same comparison backed by client-reported evidence is reported as
    INCONCLUSIVE, because the server did not obtain the value itself and cannot vouch for
    it. That demotion happens here rather than at the edge so that no caller can skip it.
    """
    if observation.key.strip() != contract.key.strip():
        return _inconclusive(
            ReasonCode.KEY_MISMATCH,
            f"observed key {observation.key!r} is not the expected {contract.key!r}",
        )

    if observation.value.strip() == "":
        return _inconclusive(ReasonCode.EMPTY_OBSERVATION, "no value was observed")

    result_type = contract.result_type
    condition = contract.condition

    if result_type not in _ORDERABLE and condition is not Condition.equal:
        return _inconclusive(
            ReasonCode.UNSUPPORTED_COMPARISON,
            f"{condition.value} has no defined meaning for {result_type.value}",
        )

    if result_type is ResultTypes.boolean:
        expected = _parse_boolean(contract.value)
        if expected is None:
            return _inconclusive(
                ReasonCode.EXPECTED_NOT_BOOLEAN,
                f"contract value {contract.value!r} is not a boolean literal",
            )
        actual = _parse_boolean(observation.value)
        if actual is None:
            return _inconclusive(
                ReasonCode.OBSERVED_NOT_BOOLEAN,
                f"observed {observation.value!r} is not a boolean literal",
            )
        satisfied = actual == expected

    elif result_type is ResultTypes.intNum:
        expected_int = _parse_integer(contract.value)
        if expected_int is None:
            return _inconclusive(
                ReasonCode.EXPECTED_NOT_INTEGER,
                f"contract value {contract.value!r} is not an integer",
            )
        actual_int = _parse_integer(observation.value)
        if actual_int is None:
            return _inconclusive(
                ReasonCode.OBSERVED_NOT_INTEGER,
                f"observed {observation.value!r} is not an integer",
            )
        satisfied = _ordered(actual_int, expected_int, condition)

    elif result_type is ResultTypes.floatNum:
        expected_float = _parse_float(contract.value)
        if expected_float is None:
            return _inconclusive(
                ReasonCode.EXPECTED_NOT_FLOAT,
                f"contract value {contract.value!r} is not a finite number",
            )
        actual_float = _parse_float(observation.value)
        if actual_float is None:
            return _inconclusive(
                ReasonCode.OBSERVED_NOT_FLOAT,
                f"observed {observation.value!r} is not a finite number",
            )
        satisfied = _ordered(actual_float, expected_float, condition)

    elif result_type is ResultTypes.string:
        # Compared case-insensitively on the trimmed text: a device reporting "On" and a
        # catalog recording "on" describe the same state, and the alternative is a
        # verification that fails on capitalisation.
        satisfied = observation.value.strip().casefold() == contract.value.strip().casefold()

    else:
        return _inconclusive(
            ReasonCode.UNSUPPORTED_RESULT_TYPE,
            f"{result_type!r} has no comparison rule",
        )

    if not satisfied:
        return ComparisonOutcome(
            VerificationStatus.VERIFICATION_FAILED,
            ReasonCode.VALUE_MISMATCH,
            f"expected {contract.describe()}, observed {observation.value!r}",
        )

    if observation.source is not EvidenceSource.TRUSTED_ADAPTER:
        # The comparison holds, but the server did not read the value. Reporting this as
        # system_verified would attach the server's authority to the client's claim, so
        # the match is recorded and the status stops short of verified.
        return _inconclusive(
            ReasonCode.MATCHED,
            "the expectation is satisfied, but the value was reported by the client "
            "rather than read by the server",
        )

    return ComparisonOutcome(
        VerificationStatus.SYSTEM_VERIFIED,
        ReasonCode.MATCHED,
        f"expected {contract.describe()}, observed {observation.value!r}",
    )


def _ordered(actual: Any, expected: Any, condition: Condition) -> bool:
    if condition is Condition.equal:
        return actual == expected
    if condition is Condition.greater:
        return actual > expected
    return actual < expected
