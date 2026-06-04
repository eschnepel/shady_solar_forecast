"""Shady – corrected solar forecast as HA sensors."""

from __future__ import annotations

import logging

# Ensure shadylib is importable – use vendored copy if not pip-installed.
# Register shady.shadylib submodules under the `shadylib` namespace so that
# `from shadylib.math_utils import r` works without pip install.
import sys as _sys
import importlib.util as _iutil

_lib = "shadylib"
if _iutil.find_spec(_lib) is not None:
    pass  # already pip/uv-installed, nothing to do
elif _lib not in _sys.modules:
    from pathlib import Path as _P

    def _vendored_import(module_path: str, module_name: str) -> bool:
        import importlib.util as _ilu
        import types as _t

        _vd = _P(module_path)
        if not _vd.exists():
            return False
        _init_loc = _vd / "__init__.py"
        if not _init_loc.exists():
            return False
        _pkg = _t.ModuleType(module_name)
        _pkg.__path__ = [str(_vd)]
        _pkg.__package__ = module_name
        _pkg.__file__ = str(_init_loc)
        _sys.modules[module_name] = _pkg
        for _f in sorted(_vd.glob("*.py")):
            if _f.stem == "__init__":
                continue
            _fn = f"shadylib.{_f.stem}"
            _sp = _ilu.spec_from_file_location(_fn, _f)
            if _sp is None or _sp.loader is None:
                continue
            _m = _ilu.module_from_spec(_sp)
            _m.__package__ = module_name
            _sys.modules[_fn] = _m
            _sp.loader.exec_module(_m)
            setattr(_pkg, _f.stem, _m)
        _isp = _ilu.spec_from_file_location(
            module_name, _init_loc, submodule_search_locations=[str(_vd)]
        )
        if _isp is not None and _isp.loader is not None:
            _isp.loader.exec_module(_pkg)
        return True

    _dir = _P(__file__).parent
    _e: ImportError | None = None
    try:
        if not _vendored_import(str(_dir / _lib), _lib):
            _e = ImportError(str(_dir / _lib))
    except ImportError as ee:
        _e = ee
    if _e is not None:
        # also try 4 levels of parent folders
        for _ in range(1, 4):
            _dir = _dir.parent
            try:
                if _vendored_import(str(_dir / _lib), _lib):
                    _e = None
                    continue
            except Exception:  # noqa: BLE001
                pass
            try:
                if _vendored_import(str(_dir / _lib / _lib), _lib):
                    _e = None
                    continue
            except Exception:  # noqa: BLE001
                pass
            try:
                if _vendored_import(str(_dir / _lib / "src" / _lib), _lib):
                    _e = None
                    continue
            except Exception:  # noqa: BLE001
                pass
        if _e is not None:
            raise _e
        print("found", _lib, "at", _dir)
    del _vendored_import, _P, _dir, _e
del _sys, _lib, _iutil

from homeassistant.config_entries import ConfigEntry  # noqa: E402
from homeassistant.const import Platform  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.exceptions import ConfigEntryNotReady  # noqa: E402

from .const import DOMAIN  # noqa: E402
from .coordinator import ShadyCoordinator  # noqa: E402

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
    return bool(unload_ok)


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
