"""statistics.py – Fetch 5-minute recorder statistics for correction model training.

Abstracts away the HA version differences in statistics_during_period row format
(object-style vs dict-style rows, Unix timestamp vs ISO string vs datetime).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.recorder import get_instance as get_recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


async def fetch_statistics(
    hass: HomeAssistant,
    statistic_ids: list[str] | set[str],
    start: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch 5-minute means for all statistic_ids in one recorder call.

    Returns {statistic_id: [{"start": datetime, "mean": float}, ...]}

    Handles both object-style rows (r.mean, r.start) and dict-style rows
    (r["mean"], r["start"]) depending on the HA version. Also handles
    Unix timestamp floats as start values.
    """

    def _mean(row: Any) -> float | None:
        return row.get("mean") if isinstance(row, dict) else getattr(row, "mean", None)

    def _start(row: Any) -> datetime:
        v = row.get("start") if isinstance(row, dict) else getattr(row, "start", None)
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            return datetime.fromisoformat(v)
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, tz=dt_util.UTC)
        raise ValueError(f"Cannot parse start value: {v!r}")

    def _query() -> dict[str, list[dict[str, Any]]]:
        result = statistics_during_period(
            hass,
            start_time=start,
            end_time=None,
            statistic_ids=set(statistic_ids),
            period="5minute",
            units=None,
            types={"mean"},
        )
        return {
            sid: [
                {"start": _start(row), "mean": _mean(row)} for row in rows if _mean(row) is not None
            ]
            for sid, rows in result.items()
        }

    res: dict[str, list[dict[str, Any]]] = await get_recorder(hass).async_add_executor_job(_query)
    return res
