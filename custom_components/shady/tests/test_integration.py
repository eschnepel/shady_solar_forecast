"""Integration-style tests for the full correction pipeline.

These tests exercise the interaction between build_bucket_models,
predict, aggregate_to_hours, and the hourly-expansion logic
that mirrors _apply_corrections in coordinator.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from shadylib import r, snap, aggregate_to_hours
from shadylib import build_bucket_models, predict

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
                val = r(max(0.0, predict(model, raw_wh)) / 12) if model else 0.0
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
        """With pv = 0.5 * fc (both in W), the hourly slot (400 Wh) should
        produce 12 sub-slots that together sum to 200 Wh (= 400 * 0.5).
        Each individual sub-slot is 200 / 12 ≈ 16.67 Wh."""
        fc_rows, pv_rows = self._make_training(400.0, 200.0, 10)
        raw = {"2025-06-01T10:00:00+00:00": 400.0}
        result = simulate_correction(raw, fc_rows, pv_rows, algorithm="factor")
        expected_per_slot = 400.0 * 0.5 / 12  # ≈ 16.67 Wh
        for val in result.values():
            assert abs(val - expected_per_slot) < 1.0  # allow small fitting error
        # The 12 sub-slots must also sum to ~200 Wh (the corrected hourly total)
        assert abs(sum(result.values()) - 200.0) < 5.0

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


class TestTodayTotalAndRemaining:
    """today_total and remaining are computed directly from forecast_today 5-min slots.

    This mirrors coordinator._build_data():
        today_total = r(sum(forecast_today.values()))
        remaining   = r(sum(wh for ts, wh in forecast_today.items() if parse_dt(ts) >= now))

    No hourly aggregation step — so remaining has 5-min precision.
    """

    def _make_slots(self, hours: range, wh_per_slot: float) -> dict[str, float]:
        slots = {}
        for h in hours:
            for mm in range(0, 60, BUCKET_MIN):
                ts = datetime(2025, 6, 2, h, mm, tzinfo=UTC).isoformat()
                slots[ts] = wh_per_slot
        return slots

    def test_today_total_sums_all_slots(self):
        """Sum of all 5-min slots equals today_total."""
        slots = self._make_slots(range(6, 20), 10.0)
        total = round(sum(slots.values()), 2)
        # 14 hours × 12 slots × 10 Wh = 1680 Wh
        assert abs(total - 1680.0) < 0.1

    def test_remaining_excludes_past_slots(self):
        """remaining only counts slots whose start timestamp >= now."""
        from shadylib import parse_dt

        slots = self._make_slots(range(6, 20), 10.0)
        now = datetime(2025, 6, 2, 12, 0, tzinfo=UTC)
        remaining = round(sum(wh for ts, wh in slots.items() if parse_dt(ts) >= now), 2)
        total = round(sum(slots.values()), 2)
        # 12:00–19:55 = 8 hours × 12 slots × 10 Wh = 960 Wh
        assert abs(remaining - 960.0) < 0.1
        assert remaining < total

    def test_remaining_5min_precision(self):
        """remaining changes by exactly one slot (10 Wh) when now advances 5 min."""
        from shadylib import parse_dt

        slots = self._make_slots(range(10, 14), 10.0)
        now_a = datetime(2025, 6, 2, 11, 0, tzinfo=UTC)
        now_b = datetime(2025, 6, 2, 11, 5, tzinfo=UTC)

        rem_a = sum(wh for ts, wh in slots.items() if parse_dt(ts) >= now_a)
        rem_b = sum(wh for ts, wh in slots.items() if parse_dt(ts) >= now_b)
        assert abs((rem_a - rem_b) - 10.0) < 0.01
