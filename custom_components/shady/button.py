"""button.py – Rebuild History button for Shady.

Provides a single diagnostic button entity per integration instance.  When
pressed it triggers a full rebuild of the effective-history cache followed by
a complete recalculation of the bucket models and forecast sensors.

Use this button after:
- Changing the correction algorithm.
- Adding or removing a PV string sensor.
- Suspecting the effective-history cache is stale or corrupted.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ShadyCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ShadyCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([RebuildHistoryButton(coordinator, entry)])


class RebuildHistoryButton(CoordinatorEntity[ShadyCoordinator], ButtonEntity):
    """Button that triggers a full effective-history rebuild and model refit.

    This is a diagnostic entity: it lives in the Shady device but is hidden
    from the default dashboard view and intended for advanced users / debugging.
    """

    _attr_name = "Rebuild History"
    _attr_icon = "mdi:history"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(self, coordinator: ShadyCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_rebuild_history"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self._entry.entry_id)})

    async def async_press(self) -> None:
        """Handle button press: rebuild history and refit bucket models."""
        _LOGGER.info("Rebuild History button pressed – starting full rebuild")
        await self.coordinator.async_rebuild_history()
