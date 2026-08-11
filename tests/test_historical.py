"""Tests for E-REDES historical statistics import."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.recorder.statistics import valid_statistic_id

from custom_components.eredes.eredes_api.models import ConsumptionReading, MeterIndex
from custom_components.eredes.historical import (
    HISTORY_IMPORT_VERSION,
    LISBON,
    _aggregate_to_hourly_statistics,
    _async_fetch_history,
    _async_verify_statistics_import,
    _daily_real_totals,
    _reconcile_with_meter_indexes,
    async_import_historical_data,
    statistic_id,
)

CPE = "PT0002000012345678AB"


def _reading(hour: int, minute: int, value_wh: float) -> ConsumptionReading:
    """Build an aware-UTC 15-minute reading on 2026-01-01.

    The client converts Lisbon local time to UTC at parse time, so the
    aggregator only ever sees timezone-aware readings.
    """
    return ConsumptionReading(
        timestamp=datetime(2026, 1, 1, hour, minute, tzinfo=UTC),
        value_wh=value_wh,
    )


def test_statistic_id_is_valid_external_id() -> None:
    """The id must be a `source:object_id` external id (the import crash regression)."""
    stat_id = statistic_id(CPE)

    assert stat_id == "eredes:energy_345678ab"
    assert valid_statistic_id(stat_id)


def test_aggregate_buckets_interval_end_timestamps_by_consumption_hour() -> None:
    """Quarter-hour timestamps mark interval ends, including the top of next hour."""
    readings = [
        _reading(0, 15, 250.0),
        _reading(0, 30, 250.0),
        _reading(0, 45, 250.0),
        _reading(1, 0, 250.0),  # final quarter of hour 0 -> 1.0 kWh total
        _reading(1, 15, 500.0),
        _reading(1, 30, 500.0),  # first half of hour 1 -> 1.0 kWh
    ]

    stats = _aggregate_to_hourly_statistics(readings)

    assert [s["start"] for s in stats] == [
        datetime(2026, 1, 1, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 1, tzinfo=UTC),
    ]
    assert [s["state"] for s in stats] == [1.0, 1.0]
    assert [s["sum"] for s in stats] == [1.0, 2.0]  # cumulative


def test_aggregate_seeds_cumulative_sum() -> None:
    """A resume continues the running sum from the previous import."""
    readings = [_reading(0, 0, 1000.0), _reading(1, 0, 1000.0)]

    stats = _aggregate_to_hourly_statistics(readings, initial_sum=10.0)

    assert [s["sum"] for s in stats] == [11.0, 12.0]


def test_real_meter_indexes_override_disagreeing_daily_load_curve() -> None:
    """A 24 kWh estimated curve is scaled to the authoritative 12 kWh real day."""
    # 96 quarter-hours ending from Aug 1 00:15 through Aug 2 00:00 local.
    readings = [
        ConsumptionReading(
            timestamp=(
                datetime(2026, 8, 1, 0, 15) + timedelta(minutes=15 * index)
            )
            .replace(tzinfo=LISBON)
            .astimezone(UTC),
            value_wh=250.0,
        )
        for index in range(96)
    ]
    indexes = [
        MeterIndex(
            timestamp=datetime(2026, 8, 1, tzinfo=LISBON).astimezone(UTC),
            value_kwh=10609.0,
            meter_serial="12345678",
        ),
        MeterIndex(
            timestamp=datetime(2026, 8, 2, tzinfo=LISBON).astimezone(UTC),
            value_kwh=10621.0,
            meter_serial="12345678",
        ),
    ]

    result = _reconcile_with_meter_indexes(
        readings,
        indexes,
        matching_days={date(2026, 8, 1)},
    )

    assert sum(reading.value_kwh for reading in readings) == 24.0
    assert sum(reading.value_kwh for reading in result.readings) == 12.0
    assert all(reading.value_wh == 125.0 for reading in result.readings)
    assert result.pending_days == {date(2026, 8, 1)}
    assert result.matching_days == set()


def test_duplicate_quarter_hours_still_reconcile_to_real_daily_total() -> None:
    """Duplicate load-curve rows must not bypass a known real daily total."""
    # Model a duplicated August 1 curve: each physical 15-minute interval is
    # returned twice at 125 Wh, so the raw imported day totals 24 kWh instead of
    # the 12 kWh implied by the real cumulative meter indexes.
    readings = []
    for index in range(96):
        timestamp = (
            datetime(2026, 8, 1, 0, 15) + timedelta(minutes=15 * index)
        ).replace(tzinfo=LISBON).astimezone(UTC)
        readings.extend(
            [
                ConsumptionReading(timestamp=timestamp, value_wh=125.0),
                ConsumptionReading(timestamp=timestamp, value_wh=125.0),
            ]
        )

    indexes = [
        MeterIndex(
            timestamp=datetime(2026, 8, 1, tzinfo=LISBON).astimezone(UTC),
            value_kwh=10609.0,
            meter_serial="12345678",
            register_count=3,
        ),
        MeterIndex(
            timestamp=datetime(2026, 8, 2, tzinfo=LISBON).astimezone(UTC),
            value_kwh=10621.0,
            meter_serial="12345678",
            register_count=3,
        ),
    ]

    result = _reconcile_with_meter_indexes(readings, indexes)

    assert sum(reading.value_kwh for reading in readings) == 24.0
    assert sum(reading.value_kwh for reading in result.readings) == 12.0
    assert result.pending_days == {date(2026, 8, 1)}


def test_real_meter_quantization_tolerance_accepts_three_register_deviation() -> None:
    """A tri-hourly real index allows up to 3 kWh of endpoint quantization error."""
    readings = [
        ConsumptionReading(
            timestamp=(
                datetime(2026, 8, 1, 0, 15) + timedelta(minutes=15 * index)
            )
            .replace(tzinfo=LISBON)
            .astimezone(UTC),
            value_wh=150.0,
        )
        for index in range(96)
    ]
    indexes = [
        MeterIndex(
            timestamp=datetime(2026, 8, 1, tzinfo=LISBON).astimezone(UTC),
            value_kwh=10609.0,
            meter_serial="12345678",
            register_count=3,
        ),
        MeterIndex(
            timestamp=datetime(2026, 8, 2, tzinfo=LISBON).astimezone(UTC),
            value_kwh=10621.0,
            meter_serial="12345678",
            register_count=3,
        ),
    ]

    result = _reconcile_with_meter_indexes(
        readings,
        indexes,
        pending_days={date(2026, 8, 1)},
    )

    # Raw curve = 14.4 kWh, real integer-register delta = 12 kWh. The 2.4 kWh
    # difference is within the ±3 kWh quantization envelope, so raw 15-minute
    # data is now credible and the pending repair is cleared.
    assert sum(reading.value_kwh for reading in result.readings) == pytest.approx(14.4)
    assert result.pending_days == set()
    assert result.matching_days == {date(2026, 8, 1)}


def test_pending_reconciliation_survives_incomplete_refetch() -> None:
    """A pending day is never cleared by an incomplete later load-curve fetch."""
    readings = [
        ConsumptionReading(
            timestamp=(
                datetime(2026, 8, 1, 0, 15) + timedelta(minutes=15 * index)
            )
            .replace(tzinfo=LISBON)
            .astimezone(UTC),
            value_wh=125.0,
        )
        for index in range(96)
        if index != 40
    ]
    indexes = [
        MeterIndex(
            timestamp=datetime(2026, 8, 1, tzinfo=LISBON).astimezone(UTC),
            value_kwh=10609.0,
            meter_serial="12345678",
            register_count=3,
        ),
        MeterIndex(
            timestamp=datetime(2026, 8, 2, tzinfo=LISBON).astimezone(UTC),
            value_kwh=10621.0,
            meter_serial="12345678",
            register_count=3,
        ),
    ]

    result = _reconcile_with_meter_indexes(readings, indexes)

    assert result.readings == readings
    assert result.pending_days == {date(2026, 8, 1)}


def test_real_daily_total_requires_consecutive_midnight_indexes() -> None:
    """Gaps in the cumulative index never invent per-day consumption."""
    indexes = [
        MeterIndex(
            timestamp=datetime(2026, 8, 1, tzinfo=LISBON).astimezone(UTC),
            value_kwh=100.0,
            meter_serial="12345678",
        ),
        MeterIndex(
            timestamp=datetime(2026, 8, 3, tzinfo=LISBON).astimezone(UTC),
            value_kwh=112.0,
            meter_serial="12345678",
        ),
    ]

    assert _daily_real_totals(indexes) == {}


def test_real_daily_total_does_not_cross_meter_replacement() -> None:
    """A new physical meter reset is not interpreted as negative consumption."""
    indexes = [
        MeterIndex(
            timestamp=datetime(2026, 8, 1, tzinfo=LISBON).astimezone(UTC),
            value_kwh=1000.0,
            meter_serial="old",
        ),
        MeterIndex(
            timestamp=datetime(2026, 8, 2, tzinfo=LISBON).astimezone(UTC),
            value_kwh=5.0,
            meter_serial="new",
        ),
    ]

    assert _daily_real_totals(indexes) == {}


@pytest.mark.asyncio
async def test_history_sync_publishes_latest_real_consumption_day() -> None:
    """A matching raw curve publishes both real-data validation dates."""
    plan = SimpleNamespace(
        start_date=datetime(2026, 8, 7),
        end_date=datetime(2026, 8, 8),
        initial_sum=0.0,
        after=None,
        needs_full_import=False,
        pending_days=set(),
        matching_days=set(),
        last_real_data_day=None,
    )
    store = MagicMock()
    store.async_save = AsyncMock()
    coordinator = SimpleNamespace(
        cpe=CPE,
        client=MagicMock(),
        set_last_real_data_day=MagicMock(),
        set_last_matching_15min_data_day=MagicMock(),
    )
    indexes = [
        MeterIndex(
            timestamp=datetime(2026, 8, 7, tzinfo=LISBON).astimezone(UTC),
            value_kwh=100.0,
            meter_serial="meter",
            register_count=3,
        ),
        MeterIndex(
            timestamp=datetime(2026, 8, 8, tzinfo=LISBON).astimezone(UTC),
            value_kwh=106.0,
            meter_serial="meter",
            register_count=3,
        ),
    ]
    readings = [
        ConsumptionReading(
            timestamp=(
                datetime(2026, 8, 7, 0, 15) + timedelta(minutes=15 * index)
            )
            .replace(tzinfo=LISBON)
            .astimezone(UTC),
            value_wh=62.5,
        )
        for index in range(96)
    ]

    with (
        patch(
            "custom_components.eredes.historical._async_build_import_plan",
            AsyncMock(return_value=(plan, store)),
        ),
        patch(
            "custom_components.eredes.historical._async_fetch_history",
            AsyncMock(return_value=readings),
        ),
        patch(
            "custom_components.eredes.historical._async_fetch_meter_indexes",
            AsyncMock(return_value=indexes),
        ),
        patch(
            "custom_components.eredes.historical._async_persist_statistics",
            AsyncMock(return_value=True),
        ),
    ):
        completed = await async_import_historical_data(MagicMock(), coordinator)

    assert completed is True
    coordinator.set_last_real_data_day.assert_called_once_with(date(2026, 8, 7))
    coordinator.set_last_matching_15min_data_day.assert_called_once_with(
        date(2026, 8, 7)
    )


@pytest.mark.asyncio
async def test_statistics_import_waits_for_commit_and_verifies_boundaries() -> None:
    """A queued import is only successful after recorder commit and read-back."""
    stat_id = statistic_id(CPE)
    statistics = _aggregate_to_hourly_statistics(
        [_reading(0, 15, 1000.0), _reading(1, 15, 2000.0)]
    )
    recorder = MagicMock()
    recorder.async_block_till_done = AsyncMock()
    recorder.async_add_executor_job = AsyncMock(
        side_effect=[
            {stat_id: [{"state": 1.0, "sum": 1.0}]},
            {stat_id: [{"state": 2.0, "sum": 3.0}]},
        ]
    )

    verified = await _async_verify_statistics_import(
        MagicMock(), recorder, stat_id, statistics
    )

    assert verified is True
    recorder.async_block_till_done.assert_awaited_once_with()
    assert recorder.async_add_executor_job.await_count == 2


@pytest.mark.asyncio
async def test_statistics_import_verification_rejects_missing_rows() -> None:
    """A recorder job that commits no rows must not be considered successful."""
    stat_id = statistic_id(CPE)
    statistics = _aggregate_to_hourly_statistics([_reading(0, 15, 1000.0)])
    recorder = MagicMock()
    recorder.async_block_till_done = AsyncMock()
    recorder.async_add_executor_job = AsyncMock(return_value={})

    verified = await _async_verify_statistics_import(
        MagicMock(), recorder, stat_id, statistics
    )

    assert verified is False


def test_aggregate_skips_hours_at_or_before_cutoff() -> None:
    """Hours already imported (<= after) are dropped so they aren't re-counted."""
    readings = [
        _reading(0, 15, 1000.0),
        _reading(1, 15, 1000.0),
        _reading(2, 15, 1000.0),
    ]
    after = datetime(2026, 1, 1, 1, tzinfo=UTC)

    stats = _aggregate_to_hourly_statistics(readings, initial_sum=5.0, after=after)

    # Only hour 2 survives; its sum continues from the seed.
    assert [s["start"] for s in stats] == [datetime(2026, 1, 1, 2, tzinfo=UTC)]
    assert stats[0]["sum"] == 6.0


