"""Recorder-backed regression tests for E-REDES external statistics."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.components import recorder as recorder_component
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import statistics_during_period

from custom_components.eredes.historical import (
    _async_persist_statistics,
    _statistics_metadata,
    statistic_id,
)

CPE = "PT0002000012345678AB"


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url: str) -> None:
    """Resolve the real Recorder database before the Home Assistant fixture."""
    del recorder_db_url


@pytest.mark.asyncio
async def test_existing_external_statistic_row_is_replaced(
    hass,
    recorder_mock,
) -> None:
    """Re-importing the same hour must replace the persisted provisional row."""
    del recorder_mock
    stat_id = statistic_id(CPE)
    start = datetime(2026, 8, 11, 23, 0, tzinfo=UTC)

    assert await _async_persist_statistics(
        hass,
        stat_id,
        _statistics_metadata(CPE),
        [StatisticData(start=start, state=0.171, sum=0.171)],
    )
    assert await _async_persist_statistics(
        hass,
        stat_id,
        _statistics_metadata(CPE),
        [StatisticData(start=start, state=0.290, sum=1315.905)],
    )

    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        start + timedelta(hours=1),
        {stat_id},
        "hour",
        None,
        {"state", "sum"},
    )
    assert rows[stat_id][0]["state"] == pytest.approx(0.290)
    assert rows[stat_id][0]["sum"] == pytest.approx(1315.905)


@pytest.mark.asyncio
async def test_external_statistic_verification_waits_for_inflight_import(
    hass,
    recorder_mock,
    monkeypatch,
) -> None:
    """Verification must tolerate an import already dequeued by Recorder."""
    stat_id = statistic_id(CPE)
    start = datetime(2026, 8, 11, 23, 0, tzinfo=UTC)

    assert await _async_persist_statistics(
        hass,
        stat_id,
        _statistics_metadata(CPE),
        [StatisticData(start=start, state=0.171, sum=0.171)],
    )

    original_import_statistics = recorder_component.statistics.import_statistics

    def delayed_import_statistics(*args, **kwargs):
        time.sleep(0.2)
        return original_import_statistics(*args, **kwargs)

    monkeypatch.setattr(
        recorder_component.statistics,
        "import_statistics",
        delayed_import_statistics,
    )
    monkeypatch.setattr(recorder_mock, "async_get_commit_future", lambda: None)

    assert await _async_persist_statistics(
        hass,
        stat_id,
        _statistics_metadata(CPE),
        [StatisticData(start=start, state=0.290, sum=1315.905)],
    )
