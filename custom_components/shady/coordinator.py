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
)
from .forecast import fetch_raw_forecast
from .units import (
    check_pv_unit_consistency,
    detect_unit,
    to_wh_per_slot,
    wh_to_unit,
)
from shadylib import apply_corrections as _shadylib_apply_corrections
from shadylib.math_utils import aggregate_to_hours

from shadylib import r, parse_dt
from .statistics import fetch_statistics

_LOGGER = logging.getLogger(__name__)

_FALLBACK_INTERVAL = timedelta(hours=1)
_STORAGE_KEY = f"{DOMAIN}.last_forecast"
_STORAGE_VERSION = 1


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
        )


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class ShadyCoordinator(DataUpdateCoordinator[CoordinatorData]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=_FALLBACK_INTERVAL)
        self._entry = entry
        self._unsub_listener: Any = None
        self._store: Store = Store(hass, _STORAGE_VERSION, _STORAGE_KEY)
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

        manager = await async_get_energy_manager(self.hass)
        self._unsub_listener = manager.async_listen_updates(self._on_energy_manager_update)
        await self.async_refresh()

    async def _on_energy_manager_update(self) -> None:
        await self.async_refresh()

    def async_teardown(self) -> None:
        if self._unsub_listener:
            self._unsub_listener()
            self._unsub_listener = None

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

        if not pv_sensors:
            corrected = dict(raw)
            string_forecasts: dict[str, dict[str, float]] = {}
        else:
            corrected, string_forecasts = await self._apply_corrections(
                raw, pv_sensors, fc_unit, pv_units
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
        )

    # ---- correction pipeline ----

    async def _apply_corrections(
        self,
        raw: dict[str, float],
        pv_sensors: list[str],
        fc_unit: str,
        pv_units: dict[str, str],
    ) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
        """Fetch statistics, normalise to Wh/slot, delegate to shadylib."""
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
        pv_sensors_rows = {
            s: to_wh_per_slot(stats.get(s, []), pv_units.get(s, "W")) for s in pv_sensors
        }

        return _shadylib_apply_corrections(raw, fc_rows, pv_sensors_rows, algorithm)