@pytest.mark.asyncio
async def test_fetch_history_uses_full_months_then_daily_partial_month() -> None:
    """Backfill mirrors portal month ranges and proven live daily ranges."""
    calls: list[tuple[datetime, datetime]] = []
    before_cutoff = ConsumptionReading(
        timestamp=datetime(2025, 8, 5, 12, 15, tzinfo=UTC), value_wh=1000.0
    )
    after_cutoff = ConsumptionReading(
        timestamp=datetime(2025, 8, 10, 12, 15, tzinfo=UTC), value_wh=1000.0
    )

    async def fetch_consumption(_cpe, start, end):
        calls.append((start, end))
        readings = [before_cutoff, after_cutoff] if len(calls) == 1 else []
        return SimpleNamespace(readings=readings)

    client = MagicMock()
    client.get_consumption = AsyncMock(side_effect=fetch_consumption)
    coordinator = SimpleNamespace(cpe=CPE, client=client)

    result = await _async_fetch_history(
        coordinator,
        statistic_id(CPE),
        datetime(2025, 8, 10, 0, 0),
        datetime(2025, 10, 2, 12, 34),
    )

    assert result == [after_cutoff]
    assert calls == [
        (datetime(2025, 8, 1, 0, 15), datetime(2025, 9, 1, 0, 0)),
        (datetime(2025, 9, 1, 0, 15), datetime(2025, 10, 1, 0, 0)),
        (datetime(2025, 10, 1, 0, 15), datetime(2025, 10, 2, 0, 0)),
        (datetime(2025, 10, 2, 0, 15), datetime(2025, 10, 2, 12, 34)),
    ]


