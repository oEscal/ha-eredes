"""Near-real-time provisional Energy Dashboard tracking."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, cast

from homeassistant.components.energy.data import async_get_manager
from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfEnergy
from homeassistant.core import (
    Event,
    EventStateChangedData,
    State,
    callback,
    valid_entity_id,
)
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util.unit_conversion import EnergyConverter

from .historical import (
    LISBON,
    TOTAL_HISTORY_DAYS,
    _async_persist_statistics,
    _statistics_metadata,
    statistic_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.components.recorder.core import Recorder
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Multiple energy entities often report together. Coalesce that burst into one
# external-statistics write while remaining effectively immediate to the user.
LIVE_UPDATE_DEBOUNCE_SECONDS = 0.25


class ProvisionalEnergyTracker:
    """Track current-day device energy directly from live entity states."""

    def __init__(
        self,
        hass: HomeAssistant,
        cpe: str,
        statistics_import_lock: asyncio.Lock,
    ) -> None:
        """Initialize the live provisional tracker."""
        self._hass = hass
        self._cpe = cpe
        self._statistics_import_lock = statistics_import_lock
        self._state_lock = asyncio.Lock()
        self._reconcile_lock = asyncio.Lock()
        self._reconcile_requested = False
        self._device_entity_ids: set[str] = set()
        self._last_values_kwh: dict[str, float] = {}
        self._hourly_changes: defaultdict[datetime, float] = defaultdict(float)
        self._day: date | None = None
        self._base_sum: float | None = None
        self._remove_state_listener: Callable[[], None] | None = None
        self._cancel_pending_flush: Callable[[], None] | None = None
        self._closed = False

    @property
    def device_entity_ids(self) -> set[str]:
        """Return the top-level Energy Dashboard entities currently tracked."""
        return set(self._device_entity_ids)

    async def async_reconcile(self) -> bool:
        """Rebuild today's live tracker from raw state history and current states."""
        result = False
        while not self._closed:
            async with self._reconcile_lock:
                self._reconcile_requested = False
                result = await self._async_reconcile_once()
            if not self._reconcile_requested:
                return result
        return False

    async def _async_reconcile_once(self) -> bool:
        """Perform one complete current-day reconstruction."""
        manager = await async_get_manager(self._hass)
        preferences = manager.data
        stat_id = statistic_id(self._cpe)
        entity_ids = _top_level_device_entity_ids(preferences, stat_id)
        self._configure_state_listener(entity_ids)

        if not entity_ids:
            async with self._state_lock:
                self._day = datetime.now(tz=UTC).astimezone(LISBON).date()
                self._base_sum = None
                self._last_values_kwh.clear()
                self._hourly_changes.clear()
            _LOGGER.debug(
                "No live top-level Energy Dashboard device entities configured"
            )
            return False

        now_utc = datetime.now(tz=UTC)
        today_start_utc = _local_day_start_utc(now_utc)
        recorder = get_instance(self._hass)

        history = await recorder.async_add_executor_job(
            get_significant_states,
            self._hass,
            today_start_utc,
            now_utc,
            sorted(entity_ids),
            None,
            True,
            True,
            False,
            False,
            False,
        )
        base_sum = await _async_prior_cumulative_sum(
            recorder,
            self._hass,
            stat_id,
            today_start_utc,
        )

        # Read live states only after the Recorder history query finishes. A state
        # that changed while Recorder was being queried is therefore still folded
        # into the reconstruction even if it has not reached the database yet.
        current_states = {
            entity_id: self._hass.states.get(entity_id) for entity_id in entity_ids
        }
        hourly_changes, last_values = _reconstruct_device_energy(
            cast("dict[str, list[State]]", history),
            current_states,
            entity_ids,
            today_start_utc,
        )

        async with self._state_lock:
            self._day = now_utc.astimezone(LISBON).date()
            self._base_sum = base_sum
            self._hourly_changes = hourly_changes
            self._last_values_kwh = last_values

        if base_sum is None:
            _LOGGER.warning(
                "Skipping provisional current-day energy because no prior E-REDES "
                "cumulative statistic is available before %s",
                today_start_utc.isoformat(),
            )
            return False

        return await self.async_flush()

    async def async_flush(self) -> bool:
        """Write the current in-memory provisional snapshot to Recorder."""
        if self._closed:
            return False
        if self._statistics_import_lock.locked():
            self._schedule_flush(delay=1.0)
            return False

        async with self._state_lock:
            base_sum = self._base_sum
            hourly_changes = dict(self._hourly_changes)
            entity_count = len(self._device_entity_ids)

        if base_sum is None or not hourly_changes:
            return False

        cumulative_sum = base_sum
        statistics: list[StatisticData] = []
        for hour_start in sorted(hourly_changes):
            hour_kwh = hourly_changes[hour_start]
            cumulative_sum += hour_kwh
            statistics.append(
                StatisticData(
                    start=hour_start,
                    state=hour_kwh,
                    sum=cumulative_sum,
                )
            )

        stat_id = statistic_id(self._cpe)
        async with self._statistics_import_lock:
            persisted = await _async_persist_statistics(
                self._hass,
                stat_id,
                _statistics_metadata(self._cpe),
                statistics,
            )

        if persisted:
            _LOGGER.debug(
                "Updated live provisional current-day energy from %d top-level "
                "Energy Dashboard device entity(s), %.3f kWh",
                entity_count,
                sum(hourly_changes.values()),
            )
        return persisted

    async def _async_handle_state_change(
        self,
        event: Event[EventStateChangedData],
    ) -> None:
        """Apply one live cumulative-energy entity change."""
        if self._closed:
            return
        if self._reconcile_lock.locked():
            # The reconciliation reads hass.states after its Recorder query, so it
            # normally absorbs this change. Request one additional pass to close the
            # tiny race where this event lands after that live-state snapshot.
            self._reconcile_requested = True
            return

        entity_id = event.data["entity_id"]
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        event_day = new_state.last_changed.astimezone(LISBON).date()
        async with self._state_lock:
            if self._day != event_day or entity_id not in self._device_entity_ids:
                needs_reconcile = True
                changed = False
            else:
                needs_reconcile = False
                new_value = _state_value_kwh(new_state)
                previous_value = self._last_values_kwh.get(entity_id)
                if new_value is None:
                    return
                self._last_values_kwh[entity_id] = new_value
                if previous_value is None:
                    needs_reconcile = True
                    changed = False
                else:
                    delta = _energy_growth(previous_value, new_value)
                    changed = delta > 0
                    if changed:
                        hour_start = new_state.last_changed.astimezone(UTC).replace(
                            minute=0,
                            second=0,
                            microsecond=0,
                        )
                        self._hourly_changes[hour_start] += delta

        if needs_reconcile:
            await self.async_reconcile()
            return
        if changed:
            self._schedule_flush()

    @callback
    def _schedule_flush(self, *, delay: float = LIVE_UPDATE_DEBOUNCE_SECONDS) -> None:
        """Coalesce nearby entity changes into one near-immediate write."""
        if self._closed or self._cancel_pending_flush is not None:
            return
        self._cancel_pending_flush = async_call_later(
            self._hass,
            delay,
            self._async_flush_later,
        )

    async def _async_flush_later(self, _now: datetime) -> None:
        """Flush one debounced live update."""
        self._cancel_pending_flush = None
        await self.async_flush()

    @callback
    def _configure_state_listener(self, entity_ids: set[str]) -> None:
        """Listen only to the current top-level Energy Dashboard entities."""
        if entity_ids == self._device_entity_ids and self._remove_state_listener:
            return
        if self._remove_state_listener is not None:
            self._remove_state_listener()
            self._remove_state_listener = None

        self._device_entity_ids = set(entity_ids)
        if entity_ids:
            self._remove_state_listener = async_track_state_change_event(
                self._hass,
                entity_ids,
                self._async_handle_state_change,
            )

    @callback
    def shutdown(self) -> None:
        """Detach listeners and pending timers when the config entry unloads."""
        self._closed = True
        if self._remove_state_listener is not None:
            self._remove_state_listener()
            self._remove_state_listener = None
        if self._cancel_pending_flush is not None:
            self._cancel_pending_flush()
            self._cancel_pending_flush = None


