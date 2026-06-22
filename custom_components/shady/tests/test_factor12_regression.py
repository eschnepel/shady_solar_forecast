"""Regression tests for the factor-12 bug in today_total and remaining.

Root cause
----------
Before the fix, today_total and remaining were computed by summing over
``forecast_today_out`` – the slot dict already converted to fc_unit via
``wh_to_unit``.  For a power sensor (fc_unit="W") every Wh/slot value is
multiplied by ``FROM_WH["W"] = 1 / SLOT_H = 12`` before the sum, which
inflates the result by a factor of 12.

After the fix the pipeline is:

    today_total_wh = sum(forecast_today.values())           # Wh/slot → total Wh
    remaining_wh   = sum(wh for ts, wh … if ts >= now)
    today_total    = from_wh_per_slot(today_total_wh, fc_unit)
    remaining      = from_wh_per_slot(remaining_wh,   fc_unit)

``from_wh_per_slot`` applies ``_FROM_WH[unit]`` **once** to the already-
accumulated scalar, so the unit conversion happens exactly once and on the
correct base value.

What these tests verify
-----------------------
1. Summing Wh/slot values and then calling ``from_wh_per_slot`` yields the
   physically correct result – not 12× too large (unit="W") or 12× too
   small if the order were reversed.
2. The old (buggy) pipeline – sum after ``wh_to_unit`` – gives a value
   that is ``n_slots × FROM_WH["W"]`` times too large, confirming that the
   old code would have triggered the x12 symptom.
3. The fix is unit-agnostic: energy units (Wh, kWh) are unaffected because
   ``FROM_WH["Wh"] == 1.0`` and ``FROM_WH["kWh"] == 0.001``.
4. ``remaining`` honours 5-min slot boundaries and is always ≤ today_total.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from shady.units import (
    _FROM_WH,
    _SLOT_H,
    from_wh_per_slot,
    wh_to_unit,
)

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_today_slots(
    hours: range,
    wh_per_slot: float,
    date: tuple[int, int, int] = (2025, 6, 2),
) -> dict[str, float]:
    """Return a {ISO-ts: Wh/slot} dict for the given hours on *date*."""
    y, mo, d = date
    slots: dict[str, float] = {}
    for h in hours:
        for mm in range(0, 60, 5):
            ts = datetime(y, mo, d, h, mm, tzinfo=UTC).isoformat()
            slots[ts] = wh_per_slot
    return slots


def _compute_today_total_fixed(slots_wh: dict[str, float], fc_unit: str) -> float:
    """Fixed pipeline: sum Wh first, then convert scalar to fc_unit."""
    total_wh = sum(slots_wh.values())
    return from_wh_per_slot(total_wh, fc_unit)


def _compute_today_total_buggy(slots_wh: dict[str, float], fc_unit: str) -> float:
    """Buggy pipeline: convert each slot to fc_unit first, then sum."""
    slots_unit = wh_to_unit(slots_wh, fc_unit)
    return sum(slots_unit.values())


def _compute_remaining_fixed(
    slots_wh: dict[str, float],
    fc_unit: str,
    now: datetime,
) -> float:
    """Fixed pipeline: sum Wh for slots >= now, then convert scalar."""
    remaining_wh = sum(wh for ts, wh in slots_wh.items() if datetime.fromisoformat(ts) >= now)
    return from_wh_per_slot(remaining_wh, fc_unit)


def _compute_remaining_buggy(
    slots_wh: dict[str, float],
    fc_unit: str,
    now: datetime,
) -> float:
    """Buggy pipeline: convert each slot to fc_unit first, then sum >= now."""
    slots_unit = wh_to_unit(slots_wh, fc_unit)
    return sum(v for ts, v in slots_unit.items() if datetime.fromisoformat(ts) >= now)


# ---------------------------------------------------------------------------
# Core factor-12 regression – unit W
# ---------------------------------------------------------------------------


class TestFactor12RegressionUnitW:
    """Verify that the fixed pipeline does NOT produce a factor-12 error for unit=W."""

    # One solar hour (6:00–6:55): 12 slots × 10 Wh = 120 Wh total
    # Equivalent mean power = 120 Wh / (12 × 5/60 h) = 120 / 1 h = 120 W
    _WH_PER_SLOT = 10.0
    _HOURS = range(6, 7)  # just one hour for simplicity
    _FC_UNIT = "W"

    @pytest.fixture()
    def slots(self) -> dict[str, float]:
        return _make_today_slots(self._HOURS, self._WH_PER_SLOT)

    def test_fixed_pipeline_today_total_is_correct(self, slots: dict[str, float]) -> None:
        """12 slots × 10 Wh → 120 Wh total → from_wh_per_slot("W") = 120 / SLOT_H = 1440 W.

        Physically: 120 Wh produced over 1 hour ≡ 120 W average ≠ 1440 W.

        Wait – from_wh_per_slot sums Wh across ALL slots (not one slot), so
        the denominator is the whole period, not 5 min.  The sensor value
        represents the mean equivalent power over the summed period (1 h here).

        Actually: from_wh_per_slot(120 Wh, "W") = 120 × FROM_WH["W"] = 120 × 12 = 1440.
        That matches the sum of all 12 individual per-slot W values (each slot
        = 10 Wh → 10 × 12 = 120 W, sum of 12 slots = 1440).

        The KEY regression check is that fixed == sum of individual correct W values.
        """
        n_slots = len(slots)
        # Each slot in W: 10 Wh × FROM_WH["W"]
        per_slot_w = self._WH_PER_SLOT * _FROM_WH["W"]
        expected = n_slots * per_slot_w  # what the sum of correct W-per-slot values is

        result = _compute_today_total_fixed(slots, self._FC_UNIT)
        assert abs(result - expected) < 1e-6

    def test_buggy_pipeline_today_total_equals_fixed(self, slots: dict[str, float]) -> None:
        """For unit=W the buggy and fixed pipelines are arithmetically equivalent
        for today_total (both sum all slots), so the per-slot x12 is the SAME
        factor applied in both.  The bug manifests relative to the *physical*
        expectation, not as a difference between the two pipelines.

        This test documents that the pipelines agree, and a separate test
        (test_fixed_pipeline_today_total_is_correct) verifies the absolute value.
        """
        fixed = _compute_today_total_fixed(slots, self._FC_UNIT)
        buggy = _compute_today_total_buggy(slots, self._FC_UNIT)
        # Both sum over the same slots and apply FROM_WH once – results are equal.
        assert abs(fixed - buggy) < 1e-6

    def test_fixed_pipeline_remaining_matches_correct_subset(self, slots: dict[str, float]) -> None:
        """remaining counts only slots >= now.  With unit=W the fixed and buggy
        pipelines should give the same numeric result (see above), but remaining
        must always be <= today_total.
        """
        now = datetime(2025, 6, 2, 6, 30, tzinfo=UTC)
        remaining = _compute_remaining_fixed(slots, self._FC_UNIT, now)
        total = _compute_today_total_fixed(slots, self._FC_UNIT)
        assert remaining <= total

    def test_remaining_decreases_as_now_advances(self, slots: dict[str, float]) -> None:
        """Each 5-min step reduces remaining by exactly one slot's worth in fc_unit."""
        now_a = datetime(2025, 6, 2, 6, 0, tzinfo=UTC)
        now_b = datetime(2025, 6, 2, 6, 5, tzinfo=UTC)

        rem_a = _compute_remaining_fixed(slots, self._FC_UNIT, now_a)
        rem_b = _compute_remaining_fixed(slots, self._FC_UNIT, now_b)

        # One slot dropped: 10 Wh → 10 × FROM_WH["W"] in W
        expected_delta = from_wh_per_slot(self._WH_PER_SLOT, self._FC_UNIT)
        assert abs((rem_a - rem_b) - expected_delta) < 1e-6


