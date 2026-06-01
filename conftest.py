"""Top-level conftest – inject HA stubs before any shady module is imported."""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from unittest.mock import MagicMock

UTC = timezone.utc


class _SubscriptableMock(MagicMock):
    """MagicMock that also supports [] subscripting for generics like Coordinator[T]."""
    def __class_getitem__(cls, item):
        return cls
    def __getitem__(self, item):
        return self


class _GenericBase:
    """Base class that supports generic subscripting: class Foo(Base[T])."""
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
    def __class_getitem__(cls, item):
        return cls


def _stub(path: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(path)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[path] = mod
    # wire parent
    parts = path.split(".")
    if len(parts) > 1:
        parent_path = ".".join(parts[:-1])
        parent = sys.modules.get(parent_path)
        if parent:
            setattr(parent, parts[-1], mod)
    return mod


_stub("homeassistant")
dt_mod = _stub("homeassistant.util.dt", UTC=UTC, now=lambda: datetime.now(tz=UTC))
_stub("homeassistant.util", dt=dt_mod)
_stub("homeassistant.const", Platform=_SubscriptableMock())
_stub("homeassistant.core", HomeAssistant=_GenericBase, callback=lambda f: f)
_stub("homeassistant.config_entries", ConfigEntry=_GenericBase)
_stub("homeassistant.exceptions", ConfigEntryNotReady=Exception)
_stub("homeassistant.components")
_stub("homeassistant.components.energy", async_get_manager=MagicMock())
_stub("homeassistant.components.energy.websocket_api",
      async_get_energy_platforms=MagicMock())
_stub("homeassistant.components.recorder", get_instance=MagicMock())
_stub("homeassistant.components.recorder.statistics",
      statistics_during_period=MagicMock())
_stub("homeassistant.components.sensor",
      SensorEntity=_GenericBase,
      SensorDeviceClass=_SubscriptableMock(),
      SensorStateClass=_SubscriptableMock())
_stub("homeassistant.helpers")
_stub("homeassistant.helpers.update_coordinator",
      DataUpdateCoordinator=_GenericBase,
      CoordinatorEntity=_GenericBase,
      UpdateFailed=Exception)
_stub("homeassistant.helpers.storage", Store=MagicMock())
_stub("homeassistant.helpers.device_registry", DeviceInfo=MagicMock())
_stub("homeassistant.helpers.entity_platform",
      AddEntitiesCallback=_SubscriptableMock())
_stub("homeassistant.helpers.selector",
      EntitySelector=MagicMock(),
      EntitySelectorConfig=MagicMock(),
      SelectSelector=MagicMock(),
      SelectSelectorConfig=MagicMock(),
      SelectSelectorMode=_SubscriptableMock())


# Mirror the vendored-shadylib logic from shady/__init__.py so tests
# can import `from shadylib.X import Y` without a pip-installed shadylib.
import importlib as _il
import importlib.util as _ilu
import pathlib as _pl
if 'shadylib' not in sys.modules:
    _vd = _pl.Path(__file__).parent / 'shady' / 'shadylib'
    _pkg = types.ModuleType('shadylib')
    _pkg.__path__ = [str(_vd)]
    _pkg.__package__ = 'shadylib'
    _pkg.__file__ = str(_vd / '__init__.py')
    sys.modules['shadylib'] = _pkg
    for _f in sorted(_vd.glob('*.py')):
        if _f.stem == '__init__': continue
        _fn = f'shadylib.{_f.stem}'
        _sp = _ilu.spec_from_file_location(_fn, _f)
        _m = _ilu.module_from_spec(_sp)
        _m.__package__ = 'shadylib'
        sys.modules[_fn] = _m
        _sp.loader.exec_module(_m)
        setattr(_pkg, _f.stem, _m)
    _isp = _ilu.spec_from_file_location('shadylib', _vd / '__init__.py',
               submodule_search_locations=[str(_vd)])
    _isp.loader.exec_module(_pkg)
