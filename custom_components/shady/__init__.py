"""Shady – corrected solar forecast as HA sensors."""
from __future__ import annotations

import logging

# Ensure shadylib is importable – use vendored copy if not pip-installed.
# Register shady.shadylib submodules under the `shadylib` namespace so that
# `from shadylib.math_utils import r` works without pip install.
import sys as _sys
if 'shadylib' not in _sys.modules:
    import importlib as _il
    import importlib.util as _ilu
    import types as _t
    from pathlib import Path as _P
    _vd = _P(__file__).parent / 'shadylib'
    _pkg = _t.ModuleType('shadylib')
    _pkg.__path__ = [str(_vd)]
    _pkg.__package__ = 'shadylib'
    _pkg.__file__ = str(_vd / '__init__.py')
    _sys.modules['shadylib'] = _pkg
    for _f in sorted(_vd.glob('*.py')):
        if _f.stem == '__init__': continue
        _fn = f'shadylib.{_f.stem}'
        _sp = _ilu.spec_from_file_location(_fn, _f)
        _m = _ilu.module_from_spec(_sp)
        _m.__package__ = 'shadylib'
        _sys.modules[_fn] = _m
        _sp.loader.exec_module(_m)
        setattr(_pkg, _f.stem, _m)
    _isp = _ilu.spec_from_file_location('shadylib', _vd / '__init__.py',
               submodule_search_locations=[str(_vd)])
    _isp.loader.exec_module(_pkg)
    del _sys, _il, _ilu, _t, _P, _vd, _pkg, _f, _fn, _sp, _m, _isp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN
from .coordinator import ShadyCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    coordinator = ShadyCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except Exception as err:
        raise ConfigEntryNotReady(f"Solar forecast not available: {err}") from err

    hass.data[DOMAIN][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: ShadyCoordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        coordinator.async_teardown()

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
