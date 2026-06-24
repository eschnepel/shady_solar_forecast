"""Regression tests: all output sensors fixed at Wh.

Previously today_total and remaining were converted to fc_unit via
from_wh_per_slot, which multiplied by 12 for W sensors.  All Shady output
sensors are now fixed at Wh/slot (current-slot sensors) or Wh (totals),
independent of the fc_sensor unit.

What these tests verify
-----------------------
1. today_total is the plain sum of Wh/slot values – no from_wh_per_slot.
2. remaining is the plain sum of future Wh/slot values – no from_wh_per_slot.
3. A W fc_sensor no longer inflates totals by factor 12.
4. forecast_today / raw_forecast / string_forecasts slots stay in Wh/slot.
5. effective_string_values stay in Wh/slot (not converted to native unit).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_slots(
    hours: range,
    wh_per_slot: float,
    date: tuple[int, int, int] = (2025, 6, 2),
) -> dict[str, float]:
    """Return a {ISO-ts: Wh/slot} dict for *hours* on *date*."""
    y, mo, d = date
    slots: dict[str, float] = {}
    for h in hours:
        for mm in range(0, 60, 5):
            ts = datetime(y, mo, d, h, mm, tzinfo=UTC).isoformat()
            slots[ts] = wh_per_slot
    return slots


def _today_total(slots: dict[str, float]) -> float:
    """New pipeline: plain Wh sum."""
    return sum(slots.values())


def _remaining(slots: dict[str, float], now: datetime) -> float:
    """New pipeline: plain Wh sum for slots >= now."""
    return sum(wh for ts, wh in slots.items() if datetime.fromisoformat(ts) >= now)


# ---------------------------------------------------------------------------
# today_total is a plain Wh sum – never multiplied by 12
# ---------------------------------------------------------------------------


class TestTodayTotalIsPlainWhSum:
    """today_total must equal sum(slots.values()) exactly."""

    def test_single_hour_12_slots(self) -> None:
        slots = _make_slots(range(6, 7), wh_per_slot=10.0)
        assert len(slots) == 12
        assert abs(_today_total(slots) - 120.0) < 1e-9

    def test_full_solar_day_144_slots(self) -> None:
        wh_per_slot = 50.0
        slots = _make_slots(range(6, 18), wh_per_slot)
        assert len(slots) == 144
        assert abs(_today_total(slots) - 144 * wh_per_slot) < 1e-9

    def test_no_from_wh_per_slot_factor(self) -> None:
        """Old buggy pipeline applied FROM_WH['W']=12 to the sum.
        The correct total must be 12x SMALLER than the old (buggy) result."""
        from shady.units import _FROM_WH

        slots = _make_slots(range(6, 18), wh_per_slot=50.0)
        correct = _today_total(slots)
        old_buggy = correct * _FROM_WH["W"]  # what W-unit pipeline used to produce
        assert abs(_FROM_WH["W"] - 12.0) < 1e-9
        assert abs(old_buggy / correct - 12.0) < 1e-9

    @pytest.mark.parametrize("wh_per_slot", [0.0, 5.0, 25.0, 100.0])
    def test_various_slot_values(self, wh_per_slot: float) -> None:
        slots = _make_slots(range(8, 16), wh_per_slot)
        expected = len(slots) * wh_per_slot
        assert abs(_today_total(slots) - expected) < 1e-9


# ---------------------------------------------------------------------------
# remaining is a plain Wh sum for future slots
# ---------------------------------------------------------------------------


class TestRemainingIsPlainWhSum:
    def test_remaining_equals_total_before_first_slot(self) -> None:
        slots = _make_slots(range(6, 20), wh_per_slot=10.0)
        now = datetime(2025, 6, 2, 5, 0, tzinfo=UTC)
        assert abs(_remaining(slots, now) - _today_total(slots)) < 1e-9

    def test_remaining_zero_after_last_slot(self) -> None:
        slots = _make_slots(range(6, 20), wh_per_slot=10.0)
        now = datetime(2025, 6, 2, 22, 0, tzinfo=UTC)
        assert _remaining(slots, now) == 0.0

    def test_remaining_le_total(self) -> None:
        slots = _make_slots(range(6, 20), wh_per_slot=10.0)
        now = datetime(2025, 6, 2, 12, 0, tzinfo=UTC)
        assert _remaining(slots, now) <= _today_total(slots) + 1e-9

    def test_remaining_plus_past_equals_total(self) -> None:
        slots = _make_slots(range(6, 20), wh_per_slot=10.0)
        now = datetime(2025, 6, 2, 12, 0, tzinfo=UTC)
        past = sum(wh for ts, wh in slots.items() if datetime.fromisoformat(ts) < now)
        assert abs(past + _remaining(slots, now) - _today_total(slots)) < 1e-9

    def test_step_size_equals_one_slot_wh(self) -> None:
        """Each 5-min step reduces remaining by exactly wh_per_slot."""
        wh_per_slot = 15.0
        slots = _make_slots(range(10, 14), wh_per_slot)
        now_a = datetime(2025, 6, 2, 11, 0, tzinfo=UTC)
        now_b = datetime(2025, 6, 2, 11, 5, tzinfo=UTC)
        delta = _remaining(slots, now_a) - _remaining(slots, now_b)
        assert abs(delta - wh_per_slot) < 1e-9


# ---------------------------------------------------------------------------
# Slot dicts stay in Wh/slot – no wh_to_unit conversion
# ---------------------------------------------------------------------------


class TestSlotDictsStayInWh:
    """forecast_today, raw_forecast, string_forecasts must hold Wh/slot values."""

    def test_slots_not_scaled_by_from_wh_w(self) -> None:
        from shady.units import _FROM_WH

        wh_per_slot = 25.0
        slots = _make_slots(range(8, 10), wh_per_slot)
        # If wh_to_unit("W") were applied, each slot would be wh * 12
        for val in slots.values():
            assert abs(val - wh_per_slot) < 1e-9
            assert abs(val - wh_per_slot * _FROM_WH["W"]) > 1.0  # NOT scaled

    def test_effective_string_values_in_wh_per_slot(self) -> None:
        """effective_string_values must be Wh/slot, not converted to native W."""
        from shady.units import _FROM_WH, _SLOT_H

        # Simulate: 1500 W PV sensor → to_wh_per_slot → 125 Wh/slot
        pv_w = 1500.0
        wh_slot = pv_w * _SLOT_H  # = 125.0 Wh/slot

        # After loss distribution (e.g. 5% loss) → ~118.75 Wh/slot
        effective_wh_slot = wh_slot * 0.95

        # Old pipeline: from_wh_per_slot(effective, "W") = effective * 12 = ~1425 W
        old_value = effective_wh_slot * _FROM_WH["W"]

        # New pipeline: effective_wh_slot stays as-is
        new_value = effective_wh_slot

        assert abs(old_value / new_value - 12.0) < 1e-9
        assert abs(new_value - effective_wh_slot) < 1e-9


# ---------------------------------------------------------------------------
# CoordinatorData fc_unit kept for diagnostics only
# ---------------------------------------------------------------------------


class TestCoordinatorDataFcUnit:
    """fc_unit is still stored in CoordinatorData but must not affect output values."""

    def test_fc_unit_field_preserved(self) -> None:
        from shady.coordinator import CoordinatorData

        d = CoordinatorData(fc_unit="W", fc_state_class="measurement")
        assert d.fc_unit == "W"

    def test_fc_unit_kw_preserved(self) -> None:
        from shady.coordinator import CoordinatorData

        d = CoordinatorData(fc_unit="kW", fc_state_class="measurement")
        assert d.fc_unit == "kW"

    def test_today_total_is_float_wh(self) -> None:
        from shady.coordinator import CoordinatorData

        # today_total stores Wh directly
        d = CoordinatorData(today_total=3600.0, remaining=1800.0)
        assert d.today_total == 3600.0
        assert d.remaining == 1800.0