@pytest.mark.asyncio
async def test_partial_recent_history_triggers_full_year_repair() -> None:
    """A lone recent statistic must not make an incomplete year look complete."""
    now = datetime.now()
    recent = (
        datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
        - timedelta(hours=1)
    )
    stat_id = statistic_id(CPE)

    async def executor_job(func, *_args):
        if func.__name__ == "get_last_statistics":
            return {stat_id: [{"start": recent.timestamp(), "sum": 10.0}]}
        if func.__name__ == "statistics_during_period":
            return {stat_id: [{"state": 1.0, "sum": 1.0}]}
        raise AssertionError(f"Unexpected recorder query: {func.__name__}")

    recorder = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=executor_job),
        async_block_till_done=AsyncMock(),
        async_clear_statistics=MagicMock(),
    )
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()
    call_count = 0

    async def fetch_consumption(*_args):
        nonlocal call_count
        call_count += 1
        readings = (
            [ConsumptionReading(timestamp=recent, value_wh=1000.0)]
            if call_count == 1
            else []
        )
        return SimpleNamespace(readings=readings)

    client = MagicMock()
    client.get_consumption = AsyncMock(side_effect=fetch_consumption)
    client.get_meter_indexes = AsyncMock(return_value=[])
    coordinator = SimpleNamespace(
        cpe=CPE,
        client=client,
        set_last_matching_15min_data_day=MagicMock(),
    )

    with (
        patch(
            "custom_components.eredes.historical.get_instance", return_value=recorder
        ),
        patch("custom_components.eredes.historical.Store", return_value=store),
        patch(
            "custom_components.eredes.historical.async_add_external_statistics"
        ) as add_statistics,
    ):
        completed = await async_import_historical_data(MagicMock(), coordinator)

    assert completed is True
    metadata = add_statistics.call_args.args[1]
    assert metadata["unit_class"] == "energy"
    first_start = client.get_consumption.await_args_list[0].args[1]
    assert first_start <= now - timedelta(days=364)
    store.async_save.assert_awaited_once_with(
        {
            "version": HISTORY_IMPORT_VERSION,
            "pending_reconciliation_days": [],
            "matching_15min_data_days": [],
            "last_real_data_day": None,
        }
    )