def _top_level_device_entity_ids(preferences: object, stat_id: str) -> set[str]:
    """Return live entity ids for top-level Energy Dashboard device consumption."""
    if not isinstance(preferences, dict):
        return set()
    return {
        str(device["stat_consumption"]).lower()
        for device in preferences.get("device_consumption", [])
        if isinstance(device, dict)
        and not device.get("included_in_stat")
        and device.get("stat_consumption") != stat_id
        and isinstance(device.get("stat_consumption"), str)
        and valid_entity_id(str(device["stat_consumption"]))
    }


def _local_day_start_utc(now_utc: datetime) -> datetime:
    """Return the current Europe/Lisbon local midnight as UTC."""
    return (
        now_utc.astimezone(LISBON)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
    )


def _state_value_kwh(state: State | None) -> float | None:
    """Convert one live cumulative-energy state to kWh."""
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    unit = state.attributes.get("unit_of_measurement")
    if not isinstance(unit, str):
        return None
    try:
        value = float(state.state)
        return float(
            EnergyConverter.convert(value, unit, UnitOfEnergy.KILO_WATT_HOUR)
        )
    except (TypeError, ValueError):
        return None


def _energy_growth(previous_kwh: float, current_kwh: float) -> float:
    """Return total-increasing growth, treating a decrease as a meter reset."""
    if current_kwh < 0:
        return 0.0
    if current_kwh >= previous_kwh:
        return current_kwh - previous_kwh
    return current_kwh


