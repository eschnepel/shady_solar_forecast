"""correction.py – Applies bucket models to a raw forecast. No HA dependencies.

The apply_corrections() function takes a raw forecast dict and a set of
pre-fetched recorder statistics, builds per-string hourly models, and
returns both the combined corrected forecast and per-string forecasts.

Hourly provider semantics:
    A raw slot with minute=0 is assumed to be a Wh sum for the full hour.
    It is expanded into 12 individual 5-minute sub-slots, each predicted
    by its own bucket model.  raw_wh is passed unchanged into every bucket
    model because the recorder 5-min mean (W) has the same magnitude as
    the hourly Wh/h value at constant power.

Sub-hourly provider semantics:
    Slots with minute≠0 are matched to their exact bucket (or nearest
    within the same hour if no exact match exists).
"""
from __future__ import annotations

import logging
from datetime import datetime

from .math_utils import r, snap, BUCKET_MIN
from .models import build_bucket_models, predict, BucketKey, BucketModels

_LOGGER = logging.getLogger(__name__)


def apply_corrections(
    raw: dict[str, float],
    fc_rows: list[dict],
    pv_sensors_rows: dict[str, list[dict]],
    algorithm: str,
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Correct a raw forecast using per-string per-bucket models.

    Arguments:
        raw:              {ISO-ts: Wh} – raw aggregated forecast
        fc_rows:          [{"start": datetime, "mean": float}] – forecast ref
        pv_sensors_rows:  {entity_id: [{"start": datetime, "mean": float}]}
        algorithm:        "factor" | "linear" | "quadratic"

    Returns:
        (combined, string_forecasts)

        combined:          {ISO-ts: Wh} – sum of all string predictions
        string_forecasts:  {entity_id: {ISO-ts: Wh}} – per-string predictions

    If all string models fail, returns (dict(raw), {}).
    """
    combined        : dict[str, float]              = {}
    string_forecasts: dict[str, dict[str, float]]   = {}

    for pv_sensor, pv_rows in pv_sensors_rows.items():
        models = build_bucket_models(fc_rows, pv_rows, algorithm)

        if not models:
            _LOGGER.warning(
                "No bucket models for %s (algorithm=%s, fc_rows=%d, pv_rows=%d)",
                pv_sensor, algorithm, len(fc_rows), len(pv_rows),
            )
            continue

        _LOGGER.info(
            "Bucket models for %s: algorithm=%s  %d buckets fitted",
            pv_sensor, algorithm, len(models),
        )

        string_slots = _predict_string(raw, models)
        string_forecasts[pv_sensor] = string_slots

        for ts, val in string_slots.items():
            combined[ts] = r(combined.get(ts, 0.0) + val)

    if not combined:
        _LOGGER.debug("All string models failed – falling back to raw forecast")
        return dict(raw), {}

    return dict(sorted(combined.items())), string_forecasts


def _predict_string(
    raw: dict[str, float],
    models: BucketModels,
) -> dict[str, float]:
    """Apply bucket models to a single string for all raw slots."""
    result: dict[str, float] = {}

    for iso_ts, raw_wh in raw.items():
        try:
            dt = datetime.fromisoformat(iso_ts)
        except ValueError:
            continue

        if dt.minute == 0:
            # Hourly slot → expand into 12 five-minute sub-slots
            for mm in range(0, 60, BUCKET_MIN):
                sub_ts = dt.replace(minute=mm, second=0, microsecond=0).isoformat()
                bk: BucketKey = (dt.hour, mm)
                model = models.get(bk)
                val   = r(max(0.0, predict(model, raw_wh))) if model else 0.0
                result[sub_ts] = r(result.get(sub_ts, 0.0) + val)
        else:
            # Sub-hourly slot → exact or nearest bucket within same hour
            bk    = (dt.hour, snap(dt.minute))
            model = _nearest_model(models, dt.hour, snap(dt.minute))
            val   = r(max(0.0, predict(model, raw_wh))) if model else 0.0
            result[iso_ts] = val

    return result


def _nearest_model(
    models: BucketModels, hour: int, snapped_minute: int
) -> tuple | None:
    """Return the model for (hour, snapped_minute) or the nearest bucket
    within the same hour if an exact match doesn't exist."""
    exact = models.get((hour, snapped_minute))
    if exact is not None:
        return exact

    best_model: tuple | None = None
    best_dist = 60
    for (mh, mm), model in models.items():
        if mh == hour:
            dist = abs(mm - snapped_minute)
            if dist < best_dist:
                best_dist = dist
                best_model = model
    return best_model
