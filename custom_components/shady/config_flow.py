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
    CONF_BATTERY_EXPORT,
    CONF_BATTERY_IMPORT,
    CONF_FC_SENSOR,
    CONF_GRID_EXPORT,
    CONF_GRID_IMPORT,
    CONF_HISTORY_DAYS,
    CONF_PV_SENSORS,
    CONF_FILTER_RECORDER_GAPS,
    CONF_USE_EFFECTIVE_SENSORS,
    DEFAULT_ALGORITHM,
    DEFAULT_FC_SENSOR,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_NAME,
    DEFAULT_FILTER_RECORDER_GAPS,
    DEFAULT_USE_EFFECTIVE_SENSORS,
    DOMAIN,
    SYSTEM_SENSOR_KEYS,
)

_ENTITY_SEL = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
_ENTITY_MULTI_SEL = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", multiple=True)
)


def _optional_entity_validator(value: str | None) -> str | None:
    """Validate an optional entity selector value.

    EntitySelector rejects empty strings and None, but the HA frontend sends
    an empty string when the user clears a field with ✕.  This validator
    converts falsy values to None so the schema accepts them, while still
    running the full EntitySelector validation for real entity IDs.
    """
    if not value:
        return None
    return _ENTITY_SEL(value)


_ALGORITHM_SEL = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=ALGORITHM_OPTIONS,
        translation_key="algorithm",
        mode=selector.SelectSelectorMode.LIST,
    )
)
_BOOL_SEL = selector.BooleanSelector()


def _optional_entity(key: str, stored_value: str | None) -> vol.Optional:
    """Return a vol.Optional for an optional entity selector.

    ``description={"suggested_value": ...}`` renders the stored entity ID as
    a grey pre-fill.  No ``default`` is set, so:
    - field unberührt  → key absent from user_input
    - user clicks ✕    → key present with empty string ''
    The schema uses _optional_entity_validator (not _ENTITY_SEL directly) to
    convert the empty string to None without raising a validation error.
    """
    if stored_value:
        return vol.Optional(key, description={"suggested_value": stored_value})
    return vol.Optional(key)


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
            # --- system I/O sensors (all optional, deletable) ---
            _optional_entity(CONF_GRID_IMPORT, _get(CONF_GRID_IMPORT)): _optional_entity_validator,
            _optional_entity(CONF_GRID_EXPORT, _get(CONF_GRID_EXPORT)): _optional_entity_validator,
            _optional_entity(
                CONF_BATTERY_IMPORT, _get(CONF_BATTERY_IMPORT)
            ): _optional_entity_validator,
            _optional_entity(
                CONF_BATTERY_EXPORT, _get(CONF_BATTERY_EXPORT)
            ): _optional_entity_validator,
            # --- effective sensor switch (options only) ---
            vol.Optional(
                CONF_USE_EFFECTIVE_SENSORS,
                default=_get(CONF_USE_EFFECTIVE_SENSORS, DEFAULT_USE_EFFECTIVE_SENSORS),
            ): _BOOL_SEL,
            # --- recorder data quality ---
            vol.Optional(
                CONF_FILTER_RECORDER_GAPS,
                default=_get(CONF_FILTER_RECORDER_GAPS, DEFAULT_FILTER_RECORDER_GAPS),
            ): _BOOL_SEL,
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


def _merge_options(user_input: dict, stored: dict) -> dict:
    """Return options dict with optional sensor keys correctly resolved.

    Three cases for each SYSTEM_SENSOR_KEY:
    - Key present in user_input with a value  → user (re-)selected a sensor, keep it.
    - Key present in user_input as None        → user clicked ✕, delete it.
    - Key absent from user_input               → user did not touch the field
                                                 (suggested_value shown as placeholder),
                                                 preserve the previously stored value.

    This prevents two failure modes:
    1. Stored value re-appears after clearing  (old bug: default= re-injected old ID).
    2. Stored value is lost without user action (new bug: absent key treated as deletion).
    """
    merged = dict(user_input)
    for key in SYSTEM_SENSOR_KEYS:
        if key not in merged:
            # User did not interact with this field — keep existing value
            merged[key] = stored.get(key)
    return merged


class _OptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        stored = self._entry.options or self._entry.data
        if user_input is not None:
            return self.async_create_entry(title="", data=_merge_options(user_input, stored))
        return self.async_show_form(step_id="init", data_schema=_schema(stored))
