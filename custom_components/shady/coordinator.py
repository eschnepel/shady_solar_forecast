"""Coordinator for Shady.

Data pipeline:

1. Fetch raw solar forecast  →  raw_forecast: {ISO-ts: Wh}
   Slots are passed through at native provider resolution (hourly or sub-hourly).

2. Fetch recorder statistics (5-minute mean) over the last N days for
   fc_sensor and each pv_sensor_i.

3. Build per-string per-5-minute-bucket correction models:

   Bucket key: (hour, minute_bucket) where minute_bucket ∈ {0,5,10,…,55}

   Neighbour smoothing weights:
     full observation (self):         1.0
     direct neighbour  (±5 min):      0.8
     second neighbour  (±10 min):     0.3

   Algorithms (all per-bucket, plain WLS on original values):
     FACTOR      factor(B) = avg_w(pv) / avg_w(fc)
     LINEAR      pv ~ slope(B)*fc + intercept(B)
     QUADRATIC   pv ~ a(B)*fc² + b(B)*fc + c(B)

4. Apply per-string models:
     corrected_i[ts] = predict(model_i[bucket(ts)], raw[ts])
   Buckets without a model default to 0.0.
   Sum all strings → forecast[ts].

5. Split into today (5-min resolution) and tomorrow (hourly aggregation):
     forecast_today     : {ISO-ts: Wh}  – native 5-min or provider resolution
     forecast_tomorrow  : {ISO-hour-ts: Wh}  – summed into full hours
     today_total        : float (Wh)
     remaining          : float (Wh)

Precision: all Wh values stored at 2 decimal places.

Persistence: CoordinatorData is saved to HA storage on every successful
update and restored on restart.
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
    CONF_ALGORITHM,
    DEFAULT_FC_SENSOR,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_ALGORITHM,
    ALGORITHM_FACTOR,
    ALGORITHM_LINEAR,
    ALGORITHM_QUADRATIC,
    PV_SENSOR_KEYS,
)

_LOGGER = logging.getLogger(__name__)

_FALLBACK_INTERVAL  = timedelta(hours=1)
_STORAGE_KEY        = f"{DOMAIN}.last_forecast"
_STORAGE_VERSION    = 1
_PRECISION          = 2        # decimal places for all Wh values

# Neighbour smoothing weights for 5-min bucket regression
_W_SELF     = 1.0   # the observation itself
_W_NEAR     = 0.8   # ±5 min neighbour
_W_FAR      = 0.3   # ±10 min neighbour

# Bucket size in minutes
_BUCKET_MIN = 5


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class CoordinatorData:
    raw_forecast      : dict[str, float]            = field(default_factory=dict)
    forecast_today    : dict[str, float]            = field(default_factory=dict)
    forecast_tomorrow : dict[str, float]            = field(default_factory=dict)
    string_forecasts  : dict[str, dict[str, float]] = field(default_factory=dict)
    today_total       : float                       = 0.0
    remaining         : float                       = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CoordinatorData":
        return cls(
            raw_forecast      = d.get("raw_forecast", {}),
            forecast_today    = d.get("forecast_today", {}),
            forecast_tomorrow = d.get("forecast_tomorrow", {}),
            string_forecasts  = d.get("string_forecasts", {}),
            today_total       = float(d.get("today_total", 0.0)),
            remaining         = float(d.get("remaining", 0.0)),
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

    def _cfg(self, key: str, default: Any = None) -> Any:
        d = self._entry.options or self._entry.data
        return d.get(key, default)

    def _active_pv_sensors(self) -> list[str]:
        return [s for k in PV_SENSOR_KEYS if (s := self._cfg(k, ""))]

    # ---- lifecycle ----

    async def async_setup(self) -> None:
        stored = await self._store.async_load()
        if isinstance(stored, dict) and stored:
            _LOGGER.debug("Restored forecast from storage (%d today + %d tomorrow slots)",
                          len(stored.get("forecast_today", {})),
                          len(stored.get("forecast_tomorrow", {})))
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
                "Saved: %d today-slots (5-min)  %d tomorrow-slots (hourly)  %d strings",
                len(data.forecast_today), len(data.forecast_tomorrow),
                len(data.string_forecasts),
            )
        return data

    async def _build_data(self) -> CoordinatorData:
        raw        = await self._fetch_raw_forecast()
        pv_sensors = self._active_pv_sensors()

        if not pv_sensors:
            corrected        = dict(raw)
            string_forecasts : dict[str, dict[str, float]] = {}
        else:
            corrected, string_forecasts = await self._apply_corrections(raw, pv_sensors)

        now            = dt_util.now()
        today_start    = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        day_after      = tomorrow_start + timedelta(days=1)

        # Today: keep native resolution (5-min or provider resolution)
        forecast_today = {
            ts: wh for ts, wh in corrected.items()
            if today_start <= _parse_dt(ts) < tomorrow_start
        }

        # Tomorrow: aggregate into full hours
        tomorrow_raw: dict[str, float] = {
            ts: wh for ts, wh in corrected.items()
            if tomorrow_start <= _parse_dt(ts) < day_after
        }
        forecast_tomorrow = _aggregate_to_hours(tomorrow_raw)

        today_total = _r(sum(forecast_today.values()))
        remaining   = _r(sum(
            wh for ts, wh in forecast_today.items()
            if _parse_dt(ts) >= now
        ))

        # Diagnostic
        for ts_needle in ("T12:", "T11:"):
            r = next((wh for ts, wh in raw.items()       if ts_needle in ts), None)
            c = next((wh for ts, wh in corrected.items() if ts_needle in ts), None)
            if r is not None and c is not None:
                _LOGGER.info("Midday slot: raw=%.2f Wh  corrected=%.2f Wh", r, c)
                break

        return CoordinatorData(
            raw_forecast      = raw,
            forecast_today    = forecast_today,
            forecast_tomorrow = forecast_tomorrow,
            string_forecasts  = string_forecasts,
            today_total       = today_total,
            remaining         = remaining,
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
                        slots[iso_str] = _r(slots.get(iso_str, 0.0) + float(wh))
                    except (ValueError, TypeError):
                        continue

        return dict(sorted(slots.items()))

    # ---- step 2: per-string correction ----

    async def _apply_corrections(
        self, raw: dict[str, float], pv_sensors: list[str]
    ) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
        algorithm    = self._cfg(CONF_ALGORITHM, DEFAULT_ALGORITHM)
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

        combined        : dict[str, float]              = {}
        string_forecasts: dict[str, dict[str, float]]   = {}

        for pv_sensor in pv_sensors:
            pv_rows = stats.get(pv_sensor, [])
            models  = _build_bucket_models(fc_rows, pv_rows, algorithm)

            if not models:
                _LOGGER.warning(
                    "No bucket models for %s (algorithm=%s, fc_rows=%d, pv_rows=%d)",
                    pv_sensor, algorithm, len(fc_rows), len(pv_rows),
                )
                continue

            _LOGGER.info(
                "Bucket models for %s: algorithm=%s  %d buckets  "
                "fc_rows=%d  pv_rows=%d  "
                "fc=[%.2f, %.2f]  pv=[%.2f, %.2f]",
                pv_sensor, algorithm, len(models),
                len(fc_rows), len(pv_rows),
                min((r["mean"] for r in fc_rows), default=0),
                max((r["mean"] for r in fc_rows), default=0),
                min((r["mean"] for r in pv_rows), default=0),
                max((r["mean"] for r in pv_rows), default=0),
            )
            for b in sorted(models)[:6]:   # log first 6 buckets only
                _LOGGER.debug("  bucket %02d:%02d → %s", b[0], b[1], models[b])

            string_slots: dict[str, float] = {}
            for iso_ts, raw_wh in raw.items():
                try:
                    dt  = datetime.fromisoformat(iso_ts)
                    key = (dt.hour, _snap(dt.minute))
                except ValueError:
                    continue
                if key not in models:
                    predicted = 0.0
                else:
                    predicted = _r(max(0.0, _predict(models[key], raw_wh)))
                string_slots[iso_ts] = predicted
                combined[iso_ts]     = _r(combined.get(iso_ts, 0.0) + predicted)
            string_forecasts[pv_sensor] = string_slots

        if not combined:
            _LOGGER.debug("All string models failed – falling back to raw forecast")
            return dict(raw), {}

        return dict(sorted(combined.items())), string_forecasts

    async def _fetch_statistics(
        self, statistic_ids: list[str], start: datetime
    ) -> dict[str, list[dict]]:
        """Fetch 5-minute means for all statistic_ids in one recorder call.

        Handles both object-style (r.mean, r.start) and dict-style rows.
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
                period="5minute",
                types={"mean"},
                units=["Wh"],  # energy sensors; power sensors (W) passed through as-is
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
# Model types
#
#   FACTOR     : (factor,)           – 1-tuple
#   LINEAR     : (slope, intercept)  – 2-tuple
#   QUADRATIC  : (a, b, c)           – 3-tuple
#
# Bucket key: (hour: int, minute: int)  where minute ∈ {0,5,10,…,55}
# ---------------------------------------------------------------------------

