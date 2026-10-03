"""The deterministic verification core.

These tests need no database, no device and no model call: the comparator is a pure
function, which is the point of keeping the verification decision out of the language
model. Everything the resolution loop later claims rests on what is asserted here.
"""

from __future__ import annotations

import pytest

from app.contract.schema import Condition, ResultTypes, ValidationDeepLink
from app.verification.adapter import AdapterReading, UnavailableAdapter
from app.verification.comparator import (
    EvidenceSource,
    ExpectedContract,
    Observation,
    ReasonCode,
    VerificationStatus,
    compare,
    contract_from,
)
from app.verification.fake import FakeTrustedAdapter

DEEPLINK = "bixby://masked/val/266037d0c5"


def contract(result_type, condition, value, key="Back up data"):
    return ExpectedContract(
        deeplink=DEEPLINK,
        key=key,
        result_type=result_type,
        condition=condition,
        value=value,
    )


def observe(value, key="Back up data", source=EvidenceSource.TRUSTED_ADAPTER):
    return Observation(key=key, value=value, source=source)


# ---------------------------------------------------------------- contract shape

def test_a_complete_validation_deeplink_yields_a_contract():
    vd = ValidationDeepLink(
        deeplink=DEEPLINK,
        key="Back up data",
        resultType=ResultTypes.boolean,
        condition=Condition.equal,
        value="True",
    )
    built = contract_from(vd)
    assert built is not None
    assert built.key == "Back up data"
    assert built.describe() == "Back up data = True"


def test_no_validation_deeplink_has_no_contract():
    assert contract_from(None) is None


@pytest.mark.parametrize(
    "missing",
    ["resultType", "condition", "value"],
    ids=["no_result_type", "no_condition", "no_value"],
)
def test_an_incomplete_contract_is_not_usable(missing):
    """Most catalog entries carry only a deeplink and a key. That is not an error, it
    means automatic verification is unavailable for the step rather than guessable."""
    fields = {
        "deeplink": DEEPLINK,
        "key": "Back up data",
        "resultType": ResultTypes.boolean,
        "condition": Condition.equal,
        "value": "True",
    }
    fields[missing] = None
    assert contract_from(ValidationDeepLink(**fields)) is None


def test_a_blank_contract_value_is_not_usable():
    assert contract_from(
        ValidationDeepLink(
            deeplink=DEEPLINK,
            key="Back up data",
            resultType=ResultTypes.boolean,
            condition=Condition.equal,
            value="   ",
        )
    ) is None


# ------------------------------------------------------------------- booleans

@pytest.mark.parametrize("observed", ["True", "true", "YES", "on", "1", "enabled"])
def test_boolean_true_literals_are_accepted(observed):
    outcome = compare(contract(ResultTypes.boolean, Condition.equal, "True"),
                      observe(observed))
    assert outcome.status is VerificationStatus.SYSTEM_VERIFIED


@pytest.mark.parametrize("observed", ["False", "no", "OFF", "0", "disabled"])
def test_boolean_false_is_a_failure_against_an_expected_true(observed):
    outcome = compare(contract(ResultTypes.boolean, Condition.equal, "True"),
                      observe(observed))
    assert outcome.status is VerificationStatus.VERIFICATION_FAILED
    assert outcome.reason is ReasonCode.VALUE_MISMATCH


@pytest.mark.parametrize("observed", ["maybe", "2", "", "  ", "truthy", "sure"])
def test_a_non_boolean_reading_is_refused_rather_than_coerced(observed):
    """"maybe" must not become False. Coercing unparseable text is how a verifier starts
    reporting outcomes it has not earned."""
    outcome = compare(contract(ResultTypes.boolean, Condition.equal, "True"),
                      observe(observed))
    assert outcome.status is VerificationStatus.INCONCLUSIVE
    assert outcome.reason in {
        ReasonCode.OBSERVED_NOT_BOOLEAN,
        ReasonCode.EMPTY_OBSERVATION,
    }


def test_a_malformed_contract_value_is_inconclusive_not_failed():
    """The device may be fine; it is the catalog entry that cannot be read."""
    outcome = compare(contract(ResultTypes.boolean, Condition.equal, "sometimes"),
                      observe("True"))
    assert outcome.status is VerificationStatus.INCONCLUSIVE
    assert outcome.reason is ReasonCode.EXPECTED_NOT_BOOLEAN


# ------------------------------------------------------------------- integers

@pytest.mark.parametrize(
    "observed,condition,expected,status",
    [
        ("5", Condition.equal, "5", VerificationStatus.SYSTEM_VERIFIED),
        ("6", Condition.equal, "5", VerificationStatus.VERIFICATION_FAILED),
        ("6", Condition.greater, "5", VerificationStatus.SYSTEM_VERIFIED),
        ("5", Condition.greater, "5", VerificationStatus.VERIFICATION_FAILED),
        ("4", Condition.less, "5", VerificationStatus.SYSTEM_VERIFIED),
        ("5", Condition.less, "5", VerificationStatus.VERIFICATION_FAILED),
        ("-3", Condition.less, "0", VerificationStatus.SYSTEM_VERIFIED),
    ],
)
def test_integer_comparisons(observed, condition, expected, status):
    outcome = compare(contract(ResultTypes.intNum, condition, expected),
                      observe(observed))
    assert outcome.status is status