@pytest.mark.asyncio
async def test_pending_reconciliation_extends_refetch_window() -> None:
    """A flagged old day keeps being fetched even after it leaves the rolling tail."""
    now = datetime.now()
    recent = (
        datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
        - timedelta(hours=1)
    )
    pending_day = (now - timedelta(days=30)).date()
    stat_id = statistic_id(CPE)

    async def executor_job(func, *_args):
        if func.__name__ == "get_last_statistics":
            return {stat_id: [{"start": recent.timestamp(), "sum": 10.0}]}
        if func.__name__ == "statistics_during_period":
            return {stat_id: [{"sum": 5.0}]}
        raise AssertionError(f"Unexpected recorder query: {func.__name__}")

    recorder = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=executor_job)
    )
    store = MagicMock()
    store.async_load = AsyncMock(
        return_value={
            "version": HISTORY_IMPORT_VERSION,
            "pending_reconciliation_days": [pending_day.isoformat()],
        }
    )
    store.async_save = AsyncMock()
    client = MagicMock()
    client.get_consumption = AsyncMock(return_value=SimpleNamespace(readings=[]))
    coordinator = SimpleNamespace(cpe=CPE, client=client)

    with (
        patch(
            "custom_components.eredes.historical.get_instance", return_value=recorder
        ),
        patch("custom_components.eredes.historical.Store", return_value=store),
        patch("custom_components.eredes.historical.async_add_external_statistics"),
    ):
        completed = await async_import_historical_data(MagicMock(), coordinator)

    assert completed is True
    first_start = client.get_consumption.await_args_list[0].args[1]
    assert first_start.date() <= pending_day


