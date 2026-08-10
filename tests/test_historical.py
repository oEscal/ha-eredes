"""Tests for E-REDES historical statistics import."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.recorder.statistics import valid_statistic_id

from custom_components.eredes.eredes_api.models import ConsumptionReading
from custom_components.eredes.historical import (
    _aggregate_to_hourly_statistics,
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


def test_aggregate_buckets_by_utc_hour() -> None:
    """15-minute readings roll up into cumulative, top-of-hour UTC statistics."""
    readings = [
        _reading(0, 0, 250.0),
        _reading(0, 15, 250.0),
        _reading(0, 30, 250.0),
        _reading(0, 45, 250.0),  # hour 0 -> 1.0 kWh
        _reading(1, 0, 500.0),
        _reading(1, 30, 500.0),  # hour 1 -> 1.0 kWh
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


def test_aggregate_skips_hours_at_or_before_cutoff() -> None:
    """Hours already imported (<= after) are dropped so they aren't re-counted."""
    readings = [_reading(0, 0, 1000.0), _reading(1, 0, 1000.0), _reading(2, 0, 1000.0)]
    after = datetime(2026, 1, 1, 1, tzinfo=UTC)

    stats = _aggregate_to_hourly_statistics(readings, initial_sum=5.0, after=after)

    # Only hour 2 survives; its sum continues from the seed.
    assert [s["start"] for s in stats] == [datetime(2026, 1, 1, 2, tzinfo=UTC)]
    assert stats[0]["sum"] == 6.0


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
            return {}
        raise AssertionError(f"Unexpected recorder query: {func.__name__}")

    recorder = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=executor_job)
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
    assert first_start <= now - timedelta(days=364)
    store.async_save.assert_awaited_once_with({"version": 1})


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
        raise AssertionError(f"Unexpected recorder query: {func.__name__}")

    recorder = SimpleNamespace(
        async_add_executor_job=AsyncMock(side_effect=executor_job)
    )
    store = MagicMock()
    store.async_load = AsyncMock(return_value={"version": 1})
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
    assert first_start >= now - timedelta(days=3)
    store.async_save.assert_not_awaited()


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
