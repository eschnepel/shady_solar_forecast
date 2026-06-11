"""coordinator.py – Orchestration, data container, lifecycle.

Data pipeline:

1. fetch_raw_forecast()         → raw_forecast: {ISO-ts: Wh}
2. fetch_statistics()           → recorder 5-min means for fc + pv sensors
3. build_bucket_models()        → per-string per-5-min-bucket WLS models
4. _apply_corrections()         → corrected forecast per string + combined
5. Split today (native res.) / tomorrow (hourly aggregation)
6. Compute today_total + remaining directly from 5-min today slots

CoordinatorData fields:
  raw_forecast      : {ISO-ts: Wh}
  forecast_today    : {ISO-ts: Wh}  – native provider resolution (5-min for hourly providers)
  forecast_tomorrow : {ISO-ts: Wh}  – aggregated to full hours
  string_forecasts  : {entity_id: {ISO-ts: Wh}}
  today_total       : float (Wh)
  remaining         : float (Wh)  – sum of slots whose start >= now (5-min precision)
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.components.energy import (
    async_get_manager as async_get_energy_manager,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_FC_SENSOR,
    CONF_HISTORY_DAYS,
    CONF_ALGORITHM,
    DEFAULT_FC_SENSOR,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_ALGORITHM,
    CONF_PV_SENSORS,
    CONF_GRID_IMPORT,
    CONF_GRID_EXPORT,
    CONF_BATTERY_IMPORT,
    CONF_BATTERY_EXPORT,
    CONF_USE_EFFECTIVE_SENSORS,
    DEFAULT_USE_EFFECTIVE_SENSORS,
    SYSTEM_SENSOR_KEYS,
    CACHE_VERSION,
)
from .forecast import fetch_raw_forecast
from .units import (
    check_pv_unit_consistency,
    detect_unit,
    to_wh_per_slot,
    wh_to_unit,
)
from shadylib import apply_corrections as _shadylib_apply_corrections
from shadylib import compute_effective_strings, split_combined_sensor
from shadylib.math_utils import aggregate_to_hours
from shadylib import r, parse_dt
from .statistics import fetch_statistics
from .effective_history import EffectiveHistoryStore


class _DiscardOnMigrationStore(Store):
    """Store subclass that silently discards cached data on any version mismatch.

    Both forecast stores in Shady hold *cache* data only – there is no
    user-configured state that needs to be preserved across version changes.
    When the major storage version is bumped the old cached data is simply
    discarded so that a fresh forecast cycle can populate it again.

    Without this override ``homeassistant.helpers.storage.Store`` raises
    ``NotImplementedError`` on a major-version mismatch, which propagates
    through ``coordinator.async_setup()`` and prevents the integration from
    loading (``ConfigEntryNotReady`` retry loop).
    """

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: Any,
    ) -> None:
        """Discard stale cache – migration is not needed for cache-only stores."""
        return None


_LOGGER = logging.getLogger(__name__)

_FALLBACK_INTERVAL = timedelta(hours=1)
_STORAGE_KEY = f"{DOMAIN}.last_forecast"
_STORAGE_VERSION = CACHE_VERSION


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------


@dataclass
class CoordinatorData:
    raw_forecast: dict[str, float] = field(default_factory=dict)
    forecast_today: dict[str, float] = field(default_factory=dict)
    forecast_tomorrow: dict[str, float] = field(default_factory=dict)
    string_forecasts: dict[str, dict[str, float]] = field(default_factory=dict)
    today_total: float = 0.0
    remaining: float = 0.0
    # Unit and state_class of the fc_sensor – used by output sensors
    fc_unit: str = "Wh"
    fc_state_class: str = "total_increasing"
    # Current effective (loss-adjusted) power per PV string {entity_id: value}
    effective_string_values: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CoordinatorData":
        return cls(
            raw_forecast=d.get("raw_forecast", {}),
            forecast_today=d.get("forecast_today", {}),
            forecast_tomorrow=d.get("forecast_tomorrow", {}),
            string_forecasts=d.get("string_forecasts", {}),
            today_total=float(d.get("today_total", 0.0)),
            remaining=float(d.get("remaining", 0.0)),
            fc_unit=d.get("fc_unit", "Wh"),
            fc_state_class=d.get("fc_state_class", "total_increasing"),
            effective_string_values=d.get("effective_string_values", {}),
        )


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class ShadyCoordinator(DataUpdateCoordinator[CoordinatorData]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=_FALLBACK_INTERVAL)
        self._entry = entry
        self._unsub_listener: Any = None
        self._store: _DiscardOnMigrationStore = _DiscardOnMigrationStore(
            hass, _STORAGE_VERSION, _STORAGE_KEY
        )
        self._effective_store = EffectiveHistoryStore(hass)
        # Cache unit/state_class per entity_id – sensor units don't change at runtime
        self._unit_cache: dict[str, tuple[str, str]] = {}

    def _cfg(self, key: str, default: Any = None) -> Any:
        d = self._entry.options or self._entry.data
        return d.get(key, default)

    async def _cached_unit(self, entity_id: str) -> tuple[str, str]:
        """Return (unit, state_class) for entity_id, reading from cache if available."""
        if entity_id not in self._unit_cache:
            self._unit_cache[entity_id] = await detect_unit(self.hass, entity_id)
        return self._unit_cache[entity_id]

    def _active_pv_sensors(self) -> list[str]:
        return [s for s in self._cfg(CONF_PV_SENSORS, []) if s]

    def _system_sensor_cfg(self) -> dict[str, str | None]:
        """Return {conf_key: entity_id | None} for the four system sensor fields."""
        return {key: self._cfg(key) or None for key in SYSTEM_SENSOR_KEYS}

    def _use_effective(self) -> bool:
        return bool(self._cfg(CONF_USE_EFFECTIVE_SENSORS, DEFAULT_USE_EFFECTIVE_SENSORS))

    # ---- lifecycle ----

    async def async_setup(self) -> None:
        stored = await self._store.async_load()
        if isinstance(stored, dict) and stored:
            _LOGGER.debug(
                "Restored forecast from storage (%d today + %d tomorrow slots)",
                len(stored.get("forecast_today", {})),
                len(stored.get("forecast_tomorrow", {})),
            )
            self.async_set_updated_data(CoordinatorData.from_dict(stored))

        await self._effective_store.async_load()

        manager = await async_get_energy_manager(self.hass)
        self._unsub_listener = manager.async_listen_updates(self._on_energy_manager_update)

        # Backfill effective history once on startup (non-blocking via task)
        pv_sensors = self._active_pv_sensors()
        if pv_sensors:
            self.hass.async_create_task(self._async_backfill_effective())

        await self.async_refresh()

    async def _on_energy_manager_update(self) -> None:
        await self.async_refresh()

    def async_teardown(self) -> None:
        if self._unsub_listener:
            self._unsub_listener()
            self._unsub_listener = None

    async def _async_backfill_effective(self) -> None:
        """Run effective history backfill (called once on startup as a task)."""
        pv_sensors = self._active_pv_sensors()
        history_days = self._cfg(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
        system_cfg = self._system_sensor_cfg()
        await self._effective_store.async_backfill_if_needed(pv_sensors, system_cfg, history_days)

    def _compute_current_effective(
        self,
        pv_sensors: list[str],
        pv_units: dict[str, str],
    ) -> dict[str, float]:
        """Compute effective power per string from current HA state values."""
        system_cfg = self._system_sensor_cfg()

        def _state_val(entity_id: str | None) -> float:
            if not entity_id:
                return 0.0
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ("unknown", "unavailable"):
                return 0.0
            try:
                return float(state.state)
            except (ValueError, TypeError):
                return 0.0

        pv_vals: list[float] = []
        for eid in pv_sensors:
            raw = _state_val(eid)
            unit = pv_units.get(eid, "W")
            # Normalise to W equivalent for loss calculation
            from .units import _TO_WH, _SLOT_H

            if unit == "W":
                pv_vals.append(max(0.0, raw))
            elif unit == "kW":
                pv_vals.append(max(0.0, raw * 1000.0))
            else:
                # Energy sensors: convert Wh/slot → W equivalent
                factor = _TO_WH.get(unit, 1.0)
                pv_vals.append(max(0.0, raw * factor / _SLOT_H))

        def _sys(conf_key: str) -> float:
            return _state_val(system_cfg.get(conf_key))

        grid_import_raw = _sys(CONF_GRID_IMPORT)
        grid_export_raw = _sys(CONF_GRID_EXPORT)
        battery_import_raw = _sys(CONF_BATTERY_IMPORT)
        battery_export_raw = _sys(CONF_BATTERY_EXPORT)

        grid_import_in, _ = split_combined_sensor(grid_import_raw)
        grid_export_out = max(0.0, grid_export_raw)
        battery_import_out = max(0.0, battery_import_raw)
        battery_export_in = max(0.0, battery_export_raw)

        effective = compute_effective_strings(
            pv_vals,
            grid_import=grid_import_in,
            grid_export=grid_export_out,
            battery_import=battery_import_out,
            battery_export=battery_export_in,
        )

        # Convert back to the PV sensor's native unit for display
        result: dict[str, float] = {}
        for i, eid in enumerate(pv_sensors):
            unit = pv_units.get(eid, "W")
            eff_w = effective[i]
            if unit == "W":
                result[eid] = round(eff_w, 2)
            elif unit == "kW":
                result[eid] = round(eff_w / 1000.0, 4)
            else:
                from .units import _SLOT_H, _FROM_WH

                factor = _FROM_WH.get(unit, 1.0)
                result[eid] = round(eff_w * _SLOT_H * factor, 2)
        return result

    # ---- main update ----

    async def _async_update_data(self) -> CoordinatorData:
        try:
            data = await self._build_data()
        except Exception as err:
            raise UpdateFailed(f"Forecast build error: {err}") from err

        if data.forecast_today or data.forecast_tomorrow:
            await self._store.async_save(data.to_dict())
            _LOGGER.debug(
                "Saved: %d today-slots  %d tomorrow-slots  %d strings",
                len(data.forecast_today),
                len(data.forecast_tomorrow),
                len(data.string_forecasts),
            )
        return data

    async def _build_data(self) -> CoordinatorData:
        raw = await fetch_raw_forecast(self.hass)
        pv_sensors = self._active_pv_sensors()

        # --- detect units (cached per entity_id) ---
        fc_sensor = self._cfg(CONF_FC_SENSOR, DEFAULT_FC_SENSOR)
        fc_unit, fc_state_class = await self._cached_unit(fc_sensor)

        pv_units: dict[str, str] = {}
        for pv in pv_sensors:
            unit, _ = await self._cached_unit(pv)
            pv_units[pv] = unit
        if pv_units:
            check_pv_unit_consistency(pv_units)

        # --- compute current effective power per string (always, for sensors) ---
        effective_string_values: dict[str, float] = {}
        if pv_sensors:
            effective_string_values = self._compute_current_effective(pv_sensors, pv_units)

        # --- choose sensor source for correction model ---
        use_effective = self._use_effective() and bool(pv_sensors)

        if not pv_sensors:
            corrected = dict(raw)
            string_forecasts: dict[str, dict[str, float]] = {}
        else:
            corrected, string_forecasts = await self._apply_corrections(
                raw, pv_sensors, fc_unit, pv_units, use_effective=use_effective
            )

        now = dt_util.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        day_after = tomorrow_start + timedelta(days=1)

        forecast_today = {
            ts: wh for ts, wh in corrected.items() if today_start <= parse_dt(ts) < tomorrow_start
        }
        forecast_tomorrow = aggregate_to_hours(
            {ts: wh for ts, wh in corrected.items() if tomorrow_start <= parse_dt(ts) < day_after}
        )

        # Convert internal Wh slots to fc_sensor output unit
        forecast_today_out = wh_to_unit(forecast_today, fc_unit)
        forecast_tomorrow_out = wh_to_unit(forecast_tomorrow, fc_unit)
        string_forecasts_out = {
            eid: wh_to_unit(slots, fc_unit) for eid, slots in string_forecasts.items()
        }
        raw_out = wh_to_unit(raw, fc_unit)

        # today_total and remaining in output unit
        today_total = r(sum(forecast_today_out.values()))
        remaining = r(sum(v for ts, v in forecast_today_out.items() if parse_dt(ts) >= now))

        for needle in ("T12:", "T11:"):
            rv = next((wh for ts, wh in raw.items() if needle in ts), None)
            cv = next((wh for ts, wh in corrected.items() if needle in ts), None)
            if rv is not None and cv is not None:
                _LOGGER.info("Midday slot: raw=%.2f Wh  corrected=%.2f Wh", rv, cv)
                break

        return CoordinatorData(
            raw_forecast=raw_out,
            forecast_today=forecast_today_out,
            forecast_tomorrow=forecast_tomorrow_out,
            string_forecasts=string_forecasts_out,
            today_total=today_total,
            remaining=remaining,
            fc_unit=fc_unit,
            fc_state_class=fc_state_class,
            effective_string_values=effective_string_values,
        )

    # ---- correction pipeline ----

    async def _apply_corrections(
        self,
        raw: dict[str, float],
        pv_sensors: list[str],
        fc_unit: str,
        pv_units: dict[str, str],
        *,
        use_effective: bool = False,
    ) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
        """Fetch statistics, normalise to Wh/slot, delegate to shadylib.

        When *use_effective* is True, the PV sensor history is replaced by the
        cached effective string history (loss-adjusted) before model building.
        """
        algorithm = self._cfg(CONF_ALGORITHM, DEFAULT_ALGORITHM)
        fc_sensor = self._cfg(CONF_FC_SENSOR, DEFAULT_FC_SENSOR)
        history_days = self._cfg(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
        start = dt_util.now() - timedelta(days=history_days)

        try:
            stats = await fetch_statistics(self.hass, [fc_sensor] + pv_sensors, start)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cannot fetch statistics: %s – using raw forecast", err)
            return dict(raw), {}

        fc_rows = to_wh_per_slot(stats.get(fc_sensor, []), fc_unit)

        if use_effective:
            # Replace PV recorder history with effective (loss-adjusted) values
            pv_sensors_rows: dict[str, list[dict]] = {}
            for s in pv_sensors:
                eff_slots = self._effective_store.get_slots(s)
                if eff_slots:
                    # Convert slot dict back to [{start, mean}] rows (already in Wh/slot)
                    eff_rows = [{"start": k, "mean": v} for k, v in sorted(eff_slots.items())]
                    pv_sensors_rows[s] = eff_rows
                else:
                    # Fallback to raw recorder data if no effective cache available
                    _LOGGER.debug("No effective history for %s – falling back to raw PV data", s)
                    pv_sensors_rows[s] = to_wh_per_slot(stats.get(s, []), pv_units.get(s, "W"))
        else:
            pv_sensors_rows = {
                s: to_wh_per_slot(stats.get(s, []), pv_units.get(s, "W")) for s in pv_sensors
            }

        return _shadylib_apply_corrections(raw, fc_rows, pv_sensors_rows, algorithm)