@pytest.mark.asyncio
async def test_reconciled_day_is_persisted_as_pending() -> None:
    """A corrected day remains explicitly flagged for a later raw-data refetch."""
    recorder = SimpleNamespace(
        async_add_executor_job=AsyncMock(return_value={}),
    )
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()
    coordinator = SimpleNamespace(
        cpe=CPE,
        client=MagicMock(),
        set_last_real_data_day=MagicMock(),
        set_last_matching_15min_data_day=MagicMock(),
    )
    readings = [
        ConsumptionReading(
            timestamp=(
                datetime(2026, 8, 1, 0, 15) + timedelta(minutes=15 * index)
            )
            .replace(tzinfo=LISBON)
            .astimezone(UTC),
            value_wh=250.0,
        )
        for index in range(96)
    ]
    indexes = [
        MeterIndex(
            timestamp=datetime(2026, 8, 1, tzinfo=LISBON).astimezone(UTC),
            value_kwh=10609.0,
            meter_serial="12345678",
            register_count=3,
        ),
        MeterIndex(
            timestamp=datetime(2026, 8, 2, tzinfo=LISBON).astimezone(UTC),
            value_kwh=10621.0,
            meter_serial="12345678",
            register_count=3,
        ),
    ]

    with (
        patch(
            "custom_components.eredes.historical.get_instance", return_value=recorder
        ),
        patch("custom_components.eredes.historical.Store", return_value=store),
        patch(
            "custom_components.eredes.historical._async_fetch_history",
            AsyncMock(return_value=readings),
        ),
        patch(
            "custom_components.eredes.historical._async_fetch_meter_indexes",
            AsyncMock(return_value=indexes),
        ),
        patch(
            "custom_components.eredes.historical._async_persist_statistics",
            AsyncMock(return_value=True),
        ),
    ):
        completed = await async_import_historical_data(MagicMock(), coordinator)

    assert completed is True
    store.async_save.assert_awaited_once_with(
        {
            "version": HISTORY_IMPORT_VERSION,
            "pending_reconciliation_days": ["2026-08-01"],
            "matching_15min_data_days": [],
            "last_real_data_day": "2026-08-01",
        }
    )


