"""Sensors for Shady.

Published sensors (entity IDs are prefixed with the device name "shady_" by HA):

  Aggregate (always present):
    solar_forecast_hourly            – corrected Wh for the current 5-min slot
                                       attr 'forecast': today's 288 × 5-min slots {ts: Wh}
                                       attr 'forecast_tomorrow': tomorrow's 288 × 5-min slots
    solar_forecast_today             – total corrected Wh for today
    solar_forecast_remaining         – corrected Wh remaining today (from now, 5-min precision)
    solar_forecast_hourly_raw        – raw (uncorrected) Wh for the current slot
                                       attr 'forecast': today's 288 × 5-min slots {ts: Wh}

  Per configured PV string (one sensor per non-empty pv_sensor_N config key):
    solar_forecast_hourly_<slug>     – corrected Wh current slot for that string
                                       attr 'forecast': today's 288 × 5-min slots {ts: Wh}
                                       attr 'pv_sensor': source entity_id
    solar_<slug>_pv_eff              – effective Wh for the current slot (loss-adjusted)
                                       attr 'pv_sensor': source entity_id

  All sensors report in Wh regardless of the fc_sensor's native unit.
  Current-slot sensors report Wh/5-min-slot; today/remaining report total Wh.

  All forecast attributes contain exactly 288 slots covering 00:00–23:55 of the
  relevant day.  Timestamps are snapped to 5-minute boundaries; night-time slots
  and any gaps are filled with 0.0.
"""

from __future__ import annotations

import importlib.metadata
import re
from datetime import timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from homeassistant.util import dt as dt_util

from .const import CONF_PV_SENSORS, DOMAIN
from .coordinator import CoordinatorData, ShadyCoordinator
from shadylib import normalise_to_5min_day as _normalise_to_5min_day
from shadylib import BucketModels as _BucketModels
from shadylib import parse_dt as _parse_dt


# Read manifest.json once at import time to avoid blocking I/O inside the event loop.
try:
    import json as _json
    import pathlib as _pathlib

    _SHADY_VERSION: str = _json.loads(
        (_pathlib.Path(__file__).parent / "manifest.json").read_text()
    ).get("version", "unknown")
except Exception:
    _SHADY_VERSION = "unknown"

try:
    _SHADYLIB_VERSION: str = importlib.metadata.version("shadylib")
except importlib.metadata.PackageNotFoundError:
    _SHADYLIB_VERSION = "unknown"


