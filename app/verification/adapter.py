"""The device-verification boundary.

Reading the state of a Galaxy device is not something this service can do. The catalog
ships masked `bixby://` URIs whose targets are opaque, and the console is a desktop web
page with no channel to the handset. That is a fact about the deployment, not a gap to be
papered over, so the boundary is explicit:

  * A DeviceVerificationAdapter is the only thing permitted to produce trusted evidence.
  * The adapter wired in production is UnavailableAdapter, which reads nothing and says
    so. Every automatic verification therefore reports VERIFICATION_UNAVAILABLE.
  * A real adapter can be dropped in later without touching the comparator, the session
    machine or the API, because they all depend on this interface rather than on a device.

The alternative — pretending the page can read the device, or letting the client declare
its own readings trusted — would make `system_verified` meaningless, which is the one
status in the vocabulary that has to mean something.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.verification.comparator import ReasonCode


@dataclass(frozen=True)
class AdapterReading:
    """What an adapter returned, or why it returned nothing."""

    available: bool
    value: str | None = None
    reason: ReasonCode | None = None
    detail: str = ""

    @classmethod
    def unavailable(cls, detail: str) -> "AdapterReading":
        return cls(
            available=False,
            reason=ReasonCode.ADAPTER_UNAVAILABLE,
            detail=detail,
        )

    @classmethod
    def read(cls, value: str) -> "AdapterReading":
        return cls(available=True, value=value)


@runtime_checkable
class DeviceVerificationAdapter(Protocol):
    """Reads one setting back from a device, under the server's own authority.

    An implementation must only return a reading it actually obtained. Returning a
    plausible value when the device could not be reached would convert an honest
    "unavailable" into a false "verified", which is the failure this interface exists to
    prevent.
    """

    name: str

    async def read_state(self, *, deeplink: str, key: str) -> AdapterReading:
        ...


class UnavailableAdapter:
    """The production adapter: no device channel exists, so nothing is read.

    This is deliberately not a stub awaiting completion. Until a Galaxy-side component
    exists, "unavailable" is the correct and truthful answer, and the console falls back
    to clearly labelled user confirmation.
    """

    name = "unavailable"

    def __init__(self, detail: str | None = None) -> None:
        self._detail = detail or (
            "no device channel is configured; this deployment cannot read Galaxy "
            "settings back, so automatic verification is unavailable"
        )

    async def read_state(self, *, deeplink: str, key: str) -> AdapterReading:
        return AdapterReading.unavailable(self._detail)