@pytest.mark.parametrize("observed", ["1.5", "abc", "1_0", "0x10", "", "five"])
def test_a_non_integer_reading_is_inconclusive(observed):
    outcome = compare(contract(ResultTypes.intNum, Condition.equal, "1"),
                      observe(observed))
    assert outcome.status is VerificationStatus.INCONCLUSIVE
    assert outcome.reason in {
        ReasonCode.OBSERVED_NOT_INTEGER,
        ReasonCode.EMPTY_OBSERVATION,
    }


# --------------------------------------------------------------------- floats

@pytest.mark.parametrize(
    "observed,condition,expected,status",
    [
        ("0.5", Condition.equal, "0.5", VerificationStatus.SYSTEM_VERIFIED),
        ("0.75", Condition.greater, "0.5", VerificationStatus.SYSTEM_VERIFIED),
        ("0.25", Condition.less, "0.5", VerificationStatus.SYSTEM_VERIFIED),
        ("0.25", Condition.greater, "0.5", VerificationStatus.VERIFICATION_FAILED),
        ("3", Condition.greater, "2.5", VerificationStatus.SYSTEM_VERIFIED),
    ],
)
def test_float_comparisons(observed, condition, expected, status):
    outcome = compare(contract(ResultTypes.floatNum, condition, expected),
                      observe(observed))
    assert outcome.status is status


@pytest.mark.parametrize("observed", ["nan", "inf", "-inf", "abc", ""])
def test_non_finite_and_unparseable_floats_are_inconclusive(observed):
    """nan compares false against everything and inf compares true against almost
    everything; neither is a meaningful device reading."""
    outcome = compare(contract(ResultTypes.floatNum, Condition.equal, "0.5"),
                      observe(observed))
    assert outcome.status is VerificationStatus.INCONCLUSIVE


# -------------------------------------------------------------------- strings

def test_string_equality_ignores_case_and_surrounding_space():
    outcome = compare(contract(ResultTypes.string, Condition.equal, "On"),
                      observe("  on "))
    assert outcome.status is VerificationStatus.SYSTEM_VERIFIED


def test_string_inequality_fails():
    outcome = compare(contract(ResultTypes.string, Condition.equal, "On"),
                      observe("Off"))
    assert outcome.status is VerificationStatus.VERIFICATION_FAILED


@pytest.mark.parametrize("condition", [Condition.greater, Condition.less])
@pytest.mark.parametrize("result_type", [ResultTypes.string, ResultTypes.boolean])
def test_ordering_is_rejected_for_unorderable_types(result_type, condition):
    """Ordering two booleans or two arbitrary strings has no agreed meaning, so the
    comparison is refused rather than given one."""
    outcome = compare(contract(result_type, condition, "On"), observe("On"))
    assert outcome.status is VerificationStatus.INCONCLUSIVE
    assert outcome.reason is ReasonCode.UNSUPPORTED_COMPARISON


# ------------------------------------------------------------------ key check

def test_a_reading_for_a_different_key_proves_nothing():
    outcome = compare(contract(ResultTypes.boolean, Condition.equal, "True",
                               key="Back up data"),
                      observe("True", key="Adaptive brightness"))
    assert outcome.status is VerificationStatus.INCONCLUSIVE
    assert outcome.reason is ReasonCode.KEY_MISMATCH


# ------------------------------------------------------- the evidence boundary

def test_client_reported_evidence_never_produces_system_verified():
    """The comparison holds, but the server did not read the value. Attaching the
    server's authority to the client's claim is the one thing this must not do."""
    outcome = compare(
        contract(ResultTypes.boolean, Condition.equal, "True"),
        observe("True", source=EvidenceSource.CLIENT_REPORTED),
    )
    assert outcome.status is VerificationStatus.INCONCLUSIVE
    assert outcome.reason is ReasonCode.MATCHED
    assert "reported by the client" in outcome.detail


def test_client_reported_evidence_can_still_fail_a_comparison():
    """Demotion applies to success only. A client-reported value that contradicts the
    contract is still a genuine failure signal worth acting on."""
    outcome = compare(
        contract(ResultTypes.boolean, Condition.equal, "True"),
        observe("False", source=EvidenceSource.CLIENT_REPORTED),
    )
    assert outcome.status is VerificationStatus.VERIFICATION_FAILED


def test_trusted_evidence_is_the_only_route_to_system_verified():
    sources = {
        EvidenceSource.TRUSTED_ADAPTER: VerificationStatus.SYSTEM_VERIFIED,
        EvidenceSource.CLIENT_REPORTED: VerificationStatus.INCONCLUSIVE,
        EvidenceSource.USER_CONFIRMATION: VerificationStatus.INCONCLUSIVE,
    }
    for source, expected in sources.items():
        outcome = compare(
            contract(ResultTypes.boolean, Condition.equal, "True"),
            observe("True", source=source),
        )
        assert outcome.status is expected, source


# ------------------------------------------------------------------- adapters

async def test_the_production_adapter_reads_nothing_and_says_so():
    """This deployment has no channel to a handset, so "unavailable" is the truthful
    answer rather than a stub to be filled in."""
    reading = await UnavailableAdapter().read_state(deeplink=DEEPLINK, key="Back up data")
    assert reading.available is False
    assert reading.value is None
    assert reading.reason is ReasonCode.ADAPTER_UNAVAILABLE


async def test_the_fake_adapter_returns_only_what_was_seeded():
    adapter = FakeTrustedAdapter().seed(DEEPLINK, "Back up data", "True")
    hit = await adapter.read_state(deeplink=DEEPLINK, key="Back up data")
    assert hit == AdapterReading.read("True")

    miss = await adapter.read_state(deeplink=DEEPLINK, key="Adaptive brightness")
    assert miss.available is False, "an unseeded key must not return a default value"
