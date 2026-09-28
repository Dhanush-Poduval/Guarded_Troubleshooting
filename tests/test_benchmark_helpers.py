"""Checkpoint 6 verification: the statistics behind the published report.

A wrong percentile silently misreports the headline latency figure, which is exactly the
kind of error a reader of metrics.md has no way to catch. These are pure functions, so
they are pinned directly.
"""

from __future__ import annotations

import pytest

from scripts.benchmark import fmt, p50, p95, percentile


def test_percentile_uses_nearest_rank():
    values = list(range(1, 101))  # 1..100
    assert percentile(values, 0.95) == 95
    assert percentile(values, 0.50) == 50
    assert percentile(values, 1.0) == 100


def test_p95_at_thirty_samples_picks_the_twenty_ninth_value():
    """The specification asks for N >= 30. At exactly 30 the 95th percentile rank is 28.5,
    and rounding it to 28 would report the 28th value and understate the result."""
    values = list(range(1, 31))  # 1..30
    assert p95(values) == 29


def test_percentile_handles_a_single_sample():
    assert percentile([42.0], 0.95) == 42.0


def test_percentile_of_empty_is_none():
    assert percentile([], 0.95) is None
    assert p50([]) is None
    assert p95([]) is None


def test_percentile_sorts_before_indexing():
    assert p95([100, 1, 50, 2, 3]) == 100


def test_p50_is_the_median():
    assert p50([1, 2, 3, 4, 5]) == 3


def test_fmt_marks_missing_values_rather_than_printing_zero():
    """An unmeasured figure must never render as a number a reader could mistake for a
    result."""
    assert fmt(None) == "not measured"
    assert fmt(12.345) == "12.3"
    assert fmt(12.345, " ms") == "12.3 ms"


@pytest.mark.parametrize("fraction", [0.5, 0.9, 0.95, 0.99])
def test_percentile_never_exceeds_the_maximum(fraction):
    values = [float(v) for v in range(1, 51)]
    assert percentile(values, fraction) <= max(values)
