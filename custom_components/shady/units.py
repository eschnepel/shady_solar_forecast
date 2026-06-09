"""units.py – Unit detection and Wh/slot conversion for PV and FC sensors.

All internal processing in Shady uses Wh per 5-minute slot.
This module handles the boundary conversions in both directions.

Supported units
---------------
Power  : W, kW
Energy : Wh, kWh, MWh

State classes
-------------
power  sensors → SensorStateClass.MEASUREMENT
energy sensors → SensorStateClass.TOTAL_INCREASING  (conservative default)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Minutes per 5-min slot → hours
_SLOT_H: float = 5.0 / 60.0

# Supported units grouped by type
_POWER_UNITS: frozenset[str] = frozenset({"W", "kW"})
_ENERGY_UNITS: frozenset[str] = frozenset({"Wh", "kWh", "MWh"})
_ALL_UNITS: frozenset[str] = _POWER_UNITS | _ENERGY_UNITS

# Multiplier to convert 1 unit of mean to Wh per 5-min slot
_TO_WH: dict[str, float] = {
    "W": _SLOT_H,  # mean W × 5/60 = Wh
    "kW": _SLOT_H * 1_000,  # mean kW × 1000 × 5/60 = Wh
    "Wh": 1.0,
    "kWh": 1_000.0,
    "MWh": 1_000_000.0,
}

# Divisor to convert internal Wh per slot back to 1 unit of output
_FROM_WH: dict[str, float] = {
    "W": 1.0 / _SLOT_H,
    "kW": 1.0 / (_SLOT_H * 1_000),
    "Wh": 1.0,
    "kWh": 1.0 / 1_000.0,
    "MWh": 1.0 / 1_000_000.0,
}

# State class strings (avoid importing HA enum at module level for testability)
_STATE_CLASS_MEASUREMENT = "measurement"
_STATE_CLASS_TOTAL_INCREASING = "total_increasing"

_DEFAULT_UNIT = "W"
_DEFAULT_STATE_CLASS = _STATE_CLASS_MEASUREMENT


def _state_class_for_unit(unit: str) -> str:
    if unit in _POWER_UNITS:
        return _STATE_CLASS_MEASUREMENT
    return _STATE_CLASS_TOTAL_INCREASING


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


async def detect_unit(hass: HomeAssistant, entity_id: str) -> tuple[str, str]:
    """Return (unit_of_measurement, state_class) for *entity_id*.

    Lookup order:
      1. HA entity registry (native_unit_of_measurement)
      2. Recorder statistics metadata
      3. Fallback: ("W", "measurement")

    Unrecognised units fall back to the default with a warning.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is not None and entry.unit_of_measurement:
        unit = entry.unit_of_measurement
        if unit in _ALL_UNITS:
            state_class = (
                entry.capabilities.get("state_class", _state_class_for_unit(unit))
                if entry.capabilities
                else _state_class_for_unit(unit)
            )
            _LOGGER.debug("unit for %s from entity registry: %s / %s", entity_id, unit, state_class)
            return unit, str(state_class)
        _LOGGER.warning(
            "Shady: unrecognised unit '%s' for %s – falling back to '%s'",
            unit,
            entity_id,
            _DEFAULT_UNIT,
        )
        return _DEFAULT_UNIT, _DEFAULT_STATE_CLASS

    # Fallback: recorder statistics metadata
    try:
        from homeassistant.components.recorder import get_instance as get_recorder
        from homeassistant.components.recorder.statistics import get_metadata

        def _query() -> dict[str, Any]:
            return get_metadata(hass, statistic_ids=[entity_id])

        meta = await get_recorder(hass).async_add_executor_job(_query)
        if entity_id in meta:
            m = meta[entity_id][1]  # (StatisticMetaData,) or (id, StatisticMetaData)
            raw_unit = getattr(m, "unit_of_measurement", None) or (
                m.get("unit_of_measurement") if isinstance(m, dict) else None
            )
            if raw_unit and raw_unit in _ALL_UNITS:
                sc = _state_class_for_unit(raw_unit)
                _LOGGER.debug(
                    "unit for %s from statistics metadata: %s / %s", entity_id, raw_unit, sc
                )
                return raw_unit, sc
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not read statistics metadata for %s: %s", entity_id, err)

    _LOGGER.warning("Shady: cannot determine unit for %s – assuming '%s'", entity_id, _DEFAULT_UNIT)
    return _DEFAULT_UNIT, _DEFAULT_STATE_CLASS


def check_pv_unit_consistency(units: dict[str, str]) -> None:
    """Log a warning if pv_sensors have mixed units.

    Args:
        units: {entity_id: unit_string}
    """
    unique = set(units.values())
    if len(unique) > 1:
        detail = ", ".join(f"{eid}={u}" for eid, u in units.items())
        _LOGGER.warning(
            "Shady: pv_sensors have mixed units: %s – each will be converted individually",
            detail,
        )


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def to_wh_per_slot(rows: list[dict], unit: str) -> list[dict]:
    """Convert [{start, mean}] rows from *unit* to Wh per 5-min slot in-place copy.

    Power sensors  : mean W/kW  → Wh  (× slot_h or × slot_h × 1000)
    Energy sensors : mean Wh/kWh/MWh → Wh (× 1 / 1000 / 1_000_000)
    """
    if unit not in _ALL_UNITS:
        _LOGGER.warning("Shady: unknown unit '%s' in to_wh_per_slot – treating as Wh", unit)
        return rows
    factor = _TO_WH[unit]
    if factor == 1.0:
        return rows
    return [{"start": r["start"], "mean": r["mean"] * factor} for r in rows]


def from_wh_per_slot(wh: float, unit: str) -> float:
    """Convert a single Wh/slot value to *unit*."""
    if unit not in _ALL_UNITS:
        return wh
    return wh * _FROM_WH[unit]


def wh_to_unit(slots: dict[str, float], unit: str) -> dict[str, float]:
    """Convert a full {ts: Wh} slot dict to *unit*."""
    if unit not in _ALL_UNITS or _FROM_WH[unit] == 1.0:
        return slots
    factor = _FROM_WH[unit]
    return {ts: wh * factor for ts, wh in slots.items()}
