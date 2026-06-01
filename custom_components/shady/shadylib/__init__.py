"""shadylib – Solar forecast correction library.

HA-independent core used by the Shady Home Assistant integration.

Public API:
    from shadylib import (
        # Math & utilities
        r, r6, snap, parse_dt, aggregate_to_hours,
        wls2, wls2_origin_quad,
        # Model types
        BucketKey, BucketModels,
        # Model building & prediction
        build_bucket_models, predict,
        # Correction pipeline
        apply_corrections,
    )
"""

from .math_utils import (
    r,
    r6,
    snap,
    parse_dt,
    aggregate_to_hours,
    wls2,
    wls2_origin_quad,
    BUCKET_MIN,
    PRECISION,
)
from .models import (
    BucketKey,
    BucketModels,
    build_bucket_models,
    predict,
    PV_MIN_W,
)
from .correction import apply_corrections

__all__ = [
    # math_utils
    "r", "r6", "snap", "parse_dt", "aggregate_to_hours",
    "wls2", "wls2_origin_quad",
    "BUCKET_MIN", "PRECISION",
    # models
    "BucketKey", "BucketModels",
    "build_bucket_models", "predict",
    "PV_MIN_W",
    # correction
    "apply_corrections",
]
