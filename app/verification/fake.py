"""A scripted adapter, for automated tests only.

This exists so the trusted-evidence path can be exercised without a handset. It is never
selected by app/bootstrap.py and never reachable from configuration: production wiring
names UnavailableAdapter explicitly, so the only way to obtain this one is to construct it
in a test.

Keeping it beside the Protocol rather than inside the test tree is a deliberate trade: it
stays type-checked against the interface it implements, and its docstring states the rule
that keeps it out of production.
"""

from __future__ import annotations

from app.verification.adapter import AdapterReading


class FakeTrustedAdapter:
    """Returns pre-seeded readings keyed by (deeplink, key).

    Tests seed exactly the readings the scenario needs. An unseeded key reports
    unavailable rather than a default value, so a test that forgets to seed one fails as
    "unavailable" instead of silently passing on a fabricated reading.
    """

    name = "fake_trusted"

    def __init__(self, readings: dict[tuple[str, str], str] | None = None) -> None:
        self._readings: dict[tuple[str, str], str] = dict(readings or {})
        self.calls: list[tuple[str, str]] = []

    def seed(self, deeplink: str, key: str, value: str) -> "FakeTrustedAdapter":
        self._readings[(deeplink, key)] = value
        return self

    def clear(self) -> None:
        self._readings.clear()
        self.calls.clear()

    async def read_state(self, *, deeplink: str, key: str) -> AdapterReading:
        self.calls.append((deeplink, key))
        try:
            return AdapterReading.read(self._readings[(deeplink, key)])
        except KeyError:
            return AdapterReading.unavailable(
                f"no reading seeded for key {key!r} at {deeplink!r}"
            )
