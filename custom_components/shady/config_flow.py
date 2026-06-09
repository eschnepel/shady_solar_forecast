"""Config flow for Shady."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    ALGORITHM_OPTIONS,
    CONF_ALGORITHM,
    CONF_FC_SENSOR,
    CONF_HISTORY_DAYS,
    CONF_PV_SENSORS,
    DEFAULT_ALGORITHM,
    DEFAULT_FC_SENSOR,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_NAME,
    DOMAIN,
)

_ENTITY_SEL = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
_ENTITY_MULTI_SEL = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", multiple=True)
)
_ALGORITHM_SEL = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=ALGORITHM_OPTIONS,
        translation_key="algorithm",
        mode=selector.SelectSelectorMode.LIST,
    )
)


def _schema(d: dict) -> vol.Schema:
    def _get(key: str, default: Any = None) -> Any:
        return d.get(key, default)

    return vol.Schema(
        {
            vol.Required(
                CONF_FC_SENSOR, default=_get(CONF_FC_SENSOR, DEFAULT_FC_SENSOR)
            ): _ENTITY_SEL,
            vol.Required(CONF_PV_SENSORS, default=_get(CONF_PV_SENSORS, [])): _ENTITY_MULTI_SEL,
            vol.Optional(
                CONF_HISTORY_DAYS, default=_get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
            ): vol.All(int, vol.Range(min=7, max=365)),
            vol.Required(
                CONF_ALGORITHM, default=_get(CONF_ALGORITHM, DEFAULT_ALGORITHM)
            ): _ALGORITHM_SEL,
        }
    )


class SolarForecastConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if any(e for e in self._async_current_entries() if e.disabled_by is None):
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title=DEFAULT_NAME, data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(entry: config_entries.ConfigEntry) -> _OptionsFlow:
        return _OptionsFlow(entry)


class _OptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        d = self._entry.options or self._entry.data
        return self.async_show_form(step_id="init", data_schema=_schema(d))
