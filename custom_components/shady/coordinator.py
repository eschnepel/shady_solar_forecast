"""Coordinator for Shady.

Data pipeline:

1. Fetch raw solar forecast  →  raw_forecast: {ISO-ts: Wh}

2. For each configured PV string (up to 4), fetch recorder statistics
   (hourly mean) over the last N days for fc_sensor and pv_sensor_i.

3. Build per-string hourly correction models via linear regression:
     For each hour-of-day H (0–23) a separate WLS model is fitted:
       pv_actual(H) ~ slope(H) * fc_reference(H) + intercept(H)
     This captures shading patterns that differ by time of day.

   Neighbour smoothing: observations from H-1 and H+1 are added at 50 %
   weight so hours with few samples borrow strength from adjacent hours.

   Z-score normalisation per bucket makes models unit-agnostic
   (fc_sensor in W, raw forecast in Wh).

4. Apply per-string hourly models:
     corrected_i[ts] = predict(models_i[hour(ts)], raw_forecast[ts])
   Slots without a fitted model for their hour are omitted.
   Sum all strings → forecast[ts]
   Per-string forecasts stored separately in string_forecasts.

5. Aggregate daily totals:
     today_total   – sum of all corrected slots for today
     remaining     – sum of corrected slots from now until end of today

CoordinatorData fields:
  raw_forecast     : {ISO-ts: Wh}              – raw aggregated provider forecast
  forecast         : {ISO-ts: Wh}              – summed corrected forecast
  string_forecasts : {sensor_entity_id: {ISO-ts: Wh}}  – per-string corrected forecasts
  today_total      : float (Wh)
  remaining        : float (Wh)

Persistence: last successful result (raw_forecast, corrected forecast,
per-string forecasts, daily totals) is saved to HA storage and restored
on restart so sensors have values immediately after a reboot.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.energy import async_get_manager as async_get_energy_manager
from homeassistant.components.energy.websocket_api import async_get_energy_platforms
from homeassistant.components.recorder import get_instance as get_recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_FC_SENSOR,
    CONF_HISTORY_DAYS,
    DEFAULT_FC_SENSOR,
    DEFAULT_HISTORY_DAYS,
    PV_SENSOR_KEYS,
)

_LOGGER = logging.getLogger(__name__)

_FALLBACK_INTERVAL = timedelta(hours=1)
_STORAGE_KEY = f"{DOMAIN}.last_forecast"
_STORAGE_VERSION = 1
_NEIGHBOUR_WEIGHT = 0.5


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class CoordinatorData:
    raw_forecast     : dict[str, float]              = field(default_factory=dict)
    forecast         : dict[str, float]              = field(default_factory=dict)
    string_forecasts : dict[str, dict[str, float]]   = field(default_factory=dict)
    today_total      : float                         = 0.0
    remaining        : float                         = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CoordinatorData":
        return cls(
            raw_forecast     = d.get("raw_forecast", {}),
            forecast         = d.get("forecast", {}),
            string_forecasts = d.get("string_forecasts", {}),
            today_total      = float(d.get("today_total", 0.0)),
            remaining        = float(d.get("remaining", 0.0)),
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

    # ---- config helpers ----

    def _cfg(self, key: str, default: Any = None) -> Any:
        d = self._entry.options or self._entry.data
        return d.get(key, default)

    def _active_pv_sensors(self) -> list[str]:
        return [s for k in PV_SENSOR_KEYS if (s := self._cfg(k, ""))]

    # ---- lifecycle ----

    async def async_setup(self) -> None:
        # Restore last known data immediately so sensors have values on restart
        stored = await self._store.async_load()
        if isinstance(stored, dict) and stored:
            _LOGGER.debug("Restored forecast + statistics from storage (%d slots)",
                          len(stored.get("forecast", {})))
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

        # Persist every successful non-empty result (includes per-string data)
        if data.forecast:
            await self._store.async_save(data.to_dict())
            _LOGGER.debug("Saved forecast to storage (%d slots, %d strings)",
                          len(data.forecast), len(data.string_forecasts))

        return data

    async def _build_data(self) -> CoordinatorData:
        raw = await self._fetch_raw_forecast()
        pv_sensors = self._active_pv_sensors()

        if not pv_sensors:
            forecast = dict(raw)
            string_forecasts: dict[str, dict[str, float]] = {}
        else:
            forecast, string_forecasts = await self._apply_corrections(raw, pv_sensors)

        now = dt_util.now()
        today_start    = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)

        today_total = sum(
            wh for ts, wh in forecast.items()
            if today_start <= _parse_dt(ts) < tomorrow_start
        )
        remaining = sum(
            wh for ts, wh in forecast.items()
            if now <= _parse_dt(ts) < tomorrow_start
        )

        return CoordinatorData(
            raw_forecast     = raw,
            forecast         = forecast,
            string_forecasts = string_forecasts,
            today_total      = round(today_total, 1),
            remaining        = round(remaining, 1),
        )

    # ---- step 1: raw forecast ----

    async def _fetch_raw_forecast(self) -> dict[str, float]:
        manager = await async_get_energy_manager(self.hass)
        if manager.data is None:
            return {}

        config_entry_ids: list[str] = []
        for source in manager.data.get("energy_sources", []):
            if source.get("type") != "solar":
                continue
            for eid in source.get("config_entry_solar_forecast") or []:
                if eid not in config_entry_ids:
                    config_entry_ids.append(eid)

        if not config_entry_ids:
            _LOGGER.debug("No solar forecast config entries found")
            return {}

        platforms = await async_get_energy_platforms(self.hass)
        slots: dict[str, float] = {}

        for eid in config_entry_ids:
            ce = self.hass.config_entries.async_get_entry(eid)
            if ce is None:
                continue
            fn = platforms.get(ce.domain)
            if fn is None:
                continue
            try:
                result = await fn(self.hass, eid)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Forecast fetch error (%s): %s", eid, err)
                continue
            if not result:
                continue
            for _, string_slots in result.items():
                if not isinstance(string_slots, dict):
                    continue
                for iso_str, wh in string_slots.items():
                    try:
                        slots[iso_str] = round(slots.get(iso_str, 0.0) + float(wh), 1)
                    except (ValueError, TypeError):
                        continue

        return dict(sorted(slots.items()))

    # ---- step 2: per-string regression + correction ----

    async def _apply_corrections(
        self, raw: dict[str, float], pv_sensors: list[str]
    ) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
        """Build one hourly WLS model per string, return (combined, per_string).

    For each string, up to 24 per-hour models are fitted.  Each forecast
    slot is predicted by the model matching its hour-of-day.  Slots whose
    hour has no fitted model are excluded from the corrected output.
    """
        fc_sensor    = self._cfg(CONF_FC_SENSOR, DEFAULT_FC_SENSOR)
        history_days = self._cfg(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
        start        = dt_util.now() - timedelta(days=history_days)

        all_ids = [fc_sensor] + pv_sensors
        try:
            stats = await self._fetch_statistics(all_ids, start)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Cannot fetch statistics: %s – using raw forecast", err)
            return dict(raw), {}

        fc_rows = stats.get(fc_sensor, [])

        combined      : dict[str, float]              = {}
        string_forecasts: dict[str, dict[str, float]] = {}

        for pv_sensor in pv_sensors:
            pv_rows  = stats.get(pv_sensor, [])
            models   = _build_hourly_models(fc_rows, pv_rows)
            if not models:
                _LOGGER.warning(
                    "No hourly models for %s (fc_rows=%d, pv_rows=%d) – skipping",
                    pv_sensor, len(fc_rows), len(pv_rows),
                )
                continue

            _LOGGER.info(
                "Hourly models for %s: %d hour-buckets fitted  "
                "fc_rows=%d  pv_rows=%d  "
                "fc_range=[%.1f, %.1f]  pv_range=[%.1f, %.1f]",
                pv_sensor, len(models),
                len(fc_rows), len(pv_rows),
                min((r["mean"] for r in fc_rows), default=0),
                max((r["mean"] for r in fc_rows), default=0),
                min((r["mean"] for r in pv_rows), default=0),
                max((r["mean"] for r in pv_rows), default=0),
            )
            # Log per-hour slopes for diagnostic visibility
            for h in sorted(models):
                s, i = models[h]
                _LOGGER.debug("  hour %02d: slope=%.4f  intercept=%.4f", h, s, i)

            string_slots: dict[str, float] = {}
            for iso_ts, raw_wh in raw.items():
                try:
                    hour = datetime.fromisoformat(iso_ts).hour
                except ValueError:
                    continue
                if hour not in models:
                    continue
                slope, intercept = models[hour]
                predicted = round(max(0.0, slope * raw_wh + intercept), 1)
                string_slots[iso_ts]  = predicted
                combined[iso_ts]      = round(combined.get(iso_ts, 0.0) + predicted, 1)
            string_forecasts[pv_sensor] = string_slots

        if not combined:
            _LOGGER.debug("All string models failed – falling back to raw forecast")
            return dict(raw), {}

        # Summary: compare a midday raw slot vs combined corrected
        midday_raw      = {ts: wh for ts, wh in raw.items()      if "T12:" in ts or "T11:" in ts}
        midday_combined = {ts: wh for ts, wh in combined.items() if "T12:" in ts or "T11:" in ts}
        if midday_raw and midday_combined:
            r = next(iter(midday_raw.values()))
            p = next(iter(midday_combined.values()))
            _LOGGER.info("  → Combined midday slot: raw=%.1f Wh  corrected=%.1f Wh", r, p)

        return dict(sorted(combined.items())), string_forecasts

    async def _fetch_statistics(
        self, statistic_ids: list[str], start: datetime
    ) -> dict[str, list[dict]]:
        """Fetch hourly means for all statistic_ids in one recorder call.

        Handles both object-style rows (r.mean, r.start) and dict-style rows
        (r["mean"], r["start"]) depending on the HA version.
        """
        def _mean(r: Any) -> float | None:
            return r.get("mean") if isinstance(r, dict) else getattr(r, "mean", None)

        def _start(r: Any) -> datetime:
            v = r.get("start") if isinstance(r, dict) else getattr(r, "start", None)
            if isinstance(v, datetime):
                return v
            if isinstance(v, str):
                return datetime.fromisoformat(v)
            if isinstance(v, (int, float)):
                return datetime.fromtimestamp(v, tz=dt_util.UTC)
            raise ValueError(f"Cannot parse start value: {v!r}")

        def _query() -> dict[str, list[dict]]:
            result = statistics_during_period(
                self.hass,
                start_time=start,
                end_time=None,
                statistic_ids=statistic_ids,
                period="hour",
                types={"mean"},
                units=["Wh"],  # energy sensors; power sensors (W) are passed through as-is
            )
            return {
                sid: [
                    {"start": _start(r), "mean": _mean(r)}
                    for r in rows
                    if _mean(r) is not None
                ]
                for sid, rows in result.items()
            }

        return await get_recorder(self.hass).async_add_executor_job(_query)


# ---------------------------------------------------------------------------
# Linear regression helpers
# ---------------------------------------------------------------------------

# Type alias: hour-of-day (0–23) → (slope, intercept)
HourlyModel = dict[int, tuple[float, float]]


def _build_hourly_models(
    fc_rows: list[dict],
    pv_rows: list[dict],
) -> HourlyModel:
    """Build one WLS model per hour-of-day (0–23).

    For each hour H a separate regression is fitted:
        pv_actual(H) ~ slope(H) * fc_reference(H) + intercept(H)

    This captures shading effects that are specific to certain times of day
    (e.g. a chimney shadow that only affects string 2 between 09:00–11:00).

    Neighbour smoothing: observations from H-1 and H+1 are added at 50 %
    weight so hours with few training samples borrow strength from adjacent
    hours.

    Z-score normalisation per hour bucket makes the regression unit-agnostic
    (fc_sensor in W, raw forecast in Wh).
    """
    fc_map: dict[datetime, float] = {r["start"]: r["mean"] for r in fc_rows}
    pv_map: dict[datetime, float] = {r["start"]: r["mean"] for r in pv_rows}

    common = sorted(set(fc_map) & set(pv_map))
    if not common:
        return {}

    # Group observations by hour-of-day: {hour: [(fc_val, pv_val, weight), ...]}
    buckets: dict[int, list[tuple[float, float, float]]] = defaultdict(list)

    for dt in common:
        h = dt.hour
        buckets[h].append((fc_map[dt], pv_map[dt], 1.0))
        # Neighbours contribute to adjacent buckets at half weight
        for delta_h in (-1, +1):
            nb = dt + timedelta(hours=delta_h)
            if nb in fc_map and nb in pv_map:
                buckets[nb.hour].append((fc_map[nb], pv_map[nb], _NEIGHBOUR_WEIGHT))

    models: HourlyModel = {}
    for hour, obs in buckets.items():
        if len(obs) < 2:
            continue
        xs = [o[0] for o in obs]
        ys = [o[1] for o in obs]
        ws = [o[2] for o in obs]

        # Z-score normalisation (weighted)
        sw   = sum(ws)
        mu_x = sum(w * x for w, x in zip(ws, xs)) / sw
        mu_y = sum(w * y for w, y in zip(ws, ys)) / sw
        sd_x = (sum(w * (x - mu_x) ** 2 for w, x in zip(ws, xs)) / sw) ** 0.5
        sd_y = (sum(w * (y - mu_y) ** 2 for w, y in zip(ws, ys)) / sw) ** 0.5

        if sd_x < 1e-9 or sd_y < 1e-9:
            # No variance – use mean ratio as constant model (slope=0, intercept=mean_pv)
            models[hour] = (0.0, round(mu_y, 4))
            continue

        xs_n = [(x - mu_x) / sd_x for x in xs]
        ys_n = [(y - mu_y) / sd_y for y in ys]

        result = _wls(xs_n, ys_n, ws)
        if result is None:
            continue

        slope_n, _ = result
        # Back-transform to original units
        slope_orig     = slope_n * (sd_y / sd_x)
        intercept_orig = mu_y - slope_orig * mu_x
        models[hour] = (round(slope_orig, 8), round(intercept_orig, 4))

    return models


def _wls(
    xs: list[float], ys: list[float], ws: list[float]
) -> tuple[float, float] | None:
    """Weighted least squares → (slope, intercept)."""
    sw   = sum(ws)
    if sw == 0:
        return None
    swx  = sum(w * x     for w, x    in zip(ws, xs))
    swy  = sum(w * y     for w, y    in zip(ws, ys))
    swxx = sum(w * x * x for w, x    in zip(ws, xs))
    swxy = sum(w * x * y for w, x, y in zip(ws, xs, ys))

    denom = sw * swxx - swx ** 2
    if abs(denom) < 1e-12:
        return None

    slope     = (sw * swxy - swx * swy) / denom
    intercept = (swy - slope * swx) / sw
    return round(slope, 8), round(intercept, 4)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def _parse_dt(iso_str: str) -> datetime:
    try:
        return datetime.fromisoformat(iso_str)
    except ValueError:
        return datetime.min.replace(tzinfo=dt_util.UTC)
