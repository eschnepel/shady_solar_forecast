"""Tests for models.py – bucket model building and prediction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from shadylib.models import (
    predict,
    build_bucket_models,
    _fit_factor,
    _fit_linear,
    _fit_quadratic,
)

UTC = timezone.utc


def dt(hour: int, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2025, 6, day, hour, minute, tzinfo=UTC)


def make_rows(pairs: list[tuple[datetime, float]], key: str = "mean") -> list[dict]:
    return [{"start": ts, "mean": val} for ts, val in pairs]


# ---------------------------------------------------------------------------
# predict
# ---------------------------------------------------------------------------


class TestPredict:
    def test_factor_model(self):
        assert predict((0.5,), 100.0) == 50.0

    def test_factor_zero(self):
        assert predict((0.0,), 999.0) == 0.0

    def test_linear_model(self):
        # slope=2, intercept=3 → 2*10+3 = 23
        assert predict((2.0, 3.0), 10.0) == 23.0

    def test_linear_zero_input(self):
        assert predict((2.0, 5.0), 0.0) == 5.0

    def test_quadratic_model(self):
        # a=1, b=2, c=0 → 1*4 + 2*2 + 0 = 8
        assert predict((1.0, 2.0, 0.0), 2.0) == 8.0

    def test_quadratic_through_origin(self):
        assert predict((1.0, 0.0, 0.0), 0.0) == 0.0

    def test_quadratic_full(self):
        # a=1, b=2, c=3 → 1*9 + 2*3 + 3 = 18
        assert predict((1.0, 2.0, 3.0), 3.0) == 18.0


# ---------------------------------------------------------------------------
# _fit_factor
# ---------------------------------------------------------------------------


class TestFitFactor:
    def test_simple_ratio(self):
        # pv is always half of fc
        xs = [100.0, 200.0, 300.0]
        ys = [50.0, 100.0, 150.0]
        ws = [1.0, 1.0, 1.0]
        model = _fit_factor(xs, ys, ws)
        assert model is not None
        assert len(model) == 1
        assert abs(model[0] - 0.5) < 1e-6

    def test_zero_fc_returns_zero_factor(self):
        xs = [0.0, 0.0]
        ys = [5.0, 5.0]
        ws = [1.0, 1.0]
        model = _fit_factor(xs, ys, ws)
        assert model == (0.0,)

    def test_zero_weights(self):
        assert _fit_factor([1.0], [1.0], [0.0]) is None

    def test_weighted_ratio(self):
        # x=100→y=50 (high weight), x=100→y=100 (low weight)
        xs = [100.0, 100.0]
        ys = [50.0, 100.0]
        ws = [10.0, 1.0]
        model = _fit_factor(xs, ys, ws)
        assert model is not None
        # weighted avg pv ≈ (10*50 + 1*100)/11 ≈ 54.5
        # weighted avg fc = 100
        # factor ≈ 0.545
        assert abs(model[0] - (10 * 50 + 100) / (11 * 100)) < 1e-4


# ---------------------------------------------------------------------------
# _fit_linear
# ---------------------------------------------------------------------------


class TestFitLinear:
    def test_exact_linear(self):
        xs = [0.0, 1.0, 2.0, 3.0]
        ys = [1.0, 3.0, 5.0, 7.0]  # y = 2x + 1
        ws = [1.0] * 4
        model = _fit_linear(xs, ys, ws)
        assert model is not None
        slope, intercept = model
        assert abs(slope - 2.0) < 1e-4
        assert abs(intercept - 1.0) < 1e-4

    def test_returns_none_for_degenerate(self):
        xs = [5.0, 5.0, 5.0]
        ys = [1.0, 2.0, 3.0]
        ws = [1.0, 1.0, 1.0]
        assert _fit_linear(xs, ys, ws) is None

    def test_model_is_2_tuple(self):
        xs = [1.0, 2.0, 3.0]
        ys = [2.0, 4.0, 6.0]
        ws = [1.0, 1.0, 1.0]
        model = _fit_linear(xs, ys, ws)
        assert model is not None
        assert len(model) == 2


# ---------------------------------------------------------------------------
# _fit_quadratic
# ---------------------------------------------------------------------------


class TestFitQuadratic:
    def test_falls_back_to_linear_for_small_input(self):
        xs = [1.0, 2.0]  # < 3 points
        ys = [2.0, 4.0]
        ws = [1.0, 1.0]
        model = _fit_quadratic(xs, ys, ws)
        # Should fall back to linear (2-tuple)
        assert model is not None
        assert len(model) == 2

    def test_returns_3_tuple_with_zero_c(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [1.0, 4.0, 9.0, 16.0, 25.0]  # y = x²
        ws = [1.0] * 5
        model = _fit_quadratic(xs, ys, ws)
        assert model is not None
        assert len(model) == 3
        assert model[2] == 0.0  # c is always 0

    def test_through_origin(self):
        """y = 2x² recovers approximately."""
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [2.0, 8.0, 18.0, 32.0, 50.0]  # y = 2x²
        ws = [1.0] * 5
        model = _fit_quadratic(xs, ys, ws)
        assert model is not None
        a, b, c = model
        assert abs(a - 2.0) < 1e-4
        assert abs(b) < 1e-4
        assert c == 0.0


# ---------------------------------------------------------------------------
# build_bucket_models
# ---------------------------------------------------------------------------


class TestBuildBucketModels:
    def _rows(
        self,
        hour: int,
        minutes: list[int],
        fc_vals: list[float],
        pv_vals: list[float],
        days: int = 30,
    ) -> tuple[list[dict], list[dict]]:
        """Generate fc_rows and pv_rows for a given hour/minute across N days."""
        fc_rows, pv_rows = [], []
        for day in range(days):
            base = datetime(2025, 1, 1, hour, 0, tzinfo=UTC) + timedelta(days=day)
            for i, minute in enumerate(minutes):
                ts = base.replace(minute=minute)
                fc_rows.append({"start": ts, "mean": fc_vals[i % len(fc_vals)]})
                pv_rows.append({"start": ts, "mean": pv_vals[i % len(pv_vals)]})
        return fc_rows, fc_rows

    def test_empty_input_returns_empty(self):
        assert build_bucket_models([], [], "linear") == {}

    def test_no_common_timestamps_returns_empty(self):
        fc_rows = [{"start": dt(10, 0), "mean": 100.0}]
        pv_rows = [{"start": dt(11, 0), "mean": 50.0}]
        assert build_bucket_models(fc_rows, pv_rows, "linear") == {}

    def test_pv_below_threshold_excluded(self):
        """Readings < 5W should not contribute to models."""
        ts = dt(10, 0)
        fc_rows = [{"start": ts + timedelta(days=i), "mean": 100.0} for i in range(30)]
        # Mix of curtailed (< 5W) and valid readings
        pv_rows = []
        for i in range(30):
            val = 0.0 if i % 2 == 0 else 50.0  # half curtailed
            pv_rows.append({"start": ts + timedelta(days=i), "mean": val})
        models = build_bucket_models(fc_rows, pv_rows, "linear")
        # Models should exist (valid readings present) but not be pulled to zero
        if models:
            model = models.get((10, 0))
            if model:
                assert predict(model, 100.0) > 0

    def test_pv_all_below_threshold_returns_empty(self):
        """All curtailed data → no models."""
        fc_rows = [
            {"start": dt(10, 0) + timedelta(days=i), "mean": 100.0} for i in range(10)
        ]
        pv_rows = [
            {"start": dt(10, 0) + timedelta(days=i), "mean": 1.0} for i in range(10)
        ]
        assert build_bucket_models(fc_rows, pv_rows, "linear") == {}

    def test_bucket_keys_are_snapped(self):
        """All keys should have minute ∈ {0,5,10,...,55}."""
        fc_rows = [
            {"start": dt(10, m) + timedelta(days=d), "mean": 100.0}
            for d in range(30)
            for m in range(0, 60, 5)
        ]
        pv_rows = [
            {"start": dt(10, m) + timedelta(days=d), "mean": 50.0}
            for d in range(30)
            for m in range(0, 60, 5)
        ]
        models = build_bucket_models(fc_rows, pv_rows, "linear")
        for h, m in models:
            assert m % 5 == 0

    def test_factor_algorithm_produces_1_tuples(self):
        fc_rows = [
            {"start": dt(10, 0) + timedelta(days=i), "mean": 200.0} for i in range(30)
        ]
        pv_rows = [
            {"start": dt(10, 0) + timedelta(days=i), "mean": 100.0} for i in range(30)
        ]
        models = build_bucket_models(fc_rows, pv_rows, "factor")
        for model in models.values():
            assert len(model) == 1

    def test_linear_algorithm_produces_2_tuples(self):
        fc_rows = [
            {"start": dt(10, 0) + timedelta(days=i), "mean": float(i + 1) * 10}
            for i in range(30)
        ]
        pv_rows = [
            {"start": dt(10, 0) + timedelta(days=i), "mean": float(i + 1) * 5}
            for i in range(30)
        ]
        models = build_bucket_models(fc_rows, pv_rows, "linear")
        for model in models.values():
            assert len(model) == 2

    def test_quadratic_algorithm_produces_3_tuples(self):
        fc_rows = [
            {"start": dt(10, 0) + timedelta(days=i), "mean": float(i + 1) * 10}
            for i in range(30)
        ]
        pv_rows = [
            {"start": dt(10, 0) + timedelta(days=i), "mean": float(i + 1) * 5}
            for i in range(30)
        ]
        models = build_bucket_models(fc_rows, pv_rows, "quadratic")
        for model in models.values():
            assert len(model) == 3
            assert model[2] == 0.0  # c always 0

    def test_neighbour_smoothing_enriches_adjacent_buckets(self):
        """Observations from one bucket enrich neighbouring buckets.

        When data exists at 10:00 and 10:10, the ±5min smoothing means that
        10:00's observations are donated into the (10,5) bucket IF 10:05
        also exists in the training data.  Without training data at 10:05,
        the bucket stays empty (neighbours donate only when the neighbour
        timestamp itself has data).

        This test verifies that when 10:05 data IS present, it gets enriched
        by the adjacent 10:00 and 10:10 observations (more weight = better fit).
        """
        # All three minutes present – (10,5) should be enriched by neighbours
        times = [dt(10, 0), dt(10, 5), dt(10, 10)]
        fc_rows = [
            {"start": ts + timedelta(days=d), "mean": 100.0 + d * 5}
            for ts in times
            for d in range(30)
        ]
        pv_rows = [
            {"start": ts + timedelta(days=d), "mean": 50.0 + d * 2.5}
            for ts in times
            for d in range(30)
        ]
        models = build_bucket_models(fc_rows, pv_rows, "linear")
        # All three buckets should be present
        assert (10, 0) in models
        assert (10, 5) in models
        assert (10, 10) in models
        # (10, 5) has more training observations (self + 2 neighbours) than (10, 0) alone
        # Verify models are consistent (same slope since all data has same ratio)
        s0, _ = models[(10, 0)]
        s5, _ = models[(10, 5)]
        assert abs(s0 - s5) < 0.05  # slopes should be similar due to shared data

    def test_correction_factor_applied_correctly(self):
        """With pv = 0.3 * fc consistently, factor model should give ~0.3."""
        fc_rows = [
            {"start": dt(10, 0) + timedelta(days=i), "mean": 400.0} for i in range(60)
        ]
        pv_rows = [
            {"start": dt(10, 0) + timedelta(days=i), "mean": 120.0} for i in range(60)
        ]
        models = build_bucket_models(fc_rows, pv_rows, "factor")
        model = models.get((10, 0))
        assert model is not None
        result = predict(model, 400.0)
        assert abs(result - 120.0) < 1.0

    def test_shading_pattern_captured(self):
        """Bucket 10:15 shaded (50% reduction), 10:00 not shaded.
        The two buckets should have different models."""
        fc_val = 400.0
        fc_rows, pv_rows = [], []
        for d in range(60):
            base = datetime(2025, 1, 1, 10, 0, tzinfo=UTC) + timedelta(days=d)
            # 10:00 – full production
            fc_rows.append({"start": base, "mean": fc_val})
            pv_rows.append({"start": base, "mean": fc_val * 0.9})
            # 10:15 – shaded, only 30%
            ts15 = base.replace(minute=15)
            fc_rows.append({"start": ts15, "mean": fc_val})
            pv_rows.append({"start": ts15, "mean": fc_val * 0.3})

        models = build_bucket_models(fc_rows, pv_rows, "factor")
        m00 = models.get((10, 0))
        m15 = models.get((10, 15))
        assert m00 is not None
        assert m15 is not None
        assert predict(m00, fc_val) > predict(m15, fc_val)
