"""Tests for unit detection and Wh/slot conversion (units.py).

Covers:
- _TO_WH / _FROM_WH constants consistency
- to_wh_per_slot: all units, identity, unknown unit
- from_wh_per_slot: all units, round-trip
- wh_to_unit: full slot dict conversion
- check_pv_unit_consistency: no warning / warning logged
- _state_class_for_unit: power vs energy
"""

from __future__ import annotations

import logging


from custom_components.shady.units import (
    _ALL_UNITS,
    _ENERGY_UNITS,
    _FROM_WH,
    _POWER_UNITS,
    _SLOT_H,
    _TO_WH,
    _state_class_for_unit,
    check_pv_unit_consistency,
    from_wh_per_slot,
    to_wh_per_slot,
    wh_to_unit,
)


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


class TestConstants:
    def test_to_wh_and_from_wh_are_inverses(self):
        """_TO_WH[u] × _FROM_WH[u] == 1.0 for every supported unit."""
        for unit in _ALL_UNITS:
            product = _TO_WH[unit] * _FROM_WH[unit]
            to, fr = _TO_WH[unit], _FROM_WH[unit]
            assert abs(product - 1.0) < 1e-10, f"unit={unit}: {to} × {fr} = {product}"

    def test_all_units_in_to_wh(self):
        assert _ALL_UNITS <= set(_TO_WH)

    def test_all_units_in_from_wh(self):
        assert _ALL_UNITS <= set(_FROM_WH)

    def test_wh_factor_is_1(self):
        assert _TO_WH["Wh"] == 1.0
        assert _FROM_WH["Wh"] == 1.0

    def test_w_factor(self):
        assert abs(_TO_WH["W"] - 5 / 60) < 1e-10

    def test_kw_factor(self):
        assert abs(_TO_WH["kW"] - 5 / 60 * 1000) < 1e-10

    def test_kwh_factor(self):
        assert _TO_WH["kWh"] == 1000.0

    def test_mwh_factor(self):
        assert _TO_WH["MWh"] == 1_000_000.0

    def test_slot_h(self):
        assert abs(_SLOT_H - 5 / 60) < 1e-10


# ---------------------------------------------------------------------------
# _state_class_for_unit
# ---------------------------------------------------------------------------


class TestStateClassForUnit:
    def test_power_units_give_measurement(self):
        for u in _POWER_UNITS:
            assert _state_class_for_unit(u) == "measurement", f"failed for {u}"

    def test_energy_units_give_total_increasing(self):
        for u in _ENERGY_UNITS:
            assert _state_class_for_unit(u) == "total_increasing", f"failed for {u}"


# ---------------------------------------------------------------------------
# to_wh_per_slot
# ---------------------------------------------------------------------------


def _rows(means: list[float]) -> list[dict]:
    from datetime import datetime, timezone

    base = datetime(2025, 6, 1, 10, 0, tzinfo=timezone.utc)
    from datetime import timedelta

    return [{"start": base + timedelta(minutes=5 * i), "mean": m} for i, m in enumerate(means)]


class TestToWhPerSlot:
    def test_wh_is_identity(self):
        rows = _rows([100.0, 200.0])
        result = to_wh_per_slot(rows, "Wh")
        assert result is rows  # same object – no copy made

    def test_kwh_multiplied_by_1000(self):
        rows = _rows([1.0, 2.0])
        result = to_wh_per_slot(rows, "kWh")
        assert abs(result[0]["mean"] - 1000.0) < 1e-6
        assert abs(result[1]["mean"] - 2000.0) < 1e-6

    def test_mwh_multiplied_by_1e6(self):
        rows = _rows([0.001])
        result = to_wh_per_slot(rows, "MWh")
        assert abs(result[0]["mean"] - 1000.0) < 1e-6

    def test_w_multiplied_by_slot_h(self):
        rows = _rows([120.0])  # 120 W average
        result = to_wh_per_slot(rows, "W")
        expected = 120.0 * 5 / 60  # = 10 Wh
        assert abs(result[0]["mean"] - expected) < 1e-6

    def test_kw_multiplied_by_slot_h_times_1000(self):
        rows = _rows([1.0])  # 1 kW average
        result = to_wh_per_slot(rows, "kW")
        expected = 1000.0 * 5 / 60  # ≈ 83.33 Wh
        assert abs(result[0]["mean"] - expected) < 1e-6

    def test_start_timestamps_preserved(self):
        rows = _rows([10.0, 20.0])
        result = to_wh_per_slot(rows, "kWh")
        assert result[0]["start"] == rows[0]["start"]
        assert result[1]["start"] == rows[1]["start"]

    def test_original_rows_not_mutated(self):
        rows = _rows([100.0])
        original_mean = rows[0]["mean"]
        to_wh_per_slot(rows, "kWh")
        assert rows[0]["mean"] == original_mean

    def test_unknown_unit_returns_rows_unchanged(self, caplog):
        rows = _rows([50.0])
        with caplog.at_level(logging.WARNING):
            result = to_wh_per_slot(rows, "FUBAR")
        assert result is rows
        assert "unknown unit" in caplog.text.lower() or "FUBAR" in caplog.text

    def test_empty_rows(self):
        assert to_wh_per_slot([], "kWh") == []