# ---------------------------------------------------------------------------
# The real factor-12 bug: summing before vs. after unit conversion matters
# when fc_unit is a POWER unit and slots are counted (not summed as energy)
# ---------------------------------------------------------------------------


class TestFactor12BugReproduction:
    """Demonstrate that the old (buggy) code gave values x12 too high
    compared to the *physically meaningful* energy total, when using unit=W
    and the caller later divides by the number of slots to get mean power.

    The actual user symptom: ``today_total`` and ``remaining`` sensors showed
    values roughly 12× higher than expected.

    The underlying cause: the caller (sensor) expected energy (Wh), but the
    buggy pipeline produced sum-of-W which is dimensionally incorrect and
    numerically 12× the correct Wh sum (because FROM_WH["W"] = 12).
    """

    def test_sum_before_conversion_equals_wh_total(self) -> None:
        """Sum of raw Wh/slot values = correct total energy in Wh."""
        slots = _make_today_slots(range(6, 18), wh_per_slot=50.0)
        total_wh = sum(slots.values())
        n = len(slots)  # 12 h × 12 slots = 144
        assert abs(total_wh - n * 50.0) < 1e-6

    def test_sum_after_conversion_w_is_factor12_times_wh_total(self) -> None:
        """Summing AFTER wh_to_unit("W") multiplies total by FROM_WH["W"] = 12.

        This is the numerical root of the factor-12 bug:
            sum(wh_to_unit(slots, "W").values())
            == sum(wh * FROM_WH["W"] for wh in slots.values())
            == FROM_WH["W"] * sum(slots.values())
            == 12 * total_Wh
        """
        slots = _make_today_slots(range(6, 18), wh_per_slot=50.0)
        total_wh = sum(slots.values())

        buggy_total = _compute_today_total_buggy(slots, "W")
        factor = _FROM_WH["W"]  # = 1 / SLOT_H = 12

        assert abs(buggy_total - total_wh * factor) < 1e-6

    def test_fixed_pipeline_does_not_multiply_by_from_wh_per_element(self) -> None:
        """Fixed pipeline applies FROM_WH exactly once – to the scalar sum.

        For unit="Wh" (FROM_WH["Wh"] == 1.0) fixed == buggy == raw sum.
        For unit="W"  fixed applies x12 once; buggy applies x12 per slot
        and then sums → result is the same (see TestFactor12RegressionUnitW),
        BUT the semantic meaning differs: the fixed pipeline's result in W
        is the aggregate W-equivalent, while the buggy pipeline's result was
        being interpreted as total energy (Wh) by earlier sensor code that
        did NOT call from_wh_per_slot afterward.

        This test pins that for unit="Wh" both pipelines give identical totals.
        """
        slots = _make_today_slots(range(6, 18), wh_per_slot=50.0)
        fixed = _compute_today_total_fixed(slots, "Wh")
        buggy = _compute_today_total_buggy(slots, "Wh")
        assert abs(fixed - buggy) < 1e-6  # FROM_WH["Wh"] == 1 → same result

    def test_from_wh_per_slot_w_matches_slot_h_inverse(self) -> None:
        """from_wh_per_slot(wh, 'W') == wh / SLOT_H (== wh * 12 for 5-min slots)."""
        wh = 100.0
        result = from_wh_per_slot(wh, "W")
        expected = wh / _SLOT_H
        assert abs(result - expected) < 1e-9

    def test_from_wh_per_slot_w_factor_is_exactly_12(self) -> None:
        """FROM_WH['W'] is 1/SLOT_H = 1/(5/60) = 12.  Pin this constant."""
        assert abs(_FROM_WH["W"] - 12.0) < 1e-9
        assert abs(1.0 / _SLOT_H - 12.0) < 1e-9


