"""The E-REDES integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import time, timedelta
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_HISTORY_SYNC_FREQUENCY,
    CONF_HISTORY_SYNC_INTERVAL_DAYS,
    CONF_HISTORY_SYNC_TIME,
    DEFAULT_HISTORY_SYNC_FREQUENCY,
    DEFAULT_HISTORY_SYNC_INTERVAL_DAYS,
    DEFAULT_HISTORY_SYNC_TIME,
    HISTORY_SYNC_FREQUENCY_HOURLY,
    LEGACY_TOKEN_KEYS,
)
from .coordinator import ERedesCoordinator

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant

    from .eredes_api import ERedesClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


@dataclass
class ERedesData:
    """Runtime data for E-REDES integration."""

    coordinator: ERedesCoordinator
    client: ERedesClient
    statistics_import_lock: asyncio.Lock
    historical_import_task: asyncio.Task[None] | None = None
    history_sync_days_elapsed: int = 0


type ERedesConfigEntry = ConfigEntry[ERedesData]


async def async_setup_entry(hass: HomeAssistant, entry: ERedesConfigEntry) -> bool:
    """Set up E-REDES from a config entry."""
    _LOGGER.debug("Setting up E-REDES integration")
    coordinator = ERedesCoordinator(hass, entry)

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Store runtime data
    entry.runtime_data = ERedesData(
        coordinator=coordinator,
        client=coordinator.client,
        statistics_import_lock=asyncio.Lock(),
    )

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Synchronize once at setup, then on the user-configured local schedule.
    _start_historical_import(hass, entry)
    sync_time = time.fromisoformat(
        str(entry.options.get(CONF_HISTORY_SYNC_TIME, DEFAULT_HISTORY_SYNC_TIME))
    )
    sync_frequency = str(
        entry.options.get(CONF_HISTORY_SYNC_FREQUENCY, DEFAULT_HISTORY_SYNC_FREQUENCY)
    )
    if sync_frequency == HISTORY_SYNC_FREQUENCY_HOURLY:
        remove_history_schedule = async_track_time_change(
            hass,
            lambda now: _handle_hourly_history_sync(hass, entry, now),
            minute=sync_time.minute,
            second=sync_time.second,
        )
    else:
        remove_history_schedule = async_track_time_change(
            hass,
            lambda now: _handle_daily_history_sync(hass, entry, now),
            hour=sync_time.hour,
            minute=sync_time.minute,
            second=sync_time.second,
        )
    entry.async_on_unload(remove_history_schedule)
    remove_provisional_schedule = async_track_time_interval(
        hass,
        partial(_handle_provisional_sync, hass, entry),
        timedelta(minutes=15),
        name="E-REDES provisional current-day energy",
    )
    entry.async_on_unload(remove_provisional_schedule)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ERedesConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry to the current version.

    Version 1 stored the access token under a legacy key (``session_cookie``,
    or ``aat_token`` in the earliest builds). Version 2 stores it under
    ``access_token`` (see CONTEXT.md → Authentication).
    """
    if entry.version > 2:
        # Downgrade from a future version is not supported.
        return False

    if entry.version == 1:
        data = dict(entry.data)
        for legacy_key in LEGACY_TOKEN_KEYS:
            if legacy_key in data:
                data[CONF_ACCESS_TOKEN] = data.pop(legacy_key)
                break
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.debug("Migrated E-REDES config entry to version 2")

    return True


@callback
def _start_historical_import(
    hass: HomeAssistant,
    entry: ERedesConfigEntry,
) -> None:
    """Start a history synchronization unless one is already running."""
    existing_task = entry.runtime_data.historical_import_task
    if existing_task is not None and not existing_task.done():
        _LOGGER.debug("Historical data import already running; skipping duplicate run")
        return

    entry.runtime_data.historical_import_task = entry.async_create_background_task(
        hass,
        _async_import_historical_data(hass, entry, entry.runtime_data.coordinator),
        name="eredes_historical_import",
    )


async def _handle_provisional_sync(
    hass: HomeAssistant,
    entry: ERedesConfigEntry,
    _now: datetime,
) -> None:
    """Refresh today's provisional Energy Dashboard-derived consumption."""
    if entry.runtime_data.statistics_import_lock.locked():
        _LOGGER.debug(
            "Skipping scheduled provisional energy refresh while statistics import "
            "is already running"
        )
        return

    await _async_import_provisional_data(hass, entry, entry.runtime_data.coordinator)


@callback
def _handle_hourly_history_sync(
    hass: HomeAssistant,
    entry: ERedesConfigEntry,
    _now: datetime,
) -> None:
    """Start the hourly history synchronization."""
    _LOGGER.debug("Starting scheduled hourly historical data synchronization")
    _start_historical_import(hass, entry)


@callback
def _handle_daily_history_sync(
    hass: HomeAssistant,
    entry: ERedesConfigEntry,
    _now: datetime,
) -> None:
    """Run history synchronization when the configured day interval elapses."""
    interval_days = int(
        entry.options.get(
            CONF_HISTORY_SYNC_INTERVAL_DAYS,
            DEFAULT_HISTORY_SYNC_INTERVAL_DAYS,
        )
    )
    entry.runtime_data.history_sync_days_elapsed += 1
    if entry.runtime_data.history_sync_days_elapsed < interval_days:
        _LOGGER.debug(
            "Skipping scheduled historical data synchronization (%d/%d days)",
            entry.runtime_data.history_sync_days_elapsed,
            interval_days,
        )
        return

    _LOGGER.debug(
        "Starting scheduled historical data synchronization after %d day(s)",
        interval_days,
    )
    _start_historical_import(hass, entry)


async def _async_import_provisional_data(
    hass: HomeAssistant,
    entry: ERedesConfigEntry,
    coordinator: ERedesCoordinator,
) -> None:
    """Import provisional current-day device consumption in the background."""
    from .historical import async_import_provisional_current_day  # noqa: PLC0415

    try:
        async with entry.runtime_data.statistics_import_lock:
            await async_import_provisional_current_day(hass, coordinator)
    except Exception:
        _LOGGER.exception("Failed to import provisional current-day energy")


async def _async_import_historical_data(
    hass: HomeAssistant,
    _entry: ERedesConfigEntry,
    coordinator: ERedesCoordinator,
) -> None:
    """Import historical data in the background."""
    from .historical import async_import_historical_data  # noqa: PLC0415

    _LOGGER.debug("Starting historical data import for CPE %s", coordinator.cpe[-8:])
    try:
        async with _entry.runtime_data.statistics_import_lock:
            completed = await async_import_historical_data(hass, coordinator)
            if completed:
                _entry.runtime_data.history_sync_days_elapsed = 0
                _LOGGER.debug("Historical data import completed")
            else:
                _LOGGER.warning(
                    "Historical data import incomplete; it will be retried at the next "
                    "daily synchronization or integration setup"
                )
    except Exception:
        _LOGGER.exception("Failed to import historical data")

    # Historical E-REDES rows are authoritative. Refresh the current-day
    # provisional tail only after that write finishes so it cannot race the
    # history replacement/reconciliation path. This runs inside the already-managed
    # historical task rather than spawning another detached background task.
    await _async_import_provisional_data(hass, _entry, coordinator)


async def _async_options_updated(
    hass: HomeAssistant,
    entry: ERedesConfigEntry,
) -> None:
    """Reload the entry so changed synchronization options take effect."""
    await hass.config_entries.async_reload(entry.entry_id)