# ---------------------------------------------------------------------------
# from_wh_per_slot
# ---------------------------------------------------------------------------


class TestFromWhPerSlot:
    def test_wh_identity(self):
        assert from_wh_per_slot(100.0, "Wh") == 100.0

    def test_kwh_divides_by_1000(self):
        assert abs(from_wh_per_slot(1000.0, "kWh") - 1.0) < 1e-9

    def test_mwh_divides_by_1e6(self):
        assert abs(from_wh_per_slot(1_000_000.0, "MWh") - 1.0) < 1e-9

    def test_w_divides_by_slot_h(self):
        wh = 120.0 * 5 / 60
        assert abs(from_wh_per_slot(wh, "W") - 120.0) < 1e-6

    def test_kw_divides_by_slot_h_times_1000(self):
        wh = 1000.0 * 5 / 60
        assert abs(from_wh_per_slot(wh, "kW") - 1.0) < 1e-6

    def test_unknown_unit_returns_wh_unchanged(self):
        assert from_wh_per_slot(42.0, "FUBAR") == 42.0

    def test_round_trip_all_units(self):
        """to_wh followed by from_wh must reproduce original value."""
        for unit in _ALL_UNITS:
            original = 300.0
            as_wh = original * _TO_WH[unit]
            back = from_wh_per_slot(as_wh, unit)
            assert abs(back - original) < 1e-6, f"round-trip failed for {unit}"


# ---------------------------------------------------------------------------
# wh_to_unit
# ---------------------------------------------------------------------------


class TestWhToUnit:
    def test_wh_identity(self):
        slots = {"2025-06-01T10:00:00+00:00": 100.0, "2025-06-01T10:05:00+00:00": 200.0}
        result = wh_to_unit(slots, "Wh")
        assert result is slots  # no copy for identity conversion

    def test_kwh_conversion(self):
        slots = {"2025-06-01T10:00:00+00:00": 1000.0}
        result = wh_to_unit(slots, "kWh")
        assert abs(result["2025-06-01T10:00:00+00:00"] - 1.0) < 1e-9

    def test_keys_preserved(self):
        slots = {"a": 100.0, "b": 200.0}
        result = wh_to_unit(slots, "kWh")
        assert set(result) == {"a", "b"}

    def test_empty_slots(self):
        assert wh_to_unit({}, "kWh") == {}

    def test_unknown_unit_returns_unchanged(self):
        slots = {"a": 42.0}
        result = wh_to_unit(slots, "FUBAR")
        assert result == slots

    def test_w_conversion(self):
        wh = 120.0 * 5 / 60
        slots = {"ts": wh}
        result = wh_to_unit(slots, "W")
        assert abs(result["ts"] - 120.0) < 1e-6


# ---------------------------------------------------------------------------
# check_pv_unit_consistency
# ---------------------------------------------------------------------------


class TestCheckPvUnitConsistency:
    def test_all_same_no_warning(self, caplog):
        units = {"sensor.a": "W", "sensor.b": "W", "sensor.c": "W"}
        with caplog.at_level(logging.WARNING):
            check_pv_unit_consistency(units)
        assert "mixed" not in caplog.text

    def test_mixed_units_logs_warning(self, caplog):
        units = {"sensor.a": "W", "sensor.b": "kWh"}
        with caplog.at_level(logging.WARNING):
            check_pv_unit_consistency(units)
        assert "mixed" in caplog.text

    def test_warning_contains_entity_ids(self, caplog):
        units = {"sensor.a": "W", "sensor.b": "kWh"}
        with caplog.at_level(logging.WARNING):
            check_pv_unit_consistency(units)
        assert "sensor.a" in caplog.text
        assert "sensor.b" in caplog.text

    def test_single_sensor_no_warning(self, caplog):
        units = {"sensor.a": "Wh"}
        with caplog.at_level(logging.WARNING):
            check_pv_unit_consistency(units)
        assert "mixed" not in caplog.text

    def test_empty_dict_no_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            check_pv_unit_consistency({})
        assert "mixed" not in caplog.text

    def test_three_different_units_warns(self, caplog):
        units = {"sensor.a": "W", "sensor.b": "kWh", "sensor.c": "MWh"}
        with caplog.at_level(logging.WARNING):
            check_pv_unit_consistency(units)
        assert "mixed" in caplog.text
