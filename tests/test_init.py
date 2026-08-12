"""Tests for E-REDES config entry migration."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eredes import (
    _handle_provisional_sync,
    async_migrate_entry,
    async_setup_entry,
)
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


async def test_provisional_tick_reconciles_live_tracker() -> None:
    """The periodic fallback asks the live tracker to rebuild current-day state."""
    entry = MagicMock()

    with patch(
        "custom_components.eredes._async_import_provisional_data",
        AsyncMock(),
    ) as import_provisional:
        await _handle_provisional_sync(MagicMock(), entry, MagicMock())

    import_provisional.assert_awaited_once_with(entry)


async def test_setup_does_not_wait_for_remote_or_provisional_work(
    hass: HomeAssistant,
) -> None:
    """Slow remote/local work must never hold config-entry setup open."""
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
    coordinator.async_refresh = AsyncMock(
        side_effect=AssertionError("remote refresh must run after setup")
    )
    background_task = MagicMock()
    background_task.done.return_value = False
    created_names: list[str] = []

    def create_background_task(_hass, target, *, name: str):
        created_names.append(name)
        target.close()
        return background_task

    with (
        patch("custom_components.eredes.ERedesCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(
            entry,
            "async_create_background_task",
            side_effect=create_background_task,
        ),
        patch("custom_components.eredes.async_track_time_change"),
        patch("custom_components.eredes.async_track_time_interval"),
        patch(
            "custom_components.eredes._async_import_provisional_data",
            AsyncMock(
                side_effect=AssertionError(
                    "provisional refresh must run after setup"
                )
            ),
        ),
    ):
        assert await async_setup_entry(hass, entry) is True

    coordinator.async_refresh.assert_not_awaited()
    assert created_names == [
        "eredes_initial_provisional_refresh",
        "eredes_initial_remote_refresh",
    ]


async def test_setup_keeps_local_provisional_refresh_when_remote_auth_is_unavailable(
    hass: HomeAssistant,
) -> None:
    """Remote authentication must not prevent the local provisional scheduler."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_ACCESS_TOKEN: TOKEN, CONF_CPE: CPE},
        options={"provisional_refresh_interval_minutes": 7},
        unique_id=CPE,
    )
    entry.add_to_hass(hass)

    coordinator = MagicMock()
    coordinator.cpe = CPE
    coordinator.client = MagicMock()
    coordinator.async_refresh = AsyncMock()
    coordinator.last_update_success = False
    coordinator.async_config_entry_first_refresh = AsyncMock(
        side_effect=AssertionError("fatal first refresh must not be used")
    )
    background_task = MagicMock()
    background_task.done.return_value = False
    created_coroutines = {}

    def create_background_task(_hass, target, *, name: str):
        created_coroutines[name] = target
        return background_task

    with (
        patch("custom_components.eredes.ERedesCoordinator", return_value=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(
            entry,
            "async_create_background_task",
            side_effect=create_background_task,
        ),
        patch("custom_components.eredes.async_track_time_change"),
        patch(
            "custom_components.eredes.async_track_time_interval",
        ) as track_time_interval,
        patch(
            "custom_components.eredes._async_import_provisional_data",
            AsyncMock(),
        ) as import_provisional,
        patch("custom_components.eredes._start_historical_import") as start_history,
    ):
        assert await async_setup_entry(hass, entry) is True

    coordinator.async_refresh.assert_not_awaited()
    import_provisional.assert_not_awaited()

    await created_coroutines["eredes_initial_remote_refresh"]
    await created_coroutines["eredes_initial_provisional_refresh"]

    coordinator.async_refresh.assert_awaited_once_with()
    coordinator.async_config_entry_first_refresh.assert_not_awaited()
    import_provisional.assert_awaited_once_with(entry)
    start_history.assert_not_called()
    track_time_interval.assert_called_once()
    assert track_time_interval.call_args.args[2] == timedelta(minutes=7)


async def test_setup_schedules_initial_jobs_and_daily_history_5am(
    hass: HomeAssistant,
) -> None:
    """Setup queues non-blocking initial jobs and daily history at 05:00."""
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
    coordinator.async_refresh = AsyncMock()
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
        patch(
            "custom_components.eredes._async_import_provisional_data",
            AsyncMock(),
        ) as import_provisional,
    ):
        assert await async_setup_entry(hass, entry) is True

        assert create_background_task_mock.call_count == 2
        assert [
            call.kwargs["name"]
            for call in create_background_task_mock.call_args_list
        ] == [
            "eredes_initial_provisional_refresh",
            "eredes_initial_remote_refresh",
        ]
        track_time_change.assert_called_once()
        track_time_interval.assert_called_once()
        assert track_time_interval.call_args.args[2] == timedelta(minutes=15)
        assert track_time_change.call_args.kwargs == {
            "hour": 5,
            "minute": 0,
            "second": 0,
        }

        provisional_callback = track_time_interval.call_args.args[1]
        provisional_job = provisional_callback(MagicMock())
        assert provisional_job is not None
        await provisional_job
        import_provisional.assert_awaited_once_with(entry)
        assert create_background_task_mock.call_count == 2

        daily_callback = track_time_change.call_args.args[1]
        daily_callback(MagicMock())
        assert create_background_task_mock.call_count == 3

        daily_callback(MagicMock())
        assert create_background_task_mock.call_count == 3

        background_task.done.return_value = True
        daily_callback(MagicMock())
        assert create_background_task_mock.call_count == 4
        assert len(created_coroutines) == 4


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
    coordinator.async_refresh = AsyncMock()
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
        assert create_background_task_mock.call_count == 4


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
    coordinator.async_refresh = AsyncMock()
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
        assert create_background_task_mock.call_count == 2

        daily_callback(MagicMock())
        assert create_background_task_mock.call_count == 3


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
