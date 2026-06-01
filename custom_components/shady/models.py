"""models.py – Re-exports shadylib model building and prediction."""

from shadylib.models import (
    BucketKey,
    BucketModels,
    build_bucket_models,
    predict,
    PV_MIN_W,
    ALGORITHM_FACTOR,
    ALGORITHM_LINEAR,
    ALGORITHM_QUADRATIC,
)

__all__ = [
    "BucketKey", "BucketModels",
    "build_bucket_models", "predict",
    "PV_MIN_W",
    "ALGORITHM_FACTOR", "ALGORITHM_LINEAR", "ALGORITHM_QUADRATIC",
]