def _sw_version() -> str:
    """Return 'shady {ver} / shadylib {ver}' (read at module import time)."""
    return f"shady {_SHADY_VERSION} / shadylib {_SHADYLIB_VERSION}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ShadyCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        SolarForecastCurrentSensor(coordinator, entry),
        SolarForecastTodaySensor(coordinator, entry),
        SolarForecastRemainingSensor(coordinator, entry),
        SolarForecastCurrentRawSensor(coordinator, entry),
    ]

    # One forecast sensor + one effective sensor + one bucket-model sensor per PV string
    d = entry.options or entry.data
    for entity_id in d.get(CONF_PV_SENSORS, []):
        if entity_id:
            entities.append(SolarForecastStringCurrentSensor(coordinator, entry, entity_id))
            entities.append(EffectiveStringSensor(coordinator, entry, entity_id))
            entities.append(BucketModelSensor(coordinator, entry, entity_id))

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class _Base(CoordinatorEntity[ShadyCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ShadyCoordinator,
        entry: ConfigEntry,
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Shady",
            manufacturer="Enrico Schnepel",
            entry_type="service",
            sw_version=_sw_version(),
        )
        # Set initial unit/state_class from defaults; updated on each coordinator refresh
        self._sync_unit_attrs()

    def _sync_unit_attrs(self) -> None:
        """Set fixed Wh unit and energy device class for all forecast sensors.

        All Shady output sensors report in Wh/slot (current-slot sensors) or
        Wh (today_total, remaining).  The fc_sensor unit is kept in
        CoordinatorData for diagnostics but no longer drives sensor metadata.
        """
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR

    def _handle_coordinator_update(self) -> None:
        self._sync_unit_attrs()
        super()._handle_coordinator_update()

    @property
    def _data(self) -> CoordinatorData:
        return self.coordinator.data or CoordinatorData()

    def _current_slot_value(self, slots: dict[str, float]) -> float:
        """Return the value for the current time from a {ts: value} dict.

        All forecast attribute dicts produced by normalise_to_5min_day use
        ``YYYY-MM-DDTHH:MM`` keys (minute precision, no seconds, no TZ offset).

        Lookup order:
          1. Exact 5-min snapped key  (e.g. ``2025-06-02T10:15``)
          2. Any key starting with ``YYYY-MM-DDTHH:`` (current hour fallback)
          3. 0.0                          (night / no data)
        """
        if not slots:
            return 0.0
        now = dt_util.now().replace(second=0, microsecond=0)
        snapped_min = (now.minute // 5) * 5
        snapped = now.replace(minute=snapped_min)

        # 1. Exact 5-min key
        key = snapped.strftime("%Y-%m-%dT%H:%M")
        if key in slots:
            return slots[key]

        # 2. Any slot in current hour (fallback for raw/tomorrow with different granularity)
        hour_prefix = snapped.strftime("%Y-%m-%dT%H:")
        for ts, val in slots.items():
            if ts.startswith(hour_prefix):
                return val

        # 3. No slot for this time (e.g. night, no training data)
        return 0.0


# ---------------------------------------------------------------------------
# Sensor 1: aggregate current slot (corrected)
# ---------------------------------------------------------------------------


class SolarForecastCurrentSensor(_Base):
    _attr_name = "Solar Forecast Current"
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator: ShadyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "fc_current")

    @property
    def native_value(self) -> float | None:
        return self._current_slot_value(self._data.forecast_today)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        now = dt_util.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        return {
            "forecast": _normalise_to_5min_day(self._data.forecast_today, today_start),
            "forecast_tomorrow": _normalise_to_5min_day(
                self._data.forecast_tomorrow, tomorrow_start
            ),
        }


# ---------------------------------------------------------------------------
# Sensor 2: today total
# ---------------------------------------------------------------------------


class SolarForecastTodaySensor(_Base):
    _attr_name = "Solar Forecast Today"
    _attr_icon = "mdi:white-balance-sunny"

    def __init__(self, coordinator: ShadyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "fc_today")

    @property
    def native_value(self) -> float | None:
        d = self._data
        return d.today_total if d.forecast_today else None


# ---------------------------------------------------------------------------
# Sensor 3: remaining today
# ---------------------------------------------------------------------------


class SolarForecastRemainingSensor(_Base):
    _attr_name = "Solar Forecast Remaining"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator: ShadyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "fc_remaining")

    @property
    def native_value(self) -> float | None:
        d = self._data
        return d.remaining if d.forecast_today else None


# ---------------------------------------------------------------------------
# Sensor 4: raw current slot
# ---------------------------------------------------------------------------


class SolarForecastCurrentRawSensor(_Base):
    _attr_name = "Solar Forecast Raw"
    _attr_icon = "mdi:solar-power-variant-outline"

    def __init__(self, coordinator: ShadyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "fc_raw")

    @property
    def native_value(self) -> float | None:
        return self._current_slot_value(self._data.raw_forecast)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        now = dt_util.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return {"forecast": _normalise_to_5min_day(self._data.raw_forecast, today_start)}


# ---------------------------------------------------------------------------
# Sensor 5+: per-string current slot
# ---------------------------------------------------------------------------


def _entity_id_to_slug(entity_id: str) -> str:
    slug = entity_id.split(".", 1)[-1]
    return re.sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")