@pytest.mark.asyncio
async def test_current_history_version_resumes_from_latest_statistic() -> None:
    """A completed full backfill uses the inexpensive resume path on restarts."""
    now = datetime.now()
    recent = (
        datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
        - timedelta(hours=1)
    )
    stat_id = statistic_id(CPE)

    async def executor_job(func, *_args):
        if func.__name__ == "get_last_statistics":
            return {stat_id: [{"start": recent.timestamp(), "sum": 10.0}]}
        if func.__name__ == "statistics_during_period":
            return {stat_id: [{"sum": 5.0}]}
        raise AssertionError(f"Unexpected recorder query: {func.__name__}")

    recorder = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=executor_job)
    )
    store = MagicMock()
    restored_real_day = (now - timedelta(days=4)).date()
    restored_matching_day = (now - timedelta(days=10)).date()
    store.async_load = AsyncMock(
        return_value={
            "version": HISTORY_IMPORT_VERSION,
            "matching_15min_data_days": [restored_matching_day.isoformat()],
            "last_real_data_day": restored_real_day.isoformat(),
        }
    )
    store.async_save = AsyncMock()
    client = MagicMock()
    client.get_consumption = AsyncMock(return_value=SimpleNamespace(readings=[]))
    coordinator = SimpleNamespace(
        cpe=CPE,
        client=client,
        set_last_real_data_day=MagicMock(),
        set_last_matching_15min_data_day=MagicMock(),
    )

    with (
        patch(
            "custom_components.eredes.historical.get_instance", return_value=recorder
        ),
        patch("custom_components.eredes.historical.Store", return_value=store),
        patch("custom_components.eredes.historical.async_add_external_statistics"),
    ):
        completed = await async_import_historical_data(MagicMock(), coordinator)

    assert completed is True
    first_start = client.get_consumption.await_args_list[0].args[1]
    assert first_start >= now - timedelta(days=8)
    assert first_start <= now - timedelta(days=6)
    coordinator.set_last_real_data_day.assert_called_once_with(restored_real_day)
    coordinator.set_last_matching_15min_data_day.assert_called_once_with(
        restored_matching_day
    )
    store.async_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_repair_removes_stale_shifted_tail_row() -> None:
    """A versioned repair must not leave an obsolete row after the rebuilt tail."""
    stat_id = statistic_id(CPE)
    corrected_start = datetime(2026, 8, 9, 22, 0, tzinfo=UTC)
    stale_start = corrected_start + timedelta(hours=1)

    # This models the exact failure seen after the Lisbon timestamp repair: the
    # corrected last row has a larger cumulative sum, while an obsolete row from
    # the old UTC interpretation survives one hour later. Home Assistant computes
    # the displayed hourly change as current_sum - previous_sum, yielding
    # 1023.243 - 1300.0 == -276.757 kWh at the stale row.
    persisted: dict[datetime, dict[str, float]] = {
        stale_start: {"state": 0.5, "sum": 1023.243},
    }

    async def executor_job(func, *_args):
        if func.__name__ == "get_last_statistics":
            return {
                stat_id: [
                    {"start": stale_start.timestamp(), "sum": 1023.243}
                ]
            }
        if func.__name__ == "statistics_during_period":
            start = _args[1]
            if start in persisted:
                return {stat_id: [persisted[start]]}
            # Seed the pre-repair series so the current implementation rebuilds
            # the corrected tail to exactly 1300.0 kWh.
            return {stat_id: [{"sum": 1299.0}]}
        raise AssertionError(f"Unexpected recorder query: {func.__name__}")

    recorder = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=executor_job),
        async_block_till_done=AsyncMock(),
        async_clear_statistics=MagicMock(side_effect=lambda _ids: persisted.clear()),
    )
    store = MagicMock()
    store.async_load = AsyncMock(
        return_value={"version": HISTORY_IMPORT_VERSION - 1}
    )
    store.async_save = AsyncMock()
    coordinator = SimpleNamespace(
        cpe=CPE,
        client=MagicMock(),
        set_last_matching_15min_data_day=MagicMock(),
    )
    coordinator.client.get_meter_indexes = AsyncMock(return_value=[])
    corrected_reading = ConsumptionReading(
        timestamp=corrected_start + timedelta(minutes=15),
        value_wh=1000.0,
    )

    def add_statistics(_hass, _metadata, statistics):
        for statistic in statistics:
            persisted[statistic["start"]] = {
                "state": statistic["state"],
                "sum": statistic["sum"],
            }

    with (
        patch(
            "custom_components.eredes.historical.get_instance", return_value=recorder
        ),
        patch("custom_components.eredes.historical.Store", return_value=store),
        patch(
            "custom_components.eredes.historical._async_fetch_history",
            AsyncMock(return_value=[corrected_reading]),
        ),
        patch(
            "custom_components.eredes.historical.async_add_external_statistics",
            side_effect=add_statistics,
        ),
    ):
        completed = await async_import_historical_data(MagicMock(), coordinator)

    assert completed is True
    ordered_sums = [persisted[start]["sum"] for start in sorted(persisted)]
    assert all(current >= previous for previous, current in pairwise(ordered_sums))


