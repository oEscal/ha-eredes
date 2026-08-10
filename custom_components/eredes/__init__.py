"""The E-REDES integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_change

from .const import CONF_ACCESS_TOKEN, HISTORY_SYNC_HOUR, LEGACY_TOKEN_KEYS
from .coordinator import ERedesCoordinator

if TYPE_CHECKING:
    import asyncio
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
    historical_import_task: asyncio.Task[None] | None = None


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
    )

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Synchronize history once at setup, then every day at 05:00 local HA time.
    _start_historical_import(hass, entry)
    entry.async_on_unload(
        async_track_time_change(
            hass,
            lambda now: _handle_daily_history_sync(hass, entry, now),
            hour=HISTORY_SYNC_HOUR,
            minute=0,
            second=0,
        )
    )

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


@callback
def _handle_daily_history_sync(
    hass: HomeAssistant,
    entry: ERedesConfigEntry,
    _now: datetime,
) -> None:
    """Start the scheduled daily history synchronization."""
    _LOGGER.debug("Starting scheduled 05:00 historical data synchronization")
    _start_historical_import(hass, entry)


async def _async_import_historical_data(
    hass: HomeAssistant,
    _entry: ERedesConfigEntry,
    coordinator: ERedesCoordinator,
) -> None:
    """Import historical data in the background."""
    from .historical import async_import_historical_data  # noqa: PLC0415

    _LOGGER.debug("Starting historical data import for CPE %s", coordinator.cpe[-8:])
    try:
        completed = await async_import_historical_data(hass, coordinator)
        if completed:
            _LOGGER.debug("Historical data import completed")
        else:
            _LOGGER.warning(
                "Historical data import incomplete; it will be retried at the next "
                "daily synchronization or integration setup"
            )
    except Exception:
        _LOGGER.exception("Failed to import historical data")
