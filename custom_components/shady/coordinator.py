"""Coordinator for Shady.

Data pipeline:

1. Fetch raw solar forecast  →  raw_forecast: {ISO-ts: Wh}

2. For each configured PV string (up to 4), fetch recorder statistics
   (hourly mean) over the last N days for fc_sensor and pv_sensor_i.

3. Build per-string hourly correction models using the configured algorithm:

   FACTOR (mean ratio per hour-of-day):
     factor(H) = avg(pv_actual(H)) / avg(fc_reference(H))
     predict(H, x) = x * factor(H)

   LINEAR (WLS per hour-of-day):
     pv_actual(H) ~ slope(H) * fc_reference(H) + intercept(H)
     predict(H, x) = slope(H) * x + intercept(H)

   QUADRATIC (WLS per hour-of-day):
     pv_actual(H) ~ a(H)*fc² + b(H)*fc + c(H)
     predict(H, x) = a(H)*x² + b(H)*x + c(H)

   All algorithms use:
   - Neighbour smoothing: observations from H±1 at 50 % weight
   - Z-score normalisation: unit-agnostic (fc in W, forecast in Wh)

4. Apply per-string hourly models:
     corrected_i[ts] = predict(models_i[hour(ts)], raw_forecast[ts])
   Slots without a fitted model for their hour are omitted.
   Sum all strings → forecast[ts]
   Per-string forecasts stored separately in string_forecasts.

5. Aggregate daily totals:
     today_total   – sum of all corrected slots for today
     remaining     – sum of corrected slots from now until end of today

CoordinatorData fields:
  raw_forecast     : {ISO-ts: Wh}
  forecast         : {ISO-ts: Wh}              – summed corrected forecast
  string_forecasts : {sensor_entity_id: {ISO-ts: Wh}}
  today_total      : float (Wh)
  remaining        : float (Wh)

Persistence: last successful result is saved to HA storage and restored
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

_FALLBACK_INTERVAL = timedelta(hours=1)
_STORAGE_KEY = f"{DOMAIN}.last_forecast"
_STORAGE_VERSION = 1
_NEIGHBOUR_WEIGHT = 0.5


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class CoordinatorData:
    raw_forecast     : dict[str, float]            = field(default_factory=dict)
    forecast         : dict[str, float]            = field(default_factory=dict)
    string_forecasts : dict[str, dict[str, float]] = field(default_factory=dict)
    today_total      : float                       = 0.0
    remaining        : float                       = 0.0

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
        stored = await self._store.async_load()
        if isinstance(stored, dict) and stored:
            _LOGGER.debug("Restored forecast from storage (%d slots)",
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

        now            = dt_util.now()
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

    # ---- step 2: per-string correction ----

    async def _apply_corrections(
        self, raw: dict[str, float], pv_sensors: list[str]
    ) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
        """Build one hourly model per string using the configured algorithm."""
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
            models  = _build_hourly_models(fc_rows, pv_rows, algorithm)

            if not models:
                _LOGGER.warning(
                    "No hourly models for %s (algorithm=%s, fc_rows=%d, pv_rows=%d) – skipping",
                    pv_sensor, algorithm, len(fc_rows), len(pv_rows),
                )
                continue

            _LOGGER.info(
                "Hourly models for %s: algorithm=%s  %d hour-buckets  "
                "fc_rows=%d  pv_rows=%d  "
                "fc_range=[%.1f, %.1f]  pv_range=[%.1f, %.1f]",
                pv_sensor, algorithm, len(models),
                len(fc_rows), len(pv_rows),
                min((r["mean"] for r in fc_rows), default=0),
                max((r["mean"] for r in fc_rows), default=0),
                min((r["mean"] for r in pv_rows), default=0),
                max((r["mean"] for r in pv_rows), default=0),
            )
            for h in sorted(models):
                _LOGGER.debug("  hour %02d: %s", h, models[h])

            string_slots: dict[str, float] = {}
            for iso_ts, raw_wh in raw.items():
                try:
                    hour = datetime.fromisoformat(iso_ts).hour
                except ValueError:
                    continue
                if hour not in models:
                    # No training data for this hour (e.g. night) → default 0
                    predicted = 0.0
                else:
                    predicted = round(max(0.0, _predict(models[hour], raw_wh)), 1)
                string_slots[iso_ts] = predicted
                combined[iso_ts]     = round(combined.get(iso_ts, 0.0) + predicted, 1)
            string_forecasts[pv_sensor] = string_slots

        if not combined:
            _LOGGER.debug("All string models failed – falling back to raw forecast")
            return dict(raw), {}

        # Diagnostic: compare midday raw vs corrected
        for ts_needle in ("T12:", "T11:"):
            midday_r = next((wh for ts, wh in raw.items()      if ts_needle in ts), None)
            midday_c = next((wh for ts, wh in combined.items() if ts_needle in ts), None)
            if midday_r is not None and midday_c is not None:
                _LOGGER.info("  → Midday slot: raw=%.1f Wh  corrected=%.1f Wh", midday_r, midday_c)
                break

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
# Model types
# ---------------------------------------------------------------------------
#
# Models are stored as tuples whose length identifies the algorithm:
#
#   FACTOR      : (factor,)              – single float
#   LINEAR      : (slope, intercept)     – 2-tuple
#   QUADRATIC   : (a, b, c)              – 3-tuple
#
# All values operate in original (back-transformed) units.

# {hour-of-day: model_tuple}
HourlyModels = dict[int, tuple]


def _predict(model: tuple, x: float) -> float:
    """Apply a model tuple to a raw forecast value x."""
    if len(model) == 1:
        # FACTOR: y = factor * x
        return model[0] * x
    if len(model) == 2:
        # LINEAR: y = slope * x + intercept
        slope, intercept = model
        return slope * x + intercept
    # QUADRATIC: y = a*x² + b*x + c
    a, b, c = model
    return a * x * x + b * x + c


# ---------------------------------------------------------------------------
# Model builder – dispatches to algorithm-specific fitters
# ---------------------------------------------------------------------------

def _build_hourly_models(
    fc_rows: list[dict],
    pv_rows: list[dict],
    algorithm: str,
) -> HourlyModels:
    """Build one model per hour-of-day using the requested algorithm.

    Common pre-processing for all algorithms:
    - Keys are datetime objects (no ISO re-parsing in inner loops)
    - Observations grouped into 24 hour-of-day buckets
    - Neighbour smoothing: H±1 observations added at 50 % weight
    - Z-score normalisation per bucket (unit-agnostic)
    """
    fc_map: dict[datetime, float] = {r["start"]: r["mean"] for r in fc_rows}
    pv_map: dict[datetime, float] = {r["start"]: r["mean"] for r in pv_rows}

    common = sorted(set(fc_map) & set(pv_map))
    if not common:
        return {}

    # Group into hour-of-day buckets with neighbour smoothing
    # {hour: [(fc_val, pv_val, weight), ...]}
    buckets: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
    for dt in common:
        h = dt.hour
        buckets[h].append((fc_map[dt], pv_map[dt], 1.0))
        for delta_h in (-1, +1):
            nb = dt + timedelta(hours=delta_h)
            if nb in fc_map and nb in pv_map:
                buckets[nb.hour].append((fc_map[nb], pv_map[nb], _NEIGHBOUR_WEIGHT))

    models: HourlyModels = {}
    for hour, obs in buckets.items():
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
            models[hour] = model

    return models


# ---------------------------------------------------------------------------
# Algorithm 1: Factor  –  factor(H) = weighted_avg(pv) / weighted_avg(fc)
# ---------------------------------------------------------------------------

def _fit_factor(
    xs: list[float], ys: list[float], ws: list[float]
) -> tuple | None:
    """Per-hour mean ratio: factor = avg_w(pv) / avg_w(fc).

    No normalisation needed – the ratio is inherently unit-agnostic.
    Returns (factor,) so _predict can identify it by length.
    """
    sw  = sum(ws)
    if sw == 0:
        return None
    mu_x = sum(w * x for w, x in zip(ws, xs)) / sw
    mu_y = sum(w * y for w, y in zip(ws, ys)) / sw
    if mu_x < 1e-9:
        return (0.0,)
    return (round(mu_y / mu_x, 8),)


# ---------------------------------------------------------------------------
# Algorithm 2: Linear  –  pv ~ slope*fc + intercept  (Z-score WLS)
# ---------------------------------------------------------------------------

def _fit_linear(
    xs: list[float], ys: list[float], ws: list[float]
) -> tuple | None:
    """WLS linear regression with Z-score normalisation.

    Returns (slope, intercept) in original units.
    """
    sw   = sum(ws)
    mu_x = sum(w * x for w, x in zip(ws, xs)) / sw
    mu_y = sum(w * y for w, y in zip(ws, ys)) / sw
    sd_x = (sum(w * (x - mu_x) ** 2 for w, x in zip(ws, xs)) / sw) ** 0.5
    sd_y = (sum(w * (y - mu_y) ** 2 for w, y in zip(ws, ys)) / sw) ** 0.5

    if sd_x < 1e-9 or sd_y < 1e-9:
        return (0.0, round(mu_y, 4))

    xs_n = [(x - mu_x) / sd_x for x in xs]
    ys_n = [(y - mu_y) / sd_y for y in ys]

    result = _wls2(xs_n, ys_n, ws)
    if result is None:
        return None

    slope_n, _ = result
    slope_orig     = slope_n * (sd_y / sd_x)
    intercept_orig = mu_y - slope_orig * mu_x
    return (round(slope_orig, 8), round(intercept_orig, 4))


# ---------------------------------------------------------------------------
# Algorithm 3: Quadratic  –  pv ~ a*fc² + b*fc + c  (Z-score WLS)
# ---------------------------------------------------------------------------

def _fit_quadratic(
    xs: list[float], ys: list[float], ws: list[float]
) -> tuple | None:
    """WLS quadratic regression with Z-score normalisation.

    Fits pv ~ a*fc² + b*fc + c in normalised space, then back-transforms
    the coefficients to original units so _predict receives raw Wh values.

    Returns (a, b, c) in original units.
    """
    if len(xs) < 3:
        # Fall back to linear if not enough points for quadratic
        return _fit_linear(xs, ys, ws)

    sw   = sum(ws)
    mu_x = sum(w * x for w, x in zip(ws, xs)) / sw
    mu_y = sum(w * y for w, y in zip(ws, ys)) / sw
    sd_x = (sum(w * (x - mu_x) ** 2 for w, x in zip(ws, xs)) / sw) ** 0.5
    sd_y = (sum(w * (y - mu_y) ** 2 for w, y in zip(ws, ys)) / sw) ** 0.5

    if sd_x < 1e-9 or sd_y < 1e-9:
        return (0.0, 0.0, round(mu_y, 4))

    xs_n = [(x - mu_x) / sd_x for x in xs]
    ys_n = [(y - mu_y) / sd_y for y in ys]

    result = _wls3(xs_n, ys_n, ws)
    if result is None:
        return _fit_linear(xs, ys, ws)  # graceful fallback

    a_n, b_n, c_n = result

    # Back-transform:  y_orig = sd_y * y_n + mu_y,  x_n = (x_orig - mu_x) / sd_x
    # y_n = a_n*x_n² + b_n*x_n + c_n
    # substituting x_n:
    #   y_orig = sd_y*(a_n*((x-mu_x)/sd_x)² + b_n*(x-mu_x)/sd_x + c_n) + mu_y
    #          = (a_n*sd_y/sd_x²)*x² + (b_n*sd_y/sd_x - 2*a_n*sd_y*mu_x/sd_x²)*x
    #            + (a_n*sd_y*mu_x²/sd_x² - b_n*sd_y*mu_x/sd_x + c_n*sd_y + mu_y)
    a_orig = a_n * sd_y / (sd_x ** 2)
    b_orig = (b_n * sd_y / sd_x) - (2 * a_n * sd_y * mu_x / sd_x ** 2)
    c_orig = (a_n * sd_y * mu_x ** 2 / sd_x ** 2
              - b_n * sd_y * mu_x / sd_x
              + c_n * sd_y
              + mu_y)

    return (round(a_orig, 10), round(b_orig, 8), round(c_orig, 4))


# ---------------------------------------------------------------------------
# WLS solvers
# ---------------------------------------------------------------------------

def _wls2(
    xs: list[float], ys: list[float], ws: list[float]
) -> tuple[float, float] | None:
    """Weighted least squares for linear model → (slope, intercept)."""
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


def _wls3(
    xs: list[float], ys: list[float], ws: list[float]
) -> tuple[float, float, float] | None:
    """Weighted least squares for quadratic model → (a, b, c).

    Solves the 3×3 normal equations:
      [Σw    Σwx   Σwx² ] [c]   [Σwy  ]
      [Σwx   Σwx²  Σwx³ ] [b] = [Σwxy ]
      [Σwx²  Σwx³  Σwx⁴] [a]   [Σwx²y]
    """
    sw    = sum(ws)
    swx   = sum(w * x         for w, x    in zip(ws, xs))
    swx2  = sum(w * x**2      for w, x    in zip(ws, xs))
    swx3  = sum(w * x**3      for w, x    in zip(ws, xs))
    swx4  = sum(w * x**4      for w, x    in zip(ws, xs))
    swy   = sum(w * y         for w, y    in zip(ws, ys))
    swxy  = sum(w * x * y     for w, x, y in zip(ws, xs, ys))
    swx2y = sum(w * x**2 * y  for w, x, y in zip(ws, xs, ys))

    # Cramer's rule on the 3×3 system
    M = [
        [sw,   swx,  swx2],
        [swx,  swx2, swx3],
        [swx2, swx3, swx4],
    ]
    rhs = [swy, swxy, swx2y]

    det = _det3(M)
    if abs(det) < 1e-12:
        return None

    c = _det3(_col_replace(M, 0, rhs)) / det
    b = _det3(_col_replace(M, 1, rhs)) / det
    a = _det3(_col_replace(M, 2, rhs)) / det

    return round(a, 10), round(b, 8), round(c, 4)


def _det3(m: list[list[float]]) -> float:
    """3×3 determinant."""
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def _col_replace(
    m: list[list[float]], col: int, vals: list[float]
) -> list[list[float]]:
    """Return a copy of m with column col replaced by vals."""
    return [
        [vals[r] if c == col else m[r][c] for c in range(3)]
        for r in range(3)
    ]


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def _parse_dt(iso_str: str) -> datetime:
    try:
        return datetime.fromisoformat(iso_str)
    except ValueError:
        return datetime.min.replace(tzinfo=dt_util.UTC)
