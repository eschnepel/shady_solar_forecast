"""coordinator.py – Orchestration, data container, lifecycle.

Data pipeline:

1. fetch_raw_forecast()         → raw_forecast: {ISO-ts: Wh}
2. fetch_statistics()           → recorder 5-min means for fc + pv sensors
3. build_bucket_models()        → per-string per-5-min-bucket WLS models
4. _apply_corrections()         → corrected forecast per string + combined
5. Split today (native res.) / tomorrow (hourly aggregation)
6. Compute today_total + remaining directly from 5-min today slots

CoordinatorData fields:
  raw_forecast          : {ISO-ts: Wh}
  forecast_today        : {ISO-ts: Wh}  – native provider resolution (5-min for hourly providers)
  forecast_tomorrow     : {ISO-ts: Wh}  – aggregated to full hours
  string_forecasts      : {entity_id: {ISO-ts: Wh}}
  today_total           : float (Wh)
  remaining             : float (Wh)  – sum of slots whose start >= now (5-min precision)
  string_bucket_models  : {entity_id: BucketModels}  – fitted bucket models per string
  bucket_models_timestamp : str | None  – ISO-8601 UTC of last bucket model fit
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import timedelta, timezone
from typing import Any

from homeassistant.components.energy import (
    async_get_manager as async_get_energy_manager,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
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
    CONF_FILTER_RECORDER_GAPS,
    DEFAULT_FILTER_RECORDER_GAPS,
    CONF_USE_EFFECTIVE_SENSORS,
    DEFAULT_USE_EFFECTIVE_SENSORS,
    SYSTEM_SENSOR_KEYS,
    CACHE_VERSION,
)
from .forecast import fetch_raw_forecast
from .units import (
    check_pv_unit_consistency,
    detect_unit,
    from_wh_per_slot,
    to_wh_per_slot,
    wh_to_unit,
)
from shadylib import apply_corrections as _shadylib_apply_corrections
from shadylib import BucketModels
from shadylib import compute_effective_strings
from shadylib import normalise_em_to_5min, filter_gap_successors
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

# ---------------------------------------------
# Temporary debug helpers – disabled by default
# ---------------------------------------------
_DEBUG_WINDOW_LOGGING_ENABLED = True
_DEBUG_WINDOW_START = (10, 50)  # (hour, minute) inclusive
_DEBUG_WINDOW_END = (12, 10)  # (hour, minute) inclusive


def _rows_to_slots(rows: list[dict]) -> dict[str, float]:
    return {
        (r["start"].isoformat() if hasattr(r["start"], "isoformat") else str(r["start"])): r["mean"]  # noqa: E501
        for r in rows
    }


def _debug_window_slots(
    label: str,
    slots: dict[str, float],
    unit: str = "",
) -> None:
    """Log slot values inside the 10:50–12:10 debug window.

    Filters *slots* to entries whose HH:MM portion falls within the debug
    window (boundaries inclusive) and emits one WARNING line per slot so
    they are visible even at the default log level in Home Assistant.

    Args:
        label:  Stage label printed before each value line.
        slots:  {ISO-timestamp: value} dict (any resolution / timezone).
        unit:   Optional unit string appended to each value (e.g. "Wh", "W").
    """
    if not _DEBUG_WINDOW_LOGGING_ENABLED:
        return
    if isinstance(slots, list):
        slots = _rows_to_slots(slots)
    h_start, m_start = _DEBUG_WINDOW_START
    h_end, m_end = _DEBUG_WINDOW_END
    start_total = h_start * 60 + m_start
    end_total = h_end * 60 + m_end

    found: list[tuple[str, float]] = []
    for ts, val in slots.items():
        # Extract HH:MM regardless of date or timezone suffix
        # ISO strings look like "2025-06-02T10:55", "2025-06-02T10:55:00+02:00", …
        try:
            t_part = ts.split("T", 1)[1] if "T" in ts else ts
            hh, mm = int(t_part[:2]), int(t_part[3:5])
        except (IndexError, ValueError):
            continue
        total = hh * 60 + mm
        if start_total <= total <= end_total:
            found.append((ts, val))

    for ts, val in sorted(found):
        unit_str = f" {unit}" if unit else ""
        _LOGGER.warning("[DEBUG %s] %s → %.4f%s", label, ts, val, unit_str)


def _debug_window_rows(
    label: str,
    rows: list[dict],
    unit: str = "",
) -> None:
    if not _DEBUG_WINDOW_LOGGING_ENABLED:
        return
    slots = _rows_to_slots(rows)
    _debug_window_slots(label, slots, unit)


def _debug_window_message(msg: object, *args: object):
    if _DEBUG_WINDOW_LOGGING_ENABLED:
        _LOGGER.warning(msg, *args)


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
    # Bucket models fitted at last day-start recalculation
    string_bucket_models: dict[str, BucketModels] = field(default_factory=dict)
    # ISO-8601 UTC timestamp of last bucket model fit; None until first fit
    bucket_models_timestamp: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict for the HA store.

        ``string_bucket_models`` and ``bucket_models_timestamp`` are intentionally
        excluded: they are transient runtime state kept in the coordinator's own
        cache (``_cached_bucket_models`` / ``_bucket_models_timestamp``) and are
        re-fitted on the next refresh.  Their keys are ``tuple[int, int]`` which
        is not JSON-serialisable, and persisting them would gain nothing because
        they are always regenerated at day-start anyway.
        """
        d = asdict(self)
        d.pop("string_bucket_models", None)
        d.pop("bucket_models_timestamp", None)
        return d

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
            # Bucket models are not persisted across restarts; refit on next refresh.
            string_bucket_models={},
            bucket_models_timestamp=None,
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
        # Lock to prevent concurrent rebuild-history operations
        self._rebuild_lock = asyncio.Lock()
        # Cached bucket models per string (reset at day-start or on rebuild)
        self._cached_bucket_models: dict[str, BucketModels] = {}
        self._bucket_models_timestamp: str | None = None
        # Unsubscribe for midnight recalculation listener
        self._unsub_midnight: Any = None

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

        # Schedule daily bucket-model recalculation at 00:00:00 local time
        self._unsub_midnight = async_track_time_change(
            self.hass,
            self._on_day_start,
            hour=0,
            minute=0,
            second=0,
        )

        await self.async_refresh()

    async def _on_energy_manager_update(self) -> None:
        await self.async_refresh()

    def async_teardown(self) -> None:
        if self._unsub_listener:
            self._unsub_listener()
            self._unsub_listener = None
        if self._unsub_midnight:
            self._unsub_midnight()
            self._unsub_midnight = None

    async def _on_day_start(self, now: Any) -> None:
        """Called at 00:00:00 local time – invalidate bucket models and refresh."""
        _LOGGER.debug("Day-start event: invalidating bucket models for recalculation")
        self._cached_bucket_models = {}
        self._bucket_models_timestamp = None
        await self.async_refresh()

    async def async_rebuild_history(self) -> None:
        """Rebuild effective-history cache and refit bucket models.

        Steps:
          1. Invalidate effective-history cache (clear all slots, reset cached_until).
          2. Re-run full backfill for the history_days window.
          3. Invalidate stored BucketModels for all strings.
          4. Call async_refresh() which re-fits models and updates all sensors.

        Guarded by an asyncio.Lock to prevent concurrent runs.
        """
        if self._rebuild_lock.locked():
            _LOGGER.warning("async_rebuild_history: already running, skipping")
            return
        async with self._rebuild_lock:
            _LOGGER.info("async_rebuild_history: starting full history rebuild")
            # Step 1: Invalidate effective-history cache
            self._effective_store.invalidate()
            # Step 2: Re-run full backfill
            pv_sensors = self._active_pv_sensors()
            if pv_sensors:
                await self._async_backfill_effective()
            # Step 3: Invalidate bucket models
            self._cached_bucket_models = {}
            self._bucket_models_timestamp = None
            # Step 4: Refresh coordinator data (refits models, updates sensors)
            await self.async_refresh()
            _LOGGER.info("async_rebuild_history: complete")

    async def _async_backfill_effective(self) -> None:
        """Run effective history backfill (called once on startup as a task)."""
        pv_sensors = self._active_pv_sensors()
        history_days = self._cfg(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
        system_cfg = self._system_sensor_cfg()
        await self._effective_store.async_backfill_if_needed(
            pv_sensors,
            system_cfg,
            history_days,
            filter_recorder_gaps=self._cfg(CONF_FILTER_RECORDER_GAPS, DEFAULT_FILTER_RECORDER_GAPS),
        )

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

        # BMS sensor semantics
        # ----------------------
        # Each BMS sensor is bidirectional: positive values indicate the primary
        # direction, negative values indicate the opposite direction within the
        # same 5-minute aggregation slot.
        #
        #   grid_export_raw > 0  → BMS feeds power into the house grid
        #   grid_export_raw < 0  → BMS draws power from the house grid (overlap)
        #   grid_import_raw > 0  → BMS draws power from the house grid
        #   grid_import_raw < 0  → BMS feeds power into the house grid (overlap)
        #
        # Both sensors of a pair can be non-zero within a 5-minute slot due to
        # aggregation of sub-minute switching.  The net directional values are:
        #
        #   grid_export_net = max(0, grid_export_raw) + max(0, -grid_import_raw)
        #   grid_import_net = max(0, grid_import_raw) + max(0, -grid_export_raw)
        #
        # Since max(0,x) - max(0,-x) = x, the full pv_usable expression simplifies
        # to using the raw values directly:
        #
        #   pv_usable = max(0,
        #       grid_export_raw - grid_import_raw
        #     + battery_import_raw - battery_export_raw
        #   )
        #
        # This is passed as grid_export to shadylib so that:
        #   total_loss = pv_sum - pv_usable
        pv_usable = max(
            0.0,
            _sys(CONF_GRID_EXPORT)
            - _sys(CONF_GRID_IMPORT)
            + _sys(CONF_BATTERY_IMPORT)
            - _sys(CONF_BATTERY_EXPORT),
        )

        effective = compute_effective_strings(
            pv_vals,
            grid_export=pv_usable,
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
        _debug_window_slots("1_raw_em_fetch", raw, "Wh(EM-interval)")
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

        # Normalise EM forecast to 5-min slots here so that raw_out uses the
        # same per-slot scale as corrected/forecast_today.  Without this,
        # raw_out would be built from the original EM intervals (e.g. 60-min
        # slots) and wh_to_unit would over-scale by the ratio of the EM
        # interval to 5 min (factor 12 for hourly providers with unit "W").
        raw_normalised_for_output = normalise_em_to_5min(raw)
        _debug_window_slots("1b_raw_normalised_for_output", raw_normalised_for_output, "Wh/slot")

        if not pv_sensors:
            corrected = dict(raw_normalised_for_output)
            string_forecasts: dict[str, dict[str, float]] = {}
        else:
            corrected, string_forecasts = await self._apply_corrections(
                raw, pv_sensors, fc_unit, pv_units, use_effective=use_effective
            )
        _debug_window_slots("4_corrected_wh_slot", corrected, "Wh/slot")

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

        # Convert internal Wh slots to fc_sensor output unit.
        # raw_out uses the 5-min-normalised raw so it is on the same per-slot
        # scale as forecast_today_out (both Wh/5-min before unit conversion).
        forecast_today_out = wh_to_unit(forecast_today, fc_unit)
        forecast_tomorrow_out = wh_to_unit(forecast_tomorrow, fc_unit)
        string_forecasts_out = {
            eid: wh_to_unit(slots, fc_unit) for eid, slots in string_forecasts.items()
        }
        raw_out = wh_to_unit(raw_normalised_for_output, fc_unit)

        _debug_window_slots("5a_raw_out_unit", raw_out, fc_unit)
        _debug_window_slots("5b_forecast_today_out_unit", forecast_today_out, fc_unit)
        for eid, sf_slots in string_forecasts_out.items():
            _debug_window_slots(f"5c_string_out[{eid}]_unit", sf_slots, fc_unit)

        # Sum in Wh first (forecast_today holds Wh/slot), then convert the
        # scalar to fc_unit.  Summing forecast_today_out (already in W/slot)
        # would give a meaningless W-sum scaled by the number of slots.
        today_total_wh = sum(forecast_today.values())
        remaining_wh = sum(v for ts, v in forecast_today.items() if parse_dt(ts) >= now)
        today_total = r(from_wh_per_slot(today_total_wh, fc_unit))
        remaining = r(from_wh_per_slot(remaining_wh, fc_unit))

        _debug_window_message(
            "[DEBUG 6_totals] today_total=%.4f %s  remaining=%.4f %s",
            today_total,
            fc_unit,
            remaining,
            fc_unit,
        )

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
            string_bucket_models=self._cached_bucket_models,
            bucket_models_timestamp=self._bucket_models_timestamp,
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

        When *use_effective* is True, the effective-history cache is consulted
        first for each PV string.  Only strings without a cache entry fall back
        to raw recorder data and are included in the ``fetch_statistics`` call.
        The fc_sensor is always fetched via recorder.

        This means ``fetch_statistics`` is called with the minimal set of IDs:
        - always: fc_sensor
        - only when use_effective=False, or as fallback for uncached strings:
          the relevant pv_sensor IDs

        Bucket models are only re-fitted when the internal cache is empty
        (i.e. after a day-start event or an explicit rebuild).  On subsequent
        intra-day refreshes the stored models are reused and the bucket-model
        timestamp is left unchanged.

        Training data cutoff: rows whose ``start`` is >= today_start (00:00:00
        local time) are excluded from model fitting so that intra-day corrections
        do not shift the coefficients mid-day.
        """
        algorithm = self._cfg(CONF_ALGORITHM, DEFAULT_ALGORITHM)
        fc_sensor = self._cfg(CONF_FC_SENSOR, DEFAULT_FC_SENSOR)
        history_days = self._cfg(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
        start = dt_util.now() - timedelta(days=history_days)

        # --- Determine which PV sensor IDs actually need recorder statistics ---
        # When use_effective=True, strings with a warm effective-history cache
        # do not need a recorder fetch.  Strings without cache (e.g. first run
        # or after a string was added) fall back to recorder and are included.
        if use_effective:
            pv_sensors_rows: dict[str, list[dict]] = {}
            recorder_pv_ids: list[str] = []
            for s in pv_sensors:
                eff_slots = self._effective_store.get_slots(s)
                if eff_slots:
                    # Convert slot dict → [{start, mean}] rows (already Wh/slot).
                    # parse_dt() converts the ISO-string keys to datetime objects
                    # so that rows are type-compatible with recorder-sourced rows
                    # (both paths use datetime for the "start" field).
                    pv_sensors_rows[s] = [
                        {"start": parse_dt(k), "mean": v} for k, v in sorted(eff_slots.items())
                    ]
                else:
                    _LOGGER.debug(
                        "No effective history for %s – will fetch recorder data as fallback", s
                    )
                    recorder_pv_ids.append(s)
        else:
            pv_sensors_rows = {}  # filled after fetch below
            recorder_pv_ids = list(pv_sensors)

        ids_to_fetch: list[str] = [fc_sensor] + recorder_pv_ids

        try:
            stats = await fetch_statistics(self.hass, ids_to_fetch, start)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cannot fetch statistics: %s – using raw forecast", err)
            return dict(raw), {}

        # Optionally discard the first sample after any downtime gap wider
        # than one slot before converting to Wh/slot. The HA recorder may
        # have accumulated all missing values into that sample, which would
        # skew bucket-model statistics (CONF_FILTER_RECORDER_GAPS).
        _filter_gaps = self._cfg(CONF_FILTER_RECORDER_GAPS, DEFAULT_FILTER_RECORDER_GAPS)
        raw_stats: dict[str, list[dict]] = {
            eid: (filter_gap_successors(rows) if _filter_gaps else rows)
            for eid, rows in stats.items()
        }
        fc_rows = to_wh_per_slot(raw_stats.get(fc_sensor, []), fc_unit)

        _debug_window_rows(
            f"3a_fc_rows_wh_slot[unit={fc_unit}]",
            fc_rows,
            "Wh/slot",
        )

        # Fill in recorder-fetched rows for strings that needed it
        for s in recorder_pv_ids:
            pv_sensors_rows[s] = to_wh_per_slot(raw_stats.get(s, []), pv_units.get(s, "W"))
            _debug_window_rows(
                f"3b_pv_rows_wh_slot[{s},unit={pv_units.get(s, 'W')}]",
                pv_sensors_rows[s],
                "Wh/slot",
            )

        # Normalise the EM forecast (arbitrary-interval timestamps) to a
        # complete 5-minute Wh/slot raster before passing to the model.
        # This ensures prediction inputs match the training scale (Wh/slot).
        raw_normalised = normalise_em_to_5min(raw)
        _debug_window_slots("2_raw_normalised_5min", raw_normalised, "Wh/slot")

        # --- Training data cutoff: exclude today's slots from model fitting ---
        # Rows from today (start >= 00:00 local) are not used for bucket model
        # training to prevent intra-day distortions.  They are still used for
        # the corrected forecast output (see below).
        today_start_utc = (
            dt_util.now()
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .astimezone(timezone.utc)
        )
        fc_rows_for_model = [r for r in fc_rows if r["start"] < today_start_utc]
        pv_sensors_rows_for_model: dict[str, list[dict]] = {
            eid: [r for r in rows if r["start"] < today_start_utc]
            for eid, rows in pv_sensors_rows.items()
        }

        # --- Reuse cached bucket models when already fitted today ---
        if self._cached_bucket_models:
            _LOGGER.debug("Reusing cached bucket models (intra-day refresh)")
            # Re-apply existing models using full (including today) fc_rows so that
            # today's corrected forecast benefits from current raw data.
            combined, string_forecasts, _new_models = _shadylib_apply_corrections(
                raw_normalised,
                fc_rows,
                pv_sensors_rows,
                algorithm,
            )
            # Overwrite string_bucket_models back onto coordinator – they were
            # already cached; the new _new_models from this call are discarded
            # since we passed full rows (not cut-off) so they may include today.
            return combined, string_forecasts

        # --- Fit new bucket models using pre-cutoff training rows ---
        combined, string_forecasts, new_models = _shadylib_apply_corrections(
            raw_normalised,
            fc_rows_for_model,
            pv_sensors_rows_for_model,
            algorithm,
        )

        # Cache newly fitted models and record timestamp
        self._cached_bucket_models = new_models
        self._bucket_models_timestamp = (
            dt_util.utcnow().replace(microsecond=0).isoformat()
            if new_models
            else self._bucket_models_timestamp
        )

        # Now generate corrected forecast using full rows (including today) so
        # today's slots get the benefit of already-fitted models.
        if new_models:
            combined, string_forecasts, _ = _shadylib_apply_corrections(
                raw_normalised,
                fc_rows,
                pv_sensors_rows,
                algorithm,
            )

        return combined, string_forecasts
