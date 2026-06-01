"""models.py – Bucket model building and prediction. No external dependencies.

Contains:
  - Type aliases (BucketKey, BucketModels)
  - predict: apply any model tuple to a raw value
  - build_bucket_models: fit one WLS model per (hour, 5-min) bucket
  - Internal fitters: _fit_factor, _fit_linear, _fit_quadratic

Model tuple encoding (identified by length in predict):
  FACTOR     : (factor,)           – 1-tuple
  LINEAR     : (slope, intercept)  – 2-tuple
  QUADRATIC  : (a, b, 0.0)         – 3-tuple  (c always 0, through-origin)
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from .math_utils import r, r6, snap, wls2, wls2_origin_quad, BUCKET_MIN

_LOGGER = logging.getLogger(__name__)

# Neighbour smoothing weights
_W_SELF = 1.0   # the observation itself
_W_NEAR = 0.8   # ±5 min neighbour
_W_FAR  = 0.3   # ±10 min neighbour

# Minimum PV value (W) included in training data.
# Readings below this threshold indicate curtailment (e.g. battery full)
# and are excluded to avoid pulling bucket models toward zero incorrectly.
PV_MIN_W = 5.0

# Algorithm name constants
ALGORITHM_FACTOR    = "factor"
ALGORITHM_LINEAR    = "linear"
ALGORITHM_QUADRATIC = "quadratic"

BucketKey    = tuple[int, int]
BucketModels = dict[BucketKey, tuple]


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------

def predict(model: tuple, x: float) -> float:
    """Apply a model tuple to raw forecast value x."""
    if len(model) == 1:
        return model[0] * x
    if len(model) == 2:
        slope, intercept = model
        return slope * x + intercept
    a, b, c = model
    return a * x * x + b * x + c


# ---------------------------------------------------------------------------
# Model builder
# ---------------------------------------------------------------------------

def build_bucket_models(
    fc_rows: list[dict],
    pv_rows: list[dict],
    algorithm: str,
) -> BucketModels:
    """Build one WLS model per (hour, 5-min-bucket) from recorder statistics.

    Arguments:
        fc_rows:   [{"start": datetime, "mean": float}, ...]  – forecast ref
        pv_rows:   [{"start": datetime, "mean": float}, ...]  – actual PV
        algorithm: "factor" | "linear" | "quadratic"

    PV readings below PV_MIN_W are excluded (curtailment filter).

    Neighbour smoothing weights:
        self    : 1.0
        ±5 min  : 0.8
        ±10 min : 0.3

    Returns a dict of {(hour, minute): model_tuple}.
    """
    fc_map: dict[datetime, float] = {r["start"]: r["mean"] for r in fc_rows}
    pv_map: dict[datetime, float] = {
        r["start"]: r["mean"] for r in pv_rows if r["mean"] >= PV_MIN_W
    }

    common = sorted(set(fc_map) & set(pv_map))
    if not common:
        return {}

    # {BucketKey: [(fc_val, pv_val, weight), ...]}
    buckets: dict[BucketKey, list[tuple[float, float, float]]] = defaultdict(list)

    for dt in common:
        bk = (dt.hour, snap(dt.minute))
        buckets[bk].append((fc_map[dt], pv_map[dt], _W_SELF))

        for delta_min, weight in (
            (-10, _W_FAR), (-5, _W_NEAR), (+5, _W_NEAR), (+10, _W_FAR)
        ):
            nb = dt + timedelta(minutes=delta_min)
            if nb in fc_map and nb in pv_map:
                nb_bk = (nb.hour, snap(nb.minute))
                buckets[nb_bk].append((fc_map[nb], pv_map[nb], weight))

    models: BucketModels = {}
    for bk, obs in buckets.items():
        if len(obs) < 2:
            continue
        xs = [o[0] for o in obs]
        ys = [o[1] for o in obs]
        ws = [o[2] for o in obs]

        if algorithm == ALGORITHM_FACTOR:
            model = _fit_factor(xs, ys, ws)
        elif algorithm == ALGORITHM_QUADRATIC:
            model = _fit_quadratic(xs, ys, ws)
        else:
            model = _fit_linear(xs, ys, ws)

        if model is not None:
            models[bk] = model

    return models


# ---------------------------------------------------------------------------
# Fitters
# ---------------------------------------------------------------------------

def _fit_factor(xs: list[float], ys: list[float], ws: list[float]) -> tuple | None:
    """Per-bucket weighted mean ratio: factor = avg_w(pv) / avg_w(fc)."""
    sw   = sum(ws)
    if sw == 0:
        return None
    mu_x = sum(w * x for w, x in zip(ws, xs)) / sw
    mu_y = sum(w * y for w, y in zip(ws, ys)) / sw
    if mu_x < 1e-9:
        return (0.0,)
    return (r6(mu_y / mu_x),)


def _fit_linear(xs: list[float], ys: list[float], ws: list[float]) -> tuple | None:
    """WLS linear: pv ~ slope*fc + intercept."""
    result = wls2(xs, ys, ws)
    if result is None:
        return None
    slope, intercept = result
    return (r6(slope), r(intercept))


def _fit_quadratic(xs: list[float], ys: list[float], ws: list[float]) -> tuple | None:
    """WLS quadratic through origin: pv ~ a*fc² + b*fc  (no free intercept).

    Returns (a, b, 0.0) so predict() uses the standard quadratic path with c=0.
    Falls back to linear if fewer than 3 points or system is degenerate.
    """
    if len(xs) < 3:
        return _fit_linear(xs, ys, ws)
    result = wls2_origin_quad(xs, ys, ws)
    if result is None:
        return _fit_linear(xs, ys, ws)
    a, b = result
    return (r6(a), r6(b), 0.0)
