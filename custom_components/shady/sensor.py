"""Sensors for Shady.

Published sensors (entity IDs are prefixed with the device name "shady_" by HA):

  Aggregate (always present):
    solar_forecast_hourly            – corrected Wh for the current hour
                                       attr 'forecast': full {ts: Wh} dict
    solar_forecast_today             – total corrected Wh for today
    solar_forecast_remaining         – corrected Wh remaining today (from now)

  Per configured PV string (one sensor per non-empty pv_sensor_N config key):
    solar_forecast_hourly_<slug>     – corrected Wh current hour for that string
                                       attr 'forecast': per-string {ts: Wh} dict
                                       attr 'pv_sensor': source entity_id

  Note: slots whose hour-of-day has no fitted regression model are excluded
  from the corrected forecast (no training data for that hour = no prediction).
"""
from __future__ import annotations

import logging
import re

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, PV_SENSOR_KEYS
from .coordinator import CoordinatorData, ShadyCoordinator

_LOGGER = logging.getLogger(__name__)


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
    ]

    # One hourly sensor per configured PV string
    d = entry.options or entry.data
    for key in PV_SENSOR_KEYS:
        entity_id = d.get(key, "")
        if entity_id:
            entities.append(
                SolarForecastStringCurrentSensor(coordinator, entry, key, entity_id)
            )

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------

class _Base(CoordinatorEntity[ShadyCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR

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
        )

    @property
    def _data(self) -> CoordinatorData:
        return self.coordinator.data or CoordinatorData()

    @staticmethod
    def _current_hour_value(slots: dict[str, float]) -> float | None:
        if not slots:
            return None
        now = dt_util.now().replace(minute=0, second=0, microsecond=0)
        key = now.isoformat()
        if key in slots:
            return slots[key]
        # Fallback: match by date+hour prefix (handles timezone format variations)
        prefix = now.strftime("%Y-%m-%dT%H:")
        for ts, wh in slots.items():
            if ts.startswith(prefix):
                return wh
        return None


# ---------------------------------------------------------------------------
# Sensor 1: aggregate current hour
# ---------------------------------------------------------------------------

class SolarForecastCurrentSensor(_Base):
    _attr_name = "Solar Forecast Hourly"
    _attr_icon = "mdi:solar-power"

    def __init__(self, coordinator: ShadyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "solar_forecast_hourly")

    @property
    def native_value(self) -> float | None:
        return self._current_hour_value(self._data.forecast)

    @property
    def extra_state_attributes(self) -> dict:
        return {"forecast": self._data.forecast}


# ---------------------------------------------------------------------------
# Sensor 2: today total
# ---------------------------------------------------------------------------

class SolarForecastTodaySensor(_Base):
    _attr_name = "Solar Forecast Today"
    _attr_icon = "mdi:white-balance-sunny"

    def __init__(self, coordinator: ShadyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "solar_forecast_today")

    @property
    def native_value(self) -> float | None:
        d = self._data
        return d.today_total if d.forecast else None


# ---------------------------------------------------------------------------
# Sensor 3: remaining today
# ---------------------------------------------------------------------------

class SolarForecastRemainingSensor(_Base):
    _attr_name = "Solar Forecast Remaining"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, coordinator: ShadyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "solar_forecast_remaining")

    @property
    def native_value(self) -> float | None:
        d = self._data
        return d.remaining if d.forecast else None


# ---------------------------------------------------------------------------
# Sensor 4+: per-string current hour
# ---------------------------------------------------------------------------

def _entity_id_to_slug(entity_id: str) -> str:
    """sensor.my_pv_string_1  →  my_pv_string_1"""
    slug = entity_id.split(".", 1)[-1]          # strip domain prefix
    slug = re.sub(r"[^a-z0-9]+", "_", slug.lower()).strip("_")
    return slug


class SolarForecastStringCurrentSensor(_Base):
    """Hourly corrected forecast for a single PV string."""

    _attr_icon = "mdi:solar-panel"

    def __init__(
        self,
        coordinator: ShadyCoordinator,
        entry: ConfigEntry,
        conf_key: str,       # e.g. "pv_sensor_1"
        pv_entity_id: str,   # e.g. "sensor.my_string_south"
    ) -> None:
        slug = _entity_id_to_slug(pv_entity_id)
        # unique_id uses conf_key so it stays stable even if entity_id is renamed
        super().__init__(coordinator, entry, f"solar_forecast_hourly_{conf_key}")
        self._pv_entity_id = pv_entity_id
        # Human-readable name derived from the PV entity ID slug, e.g.
        # "Solar Forecast Hourly solakon_one_string_1_leistung"
        self._attr_name = f"Solar Forecast Hourly {slug}"

    @property
    def native_value(self) -> float | None:
        slots = self._data.string_forecasts.get(self._pv_entity_id, {})
        return self._current_hour_value(slots)

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "forecast": self._data.string_forecasts.get(self._pv_entity_id, {}),
            "pv_sensor": self._pv_entity_id,
        }