class SolarForecastStringCurrentSensor(_Base):
    """Corrected forecast for a single PV string, current 5-min slot."""

    _attr_icon = "mdi:solar-panel"

    def __init__(
        self,
        coordinator: ShadyCoordinator,
        entry: ConfigEntry,
        pv_entity_id: str,
    ) -> None:
        slug = _entity_id_to_slug(pv_entity_id)
        super().__init__(coordinator, entry, f"{slug}_fc")
        self._pv_entity_id = pv_entity_id
        self._attr_name = f"Solar String {slug} Forecast"

    @property
    def native_value(self) -> float | None:
        slots = self._data.string_forecasts.get(self._pv_entity_id, {})
        # Per-string dict contains all slots; filter to today for current lookup
        now = dt_util.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        today_slots = {
            ts: wh for ts, wh in slots.items() if today_start <= _parse_dt(ts) < tomorrow_start
        }
        return self._current_slot_value(today_slots)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        now = dt_util.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "forecast": _normalise_to_5min_day(
                self._data.string_forecasts.get(self._pv_entity_id, {}),
                today_start,
            ),
            "pv_sensor": self._pv_entity_id,
        }


# ---------------------------------------------------------------------------
# Sensor 6+: effective string power (loss-adjusted current value)
# ---------------------------------------------------------------------------


class EffectiveStringSensor(_Base):
    """Effective (loss-adjusted) power for a single PV string.

    Entity ID pattern: sensor.shady_<slug>_pv_eff
    Reports in Wh/slot (energy device class, measurement state class).
    """

    _attr_icon = "mdi:solar-panel-large"

    def __init__(
        self,
        coordinator: ShadyCoordinator,
        entry: ConfigEntry,
        pv_entity_id: str,
    ) -> None:
        slug = _entity_id_to_slug(pv_entity_id)
        super().__init__(coordinator, entry, f"{slug}_pv_eff")
        self._pv_entity_id = pv_entity_id
        self._attr_name = f"Solar String {slug} Effective"

    @property
    def native_value(self) -> float | None:
        return self._data.effective_string_values.get(self._pv_entity_id)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"pv_sensor": self._pv_entity_id}


# ---------------------------------------------------------------------------
# Diagnostic sensor: bucket models per PV string
# ---------------------------------------------------------------------------


class BucketModelSensor(CoordinatorEntity[ShadyCoordinator], SensorEntity):
    """Diagnostic sensor exposing the fitted bucket models for a single PV string.

    native_value: ISO-8601 UTC timestamp of the last successful bucket model fit,
                  or None if no models have been computed yet.

    extra_state_attributes: dict mapping "HH:MM" bucket keys to model coefficient
                            tuples, e.g. {"08:00": [0.912], "12:00": [0.743, 12.5]}.
    """

    _attr_icon = "mdi:chart-scatter-plot"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = None
    _attr_device_class = None
    _attr_state_class = None

    def __init__(
        self,
        coordinator: ShadyCoordinator,
        entry: ConfigEntry,
        pv_entity_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._pv_entity_id = pv_entity_id
        slug = _entity_id_to_slug(pv_entity_id)
        self._attr_unique_id = f"{entry.entry_id}_{slug}_bucket_models"
        self._attr_name = f"Solar String {slug} Bucket Models"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
        )

    @property
    def _data(self) -> CoordinatorData:
        return self.coordinator.data or CoordinatorData()

    @property
    def native_value(self) -> str | None:
        """ISO-8601 UTC timestamp of last bucket model fit, or None."""
        models = self._data.string_bucket_models.get(self._pv_entity_id)
        if not models:
            return None
        return self._data.bucket_models_timestamp

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Bucket model coefficients keyed by 'HH:MM' bucket label."""
        models: _BucketModels = self._data.string_bucket_models.get(self._pv_entity_id, {})
        return {
            f"{hour:02d}:{minute:02d}": list(coeff)
            for (hour, minute), coeff in sorted(models.items())
        }