BucketKey    = tuple[int, int]
BucketModels = dict[BucketKey, tuple]


def _snap(minute: int) -> int:
    """Round a minute value down to the nearest 5-minute boundary."""
    return (minute // _BUCKET_MIN) * _BUCKET_MIN


def _predict(model: tuple, x: float) -> float:
    if len(model) == 1:
        return model[0] * x
    if len(model) == 2:
        slope, intercept = model
        return slope * x + intercept
    a, b, c = model
    return a * x * x + b * x + c


# ---------------------------------------------------------------------------
# Model builder
# ---------------------------------------------------------------------------

def _build_bucket_models(
    fc_rows: list[dict],
    pv_rows: list[dict],
    algorithm: str,
) -> BucketModels:
    """Build one model per (hour, 5-min-bucket) with neighbour smoothing.

    Neighbour weights:
      self       : 1.0
      ±5 min     : 0.8
      ±10 min    : 0.3
    """
    fc_map: dict[datetime, float] = {r["start"]: r["mean"] for r in fc_rows}
    pv_map: dict[datetime, float] = {r["start"]: r["mean"] for r in pv_rows}

    common = sorted(set(fc_map) & set(pv_map))
    if not common:
        return {}

    # Group into (hour, snapped_minute) buckets
    # {BucketKey: [(fc_val, pv_val, weight), ...]}
    buckets: dict[BucketKey, list[tuple[float, float, float]]] = defaultdict(list)

    for dt in common:
        bk = (dt.hour, _snap(dt.minute))
        buckets[bk].append((fc_map[dt], pv_map[dt], _W_SELF))

        # Neighbour smoothing: ±5 min at 0.8, ±10 min at 0.3
        for delta_min, weight in (
            (-10, _W_FAR), (-5, _W_NEAR), (+5, _W_NEAR), (+10, _W_FAR)
        ):
            nb = dt + timedelta(minutes=delta_min)
            if nb in fc_map and nb in pv_map:
                nb_bk = (nb.hour, _snap(nb.minute))
                buckets[nb_bk].append((fc_map[nb], pv_map[nb], weight))

    models: BucketModels = {}
    for bk, obs in buckets.items():
        if len(obs) < 2:
            continue
        xs = [o[0] for o in obs]
        ys = [o[1] for o in obs]
        ws = [o[2] for o in obs]

        if algorithm == ALGORITHM_FACTOR:
            model = _fit_factor(xs, ys, ws)
        elif algorithm == ALGORITHM_QUADRATIC:
            model = _fit_quadratic(xs, ys, ws)
        else:
            model = _fit_linear(xs, ys, ws)

        if model is not None:
            models[bk] = model

    return models


# ---------------------------------------------------------------------------
# Fitters
# ---------------------------------------------------------------------------

def _fit_factor(xs: list[float], ys: list[float], ws: list[float]) -> tuple | None:
    sw   = sum(ws)
    if sw == 0:
        return None
    mu_x = sum(w * x for w, x in zip(ws, xs)) / sw
    mu_y = sum(w * y for w, y in zip(ws, ys)) / sw
    if mu_x < 1e-9:
        return (0.0,)
    return (_r6(mu_y / mu_x),)


def _fit_linear(xs: list[float], ys: list[float], ws: list[float]) -> tuple | None:
    result = _wls2(xs, ys, ws)
    if result is None:
        return None
    slope, intercept = result
    return (_r6(slope), _r(intercept))

"""
def _fit_quadratic(xs: list[float], ys: list[float], ws: list[float]) -> tuple | None:
    if len(xs) < 3:
        return _fit_linear(xs, ys, ws)
    result = _wls3(xs, ys, ws)
    if result is None:
        return _fit_linear(xs, ys, ws)
    a, b, c_coef = result
    return (_r6(a), _r6(b), _r(c_coef))
"""
def _fit_quadratic(xs: list[float], ys: list[float], ws: list[float]) -> tuple | None:
    """WLS quadratic through origin: pv ~ a*fc² + b*fc  (no free intercept).
 
    Fixing the intercept to zero is physically correct (fc=0 → pv=0) and
    prevents the model from memorising the historical mean as a constant offset,
    which caused the corrected forecast to mirror the training data shape
    rather than scaling the current raw forecast.
 
    Falls back to linear if fewer than 3 points or the system is degenerate.
    Returns (a, b, 0.0) so _predict uses the standard quadratic path with c=0.
    """
    if len(xs) < 3:
        return _fit_linear(xs, ys, ws)
    result = _wls2_origin_quad(xs, ys, ws)
    if result is None:
        return _fit_linear(xs, ys, ws)
    a, b = result
    return (_r6(a), _r6(b), 0.0)   # c fixed at 0
 
# ---------------------------------------------------------------------------
# WLS solvers
# ---------------------------------------------------------------------------

def _wls2(xs: list[float], ys: list[float], ws: list[float]) -> tuple[float, float] | None:
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
    return slope, intercept


def _wls2_origin_quad(
    xs: list[float], ys: list[float], ws: list[float]
) -> tuple[float, float] | None:
    """WLS quadratic through origin: y ~ a*x² + b*x  (no intercept).
 
    The 2×2 normal equations are:
      [Σw*x⁴  Σw*x³] [a]   [Σw*x²*y]
      [Σw*x³  Σw*x²] [b] = [Σw*x*y  ]
    """
    swx2  = sum(w * x**2     for w, x    in zip(ws, xs))
    swx3  = sum(w * x**3     for w, x    in zip(ws, xs))
    swx4  = sum(w * x**4     for w, x    in zip(ws, xs))
    swxy  = sum(w * x * y    for w, x, y in zip(ws, xs, ys))
    swx2y = sum(w * x**2 * y for w, x, y in zip(ws, xs, ys))
 
    det = swx4 * swx2 - swx3 ** 2
    if abs(det) < 1e-12:
        return None
 
    a = (swx2y * swx2 - swxy  * swx3) / det
    b = (swxy  * swx4 - swx2y * swx3) / det
    return a, b
 
def _wls3(
    xs: list[float], ys: list[float], ws: list[float]
) -> tuple[float, float, float] | None:
    """WLS quadratic via Cramer's rule on the 3×3 normal equations."""
    sw    = sum(ws)
    swx   = sum(w * x        for w, x    in zip(ws, xs))
    swx2  = sum(w * x**2     for w, x    in zip(ws, xs))
    swx3  = sum(w * x**3     for w, x    in zip(ws, xs))
    swx4  = sum(w * x**4     for w, x    in zip(ws, xs))
    swy   = sum(w * y        for w, y    in zip(ws, ys))
    swxy  = sum(w * x * y    for w, x, y in zip(ws, xs, ys))
    swx2y = sum(w * x**2 * y for w, x, y in zip(ws, xs, ys))

    M   = [[sw, swx, swx2], [swx, swx2, swx3], [swx2, swx3, swx4]]
    rhs = [swy, swxy, swx2y]
    det = _det3(M)
    if abs(det) < 1e-12:
        return None

    c = _det3(_col_replace(M, 0, rhs)) / det
    b = _det3(_col_replace(M, 1, rhs)) / det
    a = _det3(_col_replace(M, 2, rhs)) / det
    return a, b, c


def _det3(m: list[list[float]]) -> float:
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def _col_replace(m: list[list[float]], col: int, vals: list[float]) -> list[list[float]]:
    return [[vals[r] if c == col else m[r][c] for c in range(3)] for r in range(3)]


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def _aggregate_to_hours(slots: dict[str, float]) -> dict[str, float]:
    """Sum sub-hourly slots into full-hour buckets.

    Key is the ISO string of the hour's start (minute/second set to 0).
    """
    hourly: dict[str, float] = {}
    for ts, wh in slots.items():
        try:
            dt  = datetime.fromisoformat(ts)
            key = dt.replace(minute=0, second=0, microsecond=0).isoformat()
        except ValueError:
            continue
        hourly[key] = _r(hourly.get(key, 0.0) + wh)
    return dict(sorted(hourly.items()))


# ---------------------------------------------------------------------------
# Precision helpers
# ---------------------------------------------------------------------------

def _r(v: float) -> float:
    """Round to standard output precision (2 decimal places)."""
    return round(v, _PRECISION)


def _r6(v: float) -> float:
    """Round model coefficients to 6 decimal places."""
    return round(v, 6)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def _parse_dt(iso_str: str) -> datetime:
    try:
        return datetime.fromisoformat(iso_str)
    except ValueError:
        return datetime.min.replace(tzinfo=dt_util.UTC)
