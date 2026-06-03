"""Integration-style tests for the full correction pipeline.

These tests exercise the interaction between build_bucket_models,
predict, aggregate_to_hours, and the hourly-expansion logic
that mirrors _apply_corrections in coordinator.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from shady.math_utils import r, snap, aggregate_to_hours
from shady.models import build_bucket_models, predict

UTC = timezone.utc
BUCKET_MIN = 5


def dt(hour: int, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2025, 6, day, hour, minute, tzinfo=UTC)


def simulate_correction(
    raw: dict[str, float],
    fc_rows: list[dict],
    pv_rows: list[dict],
    algorithm: str = "linear",
) -> dict[str, float]:
    """Simulate the coordinator's _apply_corrections for a single string."""
    models = build_bucket_models(fc_rows, pv_rows, algorithm)
    if not models:
        return {}

    result: dict[str, float] = {}
    for iso_ts, raw_wh in raw.items():
        slot_dt = datetime.fromisoformat(iso_ts)
        is_hourly = slot_dt.minute == 0

        if is_hourly:
            for mm in range(0, 60, BUCKET_MIN):
                sub_ts = slot_dt.replace(minute=mm, second=0, microsecond=0).isoformat()
                bk = (slot_dt.hour, mm)
                model = models.get(bk)
                val = r(max(0.0, predict(model, raw_wh))) if model else 0.0
                result[sub_ts] = r(result.get(sub_ts, 0.0) + val)
        else:
            bk = (slot_dt.hour, snap(slot_dt.minute))
            model = models.get(bk)
            val = r(max(0.0, predict(model, raw_wh))) if model else 0.0
            result[iso_ts] = val

    return result


class TestHourlyExpansion:
    def _make_training(
        self, fc_val: float, pv_val: float, hour: int, days: int = 60
    ) -> tuple[list[dict], list[dict]]:
        """Generate training rows with realistic daily variation to avoid
        degenerate (zero-variance) regression inputs."""
        fc_rows, pv_rows = [], []
        ratio = pv_val / fc_val if fc_val else 0.5
        for d in range(days):
            # Vary fc by ±20% across days so wls2 has non-zero variance
            scale = 0.8 + 0.4 * (d / max(days - 1, 1))
            for mm in range(0, 60, BUCKET_MIN):
                ts = datetime(2025, 1, 1, hour, mm, tzinfo=UTC) + timedelta(days=d)
                fc = fc_val * scale
                pv = fc * ratio
                fc_rows.append({"start": ts, "mean": fc})
                pv_rows.append({"start": ts, "mean": pv})
        return fc_rows, pv_rows

    def test_hourly_slot_expands_to_12_sub_slots(self):
        fc_rows, pv_rows = self._make_training(400.0, 200.0, 10)
        raw = {"2025-06-01T10:00:00+00:00": 400.0}
        result = simulate_correction(raw, fc_rows, pv_rows)
        # Should produce 12 sub-slots: 10:00, 10:05, ..., 10:55
        hour_keys = [k for k in result if "T10:" in k]
        assert len(hour_keys) == 12

    def test_sub_slots_have_correct_timestamps(self):
        fc_rows, pv_rows = self._make_training(400.0, 200.0, 10)
        raw = {"2025-06-01T10:00:00+00:00": 400.0}
        result = simulate_correction(raw, fc_rows, pv_rows)
        for mm in range(0, 60, BUCKET_MIN):
            expected = f"2025-06-01T10:{mm:02d}:00+00:00"
            assert expected in result

    def test_constant_factor_preserved_over_expansion(self):
        """With pv = 0.5 * fc, all 12 sub-slots should predict 0.5 * raw_wh."""
        fc_rows, pv_rows = self._make_training(400.0, 200.0, 10)
        raw = {"2025-06-01T10:00:00+00:00": 400.0}
        result = simulate_correction(raw, fc_rows, pv_rows, algorithm="factor")
        for val in result.values():
            assert abs(val - 200.0) < 5.0  # allow small fitting error

    def test_shading_in_middle_of_hour(self):
        """Buckets 10:15–10:30 shaded, others not. Shaded slots should be lower."""
        fc_rows, pv_rows = [], []
        for d in range(60):
            for mm in range(0, 60, BUCKET_MIN):
                ts = datetime(2025, 1, 1, 10, mm, tzinfo=UTC) + timedelta(days=d)
                fc_rows.append({"start": ts, "mean": 400.0})
                # Shaded 10:15–10:30
                pv = 80.0 if mm in (15, 20, 25, 30) else 320.0
                pv_rows.append({"start": ts, "mean": pv})

        raw = {"2025-06-01T10:00:00+00:00": 400.0}
        result = simulate_correction(raw, fc_rows, pv_rows, algorithm="factor")

        unshaded = result.get("2025-06-01T10:00:00+00:00", 0)
        shaded = result.get("2025-06-01T10:15:00+00:00", 0)
        assert shaded < unshaded * 0.5

    def test_no_negative_values(self):
        """All corrected values must be >= 0."""
        fc_rows = [
            {"start": dt(10, mm) + timedelta(days=d), "mean": float(d + 1) * 10}
            for d in range(30)
            for mm in range(0, 60, BUCKET_MIN)
        ]
        pv_rows = [
            {"start": dt(10, mm) + timedelta(days=d), "mean": float(d + 1) * 3}
            for d in range(30)
            for mm in range(0, 60, BUCKET_MIN)
        ]
        raw = {"2025-06-01T10:00:00+00:00": 5.0}
        result = simulate_correction(raw, fc_rows, pv_rows)
        for val in result.values():
            assert val >= 0.0


class TestTomorrowAggregation:
    def test_5min_slots_aggregate_to_hours(self):
        slots = {f"2025-06-02T10:{mm:02d}:00+00:00": 20.0 for mm in range(0, 60, 5)}
        hourly = aggregate_to_hours(slots)
        assert len(hourly) == 1
        val = list(hourly.values())[0]
        assert abs(val - 240.0) < 0.1  # 12 × 20

    def test_multi_hour_aggregation(self):
        slots = {}
        for h in range(6, 20):
            for mm in range(0, 60, 5):
                slots[f"2025-06-02T{h:02d}:{mm:02d}:00+00:00"] = 10.0
        hourly = aggregate_to_hours(slots)
        assert len(hourly) == 14
        for val in hourly.values():
            assert abs(val - 120.0) < 0.1  # 12 × 10


class TestCurtailmentFilter:
    def test_curtailed_readings_excluded_from_models(self):
        """When half the training data is curtailed (< 5W), models should
        predict as if curtailment didn't happen."""
        fc_rows, pv_rows = [], []
        for d in range(60):
            ts = dt(10, 0) + timedelta(days=d)
            fc_rows.append({"start": ts, "mean": 400.0})
            # Alternate between curtailed and normal
            pv = 1.0 if d % 2 == 0 else 200.0
            pv_rows.append({"start": ts, "mean": pv})

        models = build_bucket_models(fc_rows, pv_rows, "factor")
        model = models.get((10, 0))
        assert model is not None
        result = predict(model, 400.0)
        # Should be closer to 200 than 100 (curtailed days excluded)
        assert result > 150.0
