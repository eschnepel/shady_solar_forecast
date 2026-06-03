"""forecast.py – Fetch raw solar forecast from the HA Energy Manager.

Calls each solar forecast platform's async_get_solar_forecast() function,
which is the same mechanism used by the Energy Dashboard internally.
Returns a flat {ISO-timestamp: Wh} dict aggregated across all sources.
"""

from __future__ import annotations

import logging

from homeassistant.components.energy import (
    async_get_manager as async_get_energy_manager,
)
from homeassistant.components.energy.websocket_api import async_get_energy_platforms
from homeassistant.core import HomeAssistant

from .math_utils import r

_LOGGER = logging.getLogger(__name__)


async def fetch_raw_forecast(hass: HomeAssistant) -> dict[str, float]:
    """Return {ISO-timestamp: Wh} aggregated over all configured solar sources."""
    manager = await async_get_energy_manager(hass)
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

    platforms = await async_get_energy_platforms(hass)
    slots: dict[str, float] = {}

    for eid in config_entry_ids:
        ce = hass.config_entries.async_get_entry(eid)
        if ce is None:
            continue
        fn = platforms.get(ce.domain)
        if fn is None:
            continue
        try:
            result = await fn(hass, eid)
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
                    slots[iso_str] = r(slots.get(iso_str, 0.0) + float(wh))
                except (ValueError, TypeError):
                    continue

    return dict(sorted(slots.items()))