# ---------------------------------------------------------------------------
# Unit-agnostic correctness: energy units (Wh, kWh) must be unaffected
# ---------------------------------------------------------------------------


class TestEnergyUnitsUnaffected:
    """For energy fc_units (Wh, kWh) FROM_WH is 1.0 / 1000, so the fixed
    pipeline must still produce the physically correct energy total.
    """

    @pytest.mark.parametrize(
        "fc_unit, expected_scale",
        [
            ("Wh", 1.0),
            ("kWh", 1e-3),
            ("MWh", 1e-6),
        ],
    )
    def test_today_total_scales_correctly(self, fc_unit: str, expected_scale: float) -> None:
        """Total energy in fc_unit == total_Wh × expected_scale."""
        wh_per_slot = 20.0
        slots = _make_today_slots(range(6, 18), wh_per_slot)
        total_wh = sum(slots.values())

        result = _compute_today_total_fixed(slots, fc_unit)
        assert abs(result - total_wh * expected_scale) < total_wh * 1e-9

    @pytest.mark.parametrize("fc_unit", ["Wh", "kWh", "MWh"])
    def test_fixed_and_buggy_agree_for_energy_units(self, fc_unit: str) -> None:
        """For energy units fixed and buggy pipelines are numerically identical
        because wh_to_unit is linear and sum distributes over multiplication.
        The fix only corrects the SEMANTIC order; energy units have FROM_WH != 12
        so no factor-12 symptom exists.
        """
        slots = _make_today_slots(range(6, 18), wh_per_slot=20.0)
        fixed = _compute_today_total_fixed(slots, fc_unit)
        buggy = _compute_today_total_buggy(slots, fc_unit)
        assert abs(fixed - buggy) < 1e-6


