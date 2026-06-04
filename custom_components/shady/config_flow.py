"""Config flow for Shady."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    DEFAULT_NAME,
    CONF_FC_SENSOR,
    CONF_HISTORY_DAYS,
    CONF_ALGORITHM,
    CONF_PV_SENSOR_1,
    CONF_PV_SENSOR_2,
    CONF_PV_SENSOR_3,
    CONF_PV_SENSOR_4,
    DEFAULT_FC_SENSOR,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_ALGORITHM,
    ALGORITHM_OPTIONS,
)

_ENTITY_SEL = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
_ENTITY_SEL_OPT = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", multiple=False)
)
_ALGORITHM_SEL = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=ALGORITHM_OPTIONS,
        translation_key="algorithm",
        mode=selector.SelectSelectorMode.LIST,
    )
)


def _schema(d: dict) -> vol.Schema:
    def _get(key: str, default: Any = "") -> Any:
        return d.get(key, default)

    return vol.Schema(
        {
            vol.Required(
                CONF_FC_SENSOR, default=_get(CONF_FC_SENSOR, DEFAULT_FC_SENSOR)
            ): _ENTITY_SEL,
            vol.Required(CONF_PV_SENSOR_1, default=_get(CONF_PV_SENSOR_1)): _ENTITY_SEL,
            vol.Optional(CONF_PV_SENSOR_2, default=_get(CONF_PV_SENSOR_2)): _ENTITY_SEL_OPT,
            vol.Optional(CONF_PV_SENSOR_3, default=_get(CONF_PV_SENSOR_3)): _ENTITY_SEL_OPT,
            vol.Optional(CONF_PV_SENSOR_4, default=_get(CONF_PV_SENSOR_4)): _ENTITY_SEL_OPT,
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
        if self._async_current_entries():
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