@pytest.mark.asyncio
async def test_empty_full_history_is_not_marked_complete() -> None:
    """An empty full-window response must be retried rather than accepted."""

    async def executor_job(func, *_args):
        if func.__name__ in {"get_last_statistics", "statistics_during_period"}:
            return {}
        raise AssertionError(f"Unexpected recorder query: {func.__name__}")

    recorder = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=executor_job)
    )
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()
    client = MagicMock()
    client.get_consumption = AsyncMock(return_value=SimpleNamespace(readings=[]))
    coordinator = SimpleNamespace(cpe=CPE, client=client)
    add_statistics = MagicMock()

    with (
        patch(
            "custom_components.eredes.historical.get_instance", return_value=recorder
        ),
        patch("custom_components.eredes.historical.Store", return_value=store),
        patch(
            "custom_components.eredes.historical.async_add_external_statistics",
            add_statistics,
        ),
    ):
        completed = await async_import_historical_data(MagicMock(), coordinator)

    assert completed is False
    add_statistics.assert_not_called()
    store.async_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_history_chunk_does_not_commit_partial_import() -> None:
    """One failed chunk invalidates the run instead of committing partial history."""
    async def executor_job(func, *_args):
        if func.__name__ in {"get_last_statistics", "statistics_during_period"}:
            return {}
        raise AssertionError(f"Unexpected recorder query: {func.__name__}")

    call_count = 0

    async def fetch_consumption(*_args):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("historical chunk failed")
        readings = [_reading(0, 0, 1000.0)] if call_count == 1 else []
        return SimpleNamespace(readings=readings)

    recorder = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=executor_job)
    )
    store = MagicMock()
    store.async_load = AsyncMock(return_value=None)
    store.async_save = AsyncMock()
    client = MagicMock()
    client.get_consumption = AsyncMock(side_effect=fetch_consumption)
    coordinator = SimpleNamespace(cpe=CPE, client=client)
    add_statistics = MagicMock()

    with (
        patch(
            "custom_components.eredes.historical.get_instance", return_value=recorder
        ),
        patch("custom_components.eredes.historical.Store", return_value=store),
        patch(
            "custom_components.eredes.historical.async_add_external_statistics",
            add_statistics,
        ),
    ):
        completed = await async_import_historical_data(MagicMock(), coordinator)

    assert completed is False
    assert call_count == 2
    add_statistics.assert_not_called()
    store.async_save.assert_not_awaited()