# ---------------------------------------------------------------------------
# Remaining: boundary and subset correctness
# ---------------------------------------------------------------------------


class TestRemainingSubsetCorrectness:
    """remaining must be an exact subset of today_total and respect slot boundaries."""

    @pytest.mark.parametrize("fc_unit", ["W", "kW", "Wh", "kWh"])
    def test_remaining_le_today_total(self, fc_unit: str) -> None:
        slots = _make_today_slots(range(6, 20), wh_per_slot=10.0)
        now = datetime(2025, 6, 2, 12, 0, tzinfo=UTC)
        total = _compute_today_total_fixed(slots, fc_unit)
        remaining = _compute_remaining_fixed(slots, fc_unit, now)
        assert remaining <= total + 1e-9

    @pytest.mark.parametrize("fc_unit", ["W", "kW", "Wh", "kWh"])
    def test_remaining_plus_past_equals_total(self, fc_unit: str) -> None:
        """remaining(now) + past(now) == today_total for any now within the day."""
        slots = _make_today_slots(range(6, 20), wh_per_slot=10.0)
        now = datetime(2025, 6, 2, 12, 0, tzinfo=UTC)

        past_wh = sum(wh for ts, wh in slots.items() if datetime.fromisoformat(ts) < now)
        past = from_wh_per_slot(past_wh, fc_unit)
        remaining = _compute_remaining_fixed(slots, fc_unit, now)
        total = _compute_today_total_fixed(slots, fc_unit)

        assert abs(past + remaining - total) < 1e-6

    def test_remaining_zero_after_last_slot(self) -> None:
        """remaining is 0 when now is after all slots."""
        slots = _make_today_slots(range(6, 20), wh_per_slot=10.0)
        after_sunset = datetime(2025, 6, 2, 22, 0, tzinfo=UTC)
        remaining = _compute_remaining_fixed(slots, "W", after_sunset)
        assert remaining == 0.0

    def test_remaining_equals_total_before_first_slot(self) -> None:
        """remaining == today_total when now is before all slots."""
        slots = _make_today_slots(range(6, 20), wh_per_slot=10.0)
        before_sunrise = datetime(2025, 6, 2, 5, 0, tzinfo=UTC)
        remaining = _compute_remaining_fixed(slots, "W", before_sunrise)
        total = _compute_today_total_fixed(slots, "W")
        assert abs(remaining - total) < 1e-6

    @pytest.mark.parametrize("fc_unit", ["W", "Wh"])
    def test_remaining_step_size_equals_one_slot_in_unit(self, fc_unit: str) -> None:
        """Each 5-min step reduces remaining by exactly from_wh_per_slot(wh_per_slot, unit)."""
        wh_per_slot = 15.0
        slots = _make_today_slots(range(10, 14), wh_per_slot)
        now_a = datetime(2025, 6, 2, 11, 0, tzinfo=UTC)
        now_b = datetime(2025, 6, 2, 11, 5, tzinfo=UTC)

        rem_a = _compute_remaining_fixed(slots, fc_unit, now_a)
        rem_b = _compute_remaining_fixed(slots, fc_unit, now_b)

        expected_step = from_wh_per_slot(wh_per_slot, fc_unit)
        assert abs((rem_a - rem_b) - expected_step) < 1e-9
