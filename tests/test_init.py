"""Tests for E-REDES config entry migration."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eredes import async_migrate_entry, async_setup_entry
from custom_components.eredes.const import (
    CONF_ACCESS_TOKEN,
    CONF_CPE,
    CONF_HISTORY_SYNC_FREQUENCY,
    CONF_HISTORY_SYNC_INTERVAL_DAYS,
    CONF_HISTORY_SYNC_TIME,
    DOMAIN,
    HISTORY_SYNC_FREQUENCY_HOURLY,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

CPE = "PT0002000012345678AB"
TOKEN = "eyJ.mock.jwt"


async def test_setup_runs_history_in_background_and_schedules_daily_5am(
    hass: HomeAssistant,
) -> None:
    """History sync runs off bootstrap and repeats every day at 05:00 local time."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_ACCESS_TOKEN: TOKEN, CONF_CPE: CPE},
        unique_id=CPE,
    )
    entry.add_to_hass(hass)

    coordinator = MagicMock()
    coordinator.cpe = CPE
    coordinator.client = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    background_task = MagicMock()
    background_task.done.return_value = False
    created_coroutines = []

    def create_background_task(_hass, target, *, name: str):
        del name
        created_coroutines.append(target)
        target.close()
        return background_task

    with (
        patch("custom_components.eredes.ERedesCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(
            entry,
            "async_create_background_task",
            side_effect=create_background_task,
        ) as create_background_task_mock,
        patch(
            "custom_components.eredes.async_track_time_change",
            create=True,
        ) as track_time_change,
        patch(
            "custom_components.eredes.async_track_time_interval",
            create=True,
        ) as track_time_interval,
    ):
        assert await async_setup_entry(hass, entry) is True

        create_background_task_mock.assert_called_once()
        assert (
            create_background_task_mock.call_args.kwargs["name"]
            == "eredes_historical_import"
        )
        track_time_change.assert_called_once()
        track_time_interval.assert_called_once()
        assert track_time_interval.call_args.args[2] == timedelta(minutes=15)
        assert track_time_change.call_args.kwargs == {
            "hour": 5,
            "minute": 0,
            "second": 0,
        }

        daily_callback = track_time_change.call_args.args[1]
        daily_callback(MagicMock())
        create_background_task_mock.assert_called_once()

        background_task.done.return_value = True
        daily_callback(MagicMock())
        assert create_background_task_mock.call_count == 2
        assert len(created_coroutines) == 2


async def test_setup_uses_hourly_history_frequency(
    hass: HomeAssistant,
) -> None:
    """Hourly mode synchronizes at the configured minute of every hour."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_ACCESS_TOKEN: TOKEN, CONF_CPE: CPE},
        options={
            CONF_HISTORY_SYNC_FREQUENCY: HISTORY_SYNC_FREQUENCY_HOURLY,
            CONF_HISTORY_SYNC_TIME: "03:30:00",
            CONF_HISTORY_SYNC_INTERVAL_DAYS: 3,
        },
        unique_id=CPE,
    )
    entry.add_to_hass(hass)

    coordinator = MagicMock()
    coordinator.cpe = CPE
    coordinator.client = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    background_task = MagicMock()
    background_task.done.return_value = True

    def create_background_task(_hass, target, *, name: str):
        del name
        target.close()
        return background_task

    with (
        patch("custom_components.eredes.ERedesCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(
            entry,
            "async_create_background_task",
            side_effect=create_background_task,
        ) as create_background_task_mock,
        patch("custom_components.eredes.async_track_time_change") as track_time_change,
        patch("custom_components.eredes.async_track_time_interval"),
    ):
        assert await async_setup_entry(hass, entry) is True

        assert track_time_change.call_args.kwargs == {
            "minute": 30,
            "second": 0,
        }
        hourly_callback = track_time_change.call_args.args[1]
        hourly_callback(MagicMock())
        hourly_callback(MagicMock())
        assert create_background_task_mock.call_count == 3


async def test_setup_uses_configured_history_schedule_and_frequency(
    hass: HomeAssistant,
) -> None:
    """Configured local time and day interval control scheduled history syncs."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_ACCESS_TOKEN: TOKEN, CONF_CPE: CPE},
        options={
            CONF_HISTORY_SYNC_TIME: "03:30:00",
            CONF_HISTORY_SYNC_INTERVAL_DAYS: 3,
        },
        unique_id=CPE,
    )
    entry.add_to_hass(hass)

    coordinator = MagicMock()
    coordinator.cpe = CPE
    coordinator.client = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    background_task = MagicMock()
    background_task.done.return_value = True

    def create_background_task(_hass, target, *, name: str):
        del name
        target.close()
        return background_task

    with (
        patch("custom_components.eredes.ERedesCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(
            entry,
            "async_create_background_task",
            side_effect=create_background_task,
        ) as create_background_task_mock,
        patch("custom_components.eredes.async_track_time_change") as track_time_change,
        patch("custom_components.eredes.async_track_time_interval"),
    ):
        assert await async_setup_entry(hass, entry) is True

        assert track_time_change.call_args.kwargs == {
            "hour": 3,
            "minute": 30,
            "second": 0,
        }
        daily_callback = track_time_change.call_args.args[1]

        daily_callback(MagicMock())
        daily_callback(MagicMock())
        create_background_task_mock.assert_called_once()

        daily_callback(MagicMock())
        assert create_background_task_mock.call_count == 2


@pytest.mark.parametrize("legacy_key", ["session_cookie", "aat_token"])
async def test_migrate_v1_legacy_token_key(
    hass: HomeAssistant, legacy_key: str
) -> None:
    """A v1 entry's legacy token key is moved to access_token and bumped to v2."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={legacy_key: TOKEN, CONF_CPE: CPE},
        unique_id=CPE,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == 2
    assert entry.data[CONF_ACCESS_TOKEN] == TOKEN
    assert entry.data[CONF_CPE] == CPE
    assert legacy_key not in entry.data


async def test_migrate_v2_is_noop(hass: HomeAssistant) -> None:
    """A current (v2) entry is left untouched."""
    data = {CONF_ACCESS_TOKEN: TOKEN, CONF_CPE: CPE}
    entry = MockConfigEntry(domain=DOMAIN, version=2, data=data, unique_id=CPE)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == 2
    assert dict(entry.data) == data


async def test_migrate_from_future_version_fails(hass: HomeAssistant) -> None:
    """A downgrade from a future version is refused."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_ACCESS_TOKEN: TOKEN, CONF_CPE: CPE},
        unique_id=CPE,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is False
