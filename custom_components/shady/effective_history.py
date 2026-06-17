"""effective_history.py – History backfill and HA-Storage cache for effective PV sensors.

On startup, checks whether the effective string sensors already have sufficient
history (covering ``history_days``).  If not, fetches recorder statistics for all
relevant sensors, computes the effective power for every missing 5-minute slot
using shadylib.compute_effective_strings, and caches the result in HA Storage.

Cache schema (JSON-serialisable):
    {
        "version": 2,
        "config_hash": "<hex>",          # SHA-256 over pv_sensors + system_sensor_cfg
                                          # + installed shadylib version
        "cached_until": "<ISO-datetime>",      # latest slot covered
        "strings": {
            "<pv_entity_id>": {
                "<YYYY-MM-DDTHH:MM:SS+00:00>": <float>,
                ...
            },
            ...
        }
    }

If ``config_hash`` is absent or differs from the current hash the entire cache
is invalidated and a fresh backfill is performed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from importlib.metadata import version as _pkg_version
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_EXPORT,
    CONF_BATTERY_IMPORT,
    CONF_GRID_EXPORT,
    CONF_GRID_IMPORT,
    EFFECTIVE_STORAGE_KEY,
    EFFECTIVE_STORAGE_VERSION,
)
from .statistics import fetch_statistics
from .units import to_wh_per_slot, detect_unit

from shadylib import compute_effective_strings, filter_gap_successors


class _DiscardOnMigrationStore(Store):
    """Store subclass that silently discards cached data on any version mismatch.

    The effective-history store holds cache data only. On a major-version
    bump the old data is simply discarded so a fresh backfill can run.
    Without this override HA raises ``NotImplementedError`` on major-version
    changes, preventing the integration from loading.
    """

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: Any,
    ) -> None:
        """Discard stale cache – migration is not needed for cache-only stores."""
        return None


_LOGGER = logging.getLogger(__name__)
_UTC = timezone.utc
_SLOT_MINUTES = 5
_HASH_LENGTH = 16  # hex chars from SHA-256 stored in cache


def compute_config_hash(
    pv_sensors: list[str],
    system_sensor_cfg: dict[str, str | None],
) -> str:
    """Return a short SHA-256 hex digest over the inputs that affect effective-history calculation.

    The hash covers:
    - the installed ``shadylib`` version (algorithm changes invalidate the cache)
    - ``pv_sensors`` list (order matters – ``compute_effective_strings`` is index-based)
    - ``system_sensor_cfg`` mapping (grid/battery entity IDs)

    Returns the first ``_HASH_LENGTH`` hex characters of the digest.
    """
    try:
        lib_version = _pkg_version("shadylib")
    except Exception:  # noqa: BLE001
        lib_version = "unknown"

    payload = json.dumps(
        {
            "shadylib": lib_version,
            "pv_sensors": pv_sensors,
            "system_sensor_cfg": system_sensor_cfg,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:_HASH_LENGTH]


def _floor5(dt: datetime) -> datetime:
    """Floor a datetime to the nearest 5-minute boundary."""
    return dt.replace(minute=(dt.minute // _SLOT_MINUTES) * _SLOT_MINUTES, second=0, microsecond=0)


def _slot_key(dt: datetime) -> str:
    """Canonical ISO key for a 5-minute slot (UTC, with offset)."""
    return dt.astimezone(_UTC).isoformat()


def _rows_to_slot_map(rows: list[dict[str, Any]], unit: str) -> dict[str, float]:
    """Convert [{start, mean}] rows (converted to Wh/slot) to {iso_key: value}."""
    converted = to_wh_per_slot(rows, unit)
    result: dict[str, float] = {}
    for row in converted:
        start = row["start"]
        if not isinstance(start, datetime):
            continue
        result[_slot_key(_floor5(start))] = float(row["mean"])
    return result


class EffectiveHistoryStore:
    """Manages on-disk cache and backfill of effective string history."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: _DiscardOnMigrationStore = _DiscardOnMigrationStore(
            hass, EFFECTIVE_STORAGE_VERSION, EFFECTIVE_STORAGE_KEY
        )
        # {pv_entity_id: {iso_slot_key: float}}
        self._cache: dict[str, dict[str, float]] = {}
        self._cached_until: datetime | None = None
        self._config_hash: str | None = None

    async def async_load(self) -> None:
        """Load cache from HA Storage."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return
        try:
            self._config_hash = data.get("config_hash") or None
            until_str = data.get("cached_until")
            if until_str:
                self._cached_until = datetime.fromisoformat(until_str)
            strings = data.get("strings", {})
            if isinstance(strings, dict):
                self._cache = {str(k): dict(v) for k, v in strings.items()}
            _LOGGER.debug(
                "Effective history loaded: %d strings, until %s (hash=%s)",
                len(self._cache),
                self._cached_until,
                self._config_hash,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not restore effective history cache: %s", err)
            self._cache = {}
            self._cached_until = None
            self._config_hash = None

    async def async_save(self) -> None:
        """Persist cache to HA Storage."""
        await self._store.async_save(
            {
                "version": EFFECTIVE_STORAGE_VERSION,
                "config_hash": self._config_hash,
                "cached_until": self._cached_until.isoformat() if self._cached_until else None,
                "strings": self._cache,
            }
        )

    def get_slots(self, pv_entity_id: str) -> dict[str, float]:
        """Return cached effective slots for a single PV string."""
        return dict(self._cache.get(pv_entity_id, {}))

    async def async_backfill_if_needed(
        self,
        pv_sensors: list[str],
        system_sensor_cfg: dict[str, str | None],
        history_days: int,
        filter_recorder_gaps: bool = True,
    ) -> None:
        """Compute and cache missing effective history slots.

        Fetches recorder statistics for all sensors involved in the loss
        calculation for any slots not yet in the cache.

        If the computed ``config_hash`` (over ``pv_sensors``, ``system_sensor_cfg``
        and the installed shadylib version) differs from the hash stored in the
        cache, the entire cache is invalidated and rebuilt from scratch.

        Args:
            pv_sensors:         Ordered list of PV string entity IDs.
            system_sensor_cfg:  {conf_key: entity_id | None} for grid/battery.
            history_days:       How many days back the history should cover.
        """
        if not pv_sensors:
            return

        current_hash = compute_config_hash(pv_sensors, system_sensor_cfg)
        if self._config_hash is not None and self._config_hash != current_hash:
            _LOGGER.info(
                "Effective history cache invalidated: config hash changed (%s → %s). "
                "Rebuilding from scratch.",
                self._config_hash,
                current_hash,
            )
            self._cache = {}
            self._cached_until = None
        self._config_hash = current_hash

        now = dt_util.now()
        required_start = _floor5(now - timedelta(days=history_days))
        cache_start = (
            _floor5(self._cached_until + timedelta(minutes=_SLOT_MINUTES))
            if self._cached_until
            else required_start
        )

        # Nothing to do if cache already covers the full history window
        if self._cached_until and self._cached_until >= _floor5(now):
            _LOGGER.debug("Effective history cache is up to date.")
            return

        fetch_from = min(required_start, cache_start)

        # Collect all entity IDs we need statistics for
        system_entity_ids: list[str] = [eid for eid in system_sensor_cfg.values() if eid]
        all_ids: list[str] = pv_sensors + system_entity_ids

        _LOGGER.info(
            "Backfilling effective history from %s for %d PV strings + %d system sensors",
            fetch_from.isoformat(),
            len(pv_sensors),
            len(system_entity_ids),
        )

        try:
            stats = await fetch_statistics(self._hass, all_ids, fetch_from)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Effective history backfill: cannot fetch statistics: %s", err)
            return

        # Detect units for each sensor
        unit_cache: dict[str, str] = {}
        for eid in all_ids:
            try:
                unit, _ = await detect_unit(self._hass, eid)
                unit_cache[eid] = unit
            except Exception:  # noqa: BLE001
                unit_cache[eid] = "W"

        # Build slot maps per sensor.
        # Optionally discard the first sample after any downtime gap wider
        # than one slot. The HA recorder may have accumulated all missing
        # values into that sample, producing an inflated effective-history
        # entry (CONF_FILTER_RECORDER_GAPS).
        def _clean(rows: list[dict]) -> list[dict]:
            return filter_gap_successors(rows) if filter_recorder_gaps else rows

        pv_slot_maps: dict[str, dict[str, float]] = {
            eid: _rows_to_slot_map(_clean(stats.get(eid, [])), unit_cache.get(eid, "W"))
            for eid in pv_sensors
        }
        sys_slot_maps: dict[str, dict[str, float]] = {
            eid: _rows_to_slot_map(_clean(stats.get(eid, [])), unit_cache.get(eid, "W"))
            for eid in system_entity_ids
        }

        # Determine the full set of slots to compute
        all_slots: set[str] = set()
        for slot_map in list(pv_slot_maps.values()) + list(sys_slot_maps.values()):
            all_slots.update(slot_map.keys())

        if not all_slots:
            _LOGGER.debug("Effective history backfill: no recorder data found.")
            return

        # Resolve system sensor entity IDs from config keys
        def _sys_val(conf_key: str, slot: str) -> float:
            eid = system_sensor_cfg.get(conf_key)
            if not eid:
                return 0.0
            return sys_slot_maps.get(eid, {}).get(slot, 0.0)

        new_slots: dict[str, dict[str, float]] = {eid: {} for eid in pv_sensors}
        latest_slot: datetime | None = self._cached_until

        for slot_key in sorted(all_slots):
            try:
                slot_dt = datetime.fromisoformat(slot_key)
            except ValueError:
                continue

            if self._cached_until and slot_dt <= self._cached_until:
                continue  # already cached

            pv_vals = [pv_slot_maps[eid].get(slot_key, 0.0) for eid in pv_sensors]

            # BMS sensor semantics
            # ----------------------
            # Each BMS sensor is bidirectional: positive values indicate the
            # primary direction, negative values indicate the opposite direction
            # within the same 5-minute aggregation slot.
            #
            #   grid_export_raw > 0  → BMS feeds power into the house grid
            #   grid_export_raw < 0  → BMS draws power from the house grid (overlap)
            #   grid_import_raw > 0  → BMS draws power from the house grid
            #   grid_import_raw < 0  → BMS feeds power into the house grid (overlap)
            #
            # Both sensors of a pair can be non-zero within a 5-minute slot due
            # to aggregation of sub-minute switching.  The net directional values
            # are:
            #
            #   grid_export_net = max(0, grid_export_raw) + max(0, -grid_import_raw)
            #   grid_import_net = max(0, grid_import_raw) + max(0, -grid_export_raw)
            #
            # Since max(0,x) - max(0,-x) = x, the full pv_usable expression
            # simplifies to using the raw values directly:
            #
            #   pv_usable = max(0,
            #       grid_export_raw - grid_import_raw
            #     + battery_import_raw - battery_export_raw
            #   )
            #
            # This is passed as grid_export to shadylib so that:
            #   total_loss = pv_sum - pv_usable
            pv_usable = max(
                0.0,
                _sys_val(CONF_GRID_EXPORT, slot_key)
                - _sys_val(CONF_GRID_IMPORT, slot_key)
                + _sys_val(CONF_BATTERY_IMPORT, slot_key)
                - _sys_val(CONF_BATTERY_EXPORT, slot_key),
            )

            effective = compute_effective_strings(
                pv_vals,
                grid_export=pv_usable,
            )

            for i, eid in enumerate(pv_sensors):
                new_slots[eid][slot_key] = effective[i]

            if latest_slot is None or slot_dt > latest_slot:
                latest_slot = slot_dt

        # Merge new slots into cache
        for eid in pv_sensors:
            if eid not in self._cache:
                self._cache[eid] = {}
            self._cache[eid].update(new_slots[eid])

        if latest_slot is not None:
            self._cached_until = latest_slot

        _LOGGER.info(
            "Effective history backfill complete: %d new slots per string, cache until %s",
            sum(len(v) for v in new_slots.values()) // max(len(pv_sensors), 1),
            self._cached_until,
        )
        await self.async_save()
