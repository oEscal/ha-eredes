"""Tests for live provisional current-day energy tracking."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import UnitOfEnergy
from homeassistant.core import State

from custom_components.eredes.historical import statistic_id
from custom_components.eredes.provisional import ProvisionalEnergyTracker

CPE = "PT0002000012345678AB"


def _energy_state(
    entity_id: str,
    value: float,
    changed_at: datetime,
    unit: str = UnitOfEnergy.KILO_WATT_HOUR,
) -> State:
    """Build one cumulative energy state."""
    return State(
        entity_id,
        str(value),
        {"unit_of_measurement": unit, "state_class": "total_increasing"},
        last_changed=changed_at,
        last_reported=changed_at,
        last_updated=changed_at,
    )


@pytest.mark.asyncio
@pytest.mark.freeze_time("2026-08-12 16:00:00+00:00")
async def test_restart_reconstructs_today_from_raw_states_and_live_values(hass) -> None:
    """Current-day usage includes live values without 5-minute statistics."""
    stat_id = statistic_id(CPE)
    today_start = datetime(2026, 8, 11, 23, 0, tzinfo=UTC)
    earlier = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    now = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)

    manager = SimpleNamespace(
        data={
            "energy_sources": [],
            "device_consumption": [
                {"stat_consumption": "sensor.kitchen_energy"},
                {
                    "stat_consumption": "sensor.kettle_energy",
                    "included_in_stat": "sensor.kitchen_energy",
                },
                {"stat_consumption": "sensor.office_energy"},
                {"stat_consumption": stat_id},
            ],
        }
    )
    history = {
        "sensor.kitchen_energy": [
            _energy_state("sensor.kitchen_energy", 100.0, today_start),
            _energy_state("sensor.kitchen_energy", 100.5, earlier),
        ],
        "sensor.office_energy": [
            _energy_state("sensor.office_energy", 50.0, today_start),
            _energy_state("sensor.office_energy", 50.2, earlier),
        ],
    }
    hass.states.async_set(
        "sensor.kitchen_energy",
        "100.8",
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )
    hass.states.async_set(
        "sensor.office_energy",
        "50.4",
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )

    recorder = SimpleNamespace(async_add_executor_job=AsyncMock())

    def executor_result(func, *_args):
        if func.__name__ == "get_significant_states":
            return history
        assert func.__name__ == "statistics_during_period"
        period = _args[4]
        assert period == "day", "provisional tracking must not query 5-minute stats"
        return {stat_id: [{"start": today_start - timedelta(days=1), "sum": 1000.0}]}

    recorder.async_add_executor_job.side_effect = executor_result
    statistics_lock = asyncio.Lock()
    tracker = ProvisionalEnergyTracker(hass, CPE, statistics_lock)

    with (
        patch(
            "custom_components.eredes.provisional.async_get_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.eredes.provisional.get_instance",
            return_value=recorder,
        ),
        patch(
            "custom_components.eredes.provisional._async_persist_statistics",
            AsyncMock(return_value=True),
        ) as persist_statistics,
    ):
        assert await tracker.async_reconcile() is True

    statistics = persist_statistics.await_args.args[3]
    assert [(row["start"], row["state"], row["sum"]) for row in statistics] == [
        (earlier, pytest.approx(0.7), pytest.approx(1000.7)),
        (now, pytest.approx(0.5), pytest.approx(1001.2)),
    ]
    assert tracker.device_entity_ids == {
        "sensor.kitchen_energy",
        "sensor.office_energy",
    }


@pytest.mark.asyncio
@pytest.mark.freeze_time("2026-08-12 16:00:00+00:00")
async def test_device_state_change_updates_provisional_energy_immediately(hass) -> None:
    """A tracked device state change schedules an immediate live-statistic update."""
    stat_id = statistic_id(CPE)
    today_start = datetime(2026, 8, 11, 23, 0, tzinfo=UTC)
    now = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)
    manager = SimpleNamespace(
        data={
            "energy_sources": [],
            "device_consumption": [{"stat_consumption": "sensor.kitchen_energy"}],
        }
    )
    history = {
        "sensor.kitchen_energy": [
            _energy_state("sensor.kitchen_energy", 100.0, today_start),
        ]
    }
    hass.states.async_set(
        "sensor.kitchen_energy",
        "100.8",
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )
    recorder = SimpleNamespace(async_add_executor_job=AsyncMock())

    def executor_result(func, *_args):
        if func.__name__ == "get_significant_states":
            return history
        assert func.__name__ == "statistics_during_period"
        assert _args[4] == "day"
        return {stat_id: [{"start": today_start - timedelta(days=1), "sum": 1000.0}]}

    recorder.async_add_executor_job.side_effect = executor_result
    tracker = ProvisionalEnergyTracker(hass, CPE, asyncio.Lock())
    delayed_actions = []

    def call_later(_hass, _delay, action):
        delayed_actions.append(action)
        return MagicMock()

    with (
        patch(
            "custom_components.eredes.provisional.async_get_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.eredes.provisional.get_instance",
            return_value=recorder,
        ),
        patch(
            "custom_components.eredes.provisional.async_call_later",
            side_effect=call_later,
        ),
        patch(
            "custom_components.eredes.provisional._async_persist_statistics",
            AsyncMock(return_value=True),
        ) as persist_statistics,
    ):
        assert await tracker.async_reconcile() is True
        persist_statistics.reset_mock()

        hass.states.async_set(
            "sensor.kitchen_energy",
            "101.0",
            {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
        )
        await hass.async_block_till_done()

        assert len(delayed_actions) == 1
        await delayed_actions[0](now)

    statistics = persist_statistics.await_args.args[3]
    assert statistics[-1]["start"] == now
    assert statistics[-1]["state"] == pytest.approx(1.0)
    assert statistics[-1]["sum"] == pytest.approx(1001.0)


@pytest.mark.asyncio
@pytest.mark.freeze_time("2026-08-12 12:00:00+00:00")
async def test_reconcile_handles_wh_units_and_total_increasing_reset(hass) -> None:
    """Raw state reconstruction converts units and counts post-reset growth."""
    stat_id = statistic_id(CPE)
    today_start = datetime(2026, 8, 11, 23, 0, tzinfo=UTC)
    manager = SimpleNamespace(
        data={
            "energy_sources": [],
            "device_consumption": [{"stat_consumption": "sensor.device_energy"}],
        }
    )
    history = {
        "sensor.device_energy": [
            _energy_state("sensor.device_energy", 10000, today_start, "Wh"),
            _energy_state(
                "sensor.device_energy",
                12400,
                datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
                "Wh",
            ),
            _energy_state(
                "sensor.device_energy",
                100,
                datetime(2026, 8, 12, 11, 0, tzinfo=UTC),
                "Wh",
            ),
        ]
    }
    hass.states.async_set(
        "sensor.device_energy",
        "100",
        {"unit_of_measurement": "Wh", "state_class": "total_increasing"},
    )
    recorder = SimpleNamespace(async_add_executor_job=AsyncMock())

    def executor_result(func, *_args):
        if func.__name__ == "get_significant_states":
            return history
        assert _args[4] == "day"
        return {stat_id: [{"start": today_start - timedelta(days=1), "sum": 500.0}]}

    recorder.async_add_executor_job.side_effect = executor_result
    tracker = ProvisionalEnergyTracker(hass, CPE, asyncio.Lock())

    with (
        patch(
            "custom_components.eredes.provisional.async_get_manager",
            AsyncMock(return_value=manager),
        ),
        patch(
            "custom_components.eredes.provisional.get_instance",
            return_value=recorder,
        ),
        patch(
            "custom_components.eredes.provisional._async_persist_statistics",
            AsyncMock(return_value=True),
        ) as persist_statistics,
    ):
        assert await tracker.async_reconcile() is True

    statistics = persist_statistics.await_args.args[3]
    assert [(row["state"], row["sum"]) for row in statistics] == [
        (pytest.approx(2.4), pytest.approx(502.4)),
        (pytest.approx(0.1), pytest.approx(502.5)),
    ]