def _reconstruct_device_energy(
    history: dict[str, list[State]],
    current_states: dict[str, State | None],
    entity_ids: set[str],
    today_start_utc: datetime,
) -> tuple[defaultdict[datetime, float], dict[str, float]]:
    """Reconstruct today's hourly growth from raw cumulative entity states."""
    hourly_changes: defaultdict[datetime, float] = defaultdict(float)
    last_values: dict[str, float] = {}

    for entity_id in entity_ids:
        states = list(history.get(entity_id, []))
        current_state = current_states.get(entity_id)
        if current_state is not None and (
            not states
            or current_state.last_changed > states[-1].last_changed
            or current_state.state != states[-1].state
        ):
            states.append(current_state)

        states.sort(key=lambda state: state.last_changed)
        previous_value: float | None = None
        for state in states:
            value = _state_value_kwh(state)
            if value is None:
                continue
            if previous_value is not None and state.last_changed >= today_start_utc:
                delta = _energy_growth(previous_value, value)
                if delta > 0:
                    hour_start = state.last_changed.astimezone(UTC).replace(
                        minute=0,
                        second=0,
                        microsecond=0,
                    )
                    hourly_changes[hour_start] += delta
            previous_value = value

        if previous_value is not None:
            last_values[entity_id] = previous_value

    return hourly_changes, last_values


async def _async_prior_cumulative_sum(
    recorder: Recorder,
    hass: HomeAssistant,
    stat_id: str,
    today_start_utc: datetime,
) -> float | None:
    """Return the latest E-REDES cumulative sum before today's local midnight."""
    seed_statistics = await recorder.async_add_executor_job(
        statistics_during_period,
        hass,
        today_start_utc - timedelta(days=TOTAL_HISTORY_DAYS + 1),
        today_start_utc - timedelta(microseconds=1),
        {stat_id},
        "day",
        None,
        {"sum"},
    )
    seed_rows = seed_statistics.get(stat_id, [])
    seed_sum = seed_rows[-1].get("sum") if seed_rows else None
    return float(seed_sum) if seed_sum is not None else None
