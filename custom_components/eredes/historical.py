"""Historical data import for E-REDES integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.recorder.core import Recorder
    from homeassistant.core import HomeAssistant

    from .coordinator import ERedesCoordinator
    from .eredes_api.models import ConsumptionReading

_LOGGER = logging.getLogger(__name__)

# E-REDES load-curve timestamps identify the end of each 15-minute interval.
READING_INTERVAL = timedelta(minutes=15)

# Total days of history to import
TOTAL_HISTORY_DAYS = 365  # 1 year

# Historical import state is versioned independently from the integration. A
# version bump forces one successful full-window rebuild before append/resume
# mode is allowed again. This repairs older partial imports without repeatedly
# downloading a year of data on every Home Assistant restart.
HISTORY_IMPORT_VERSION = 3
HISTORY_STORAGE_VERSION = 1
HISTORY_STORAGE_KEY_PREFIX = f"{DOMAIN}.historical_import"

# E-REDES request boundaries are Lisbon wall-clock values.
LISBON = ZoneInfo("Europe/Lisbon")

# When resuming, start the fetch a couple of days before the last stored hour.
# Aggregation then drops everything up to and including that hour, so already
# stored statistics are never re-counted.
REFETCH_BUFFER_DAYS = 2


@dataclass(slots=True)
class _HistoryImportPlan:
    """Resolved boundaries and cumulative state for one historical import."""

    start_date: datetime
    end_date: datetime
    initial_sum: float
    after: datetime | None
    needs_full_import: bool


def statistic_id(cpe: str) -> str:
    """Return the external long-term-statistics id for a CPE's energy history.

    External statistics must be ``<source>:<object_id>`` (colon-separated), not
    an entity id — see CONTEXT.md and docs/adr/0002.
    """
    return f"{DOMAIN}:energy_{cpe[-8:].lower()}"


async def _async_build_import_plan(
    hass: HomeAssistant,
    coordinator: ERedesCoordinator,
    stat_id: str,
) -> tuple[_HistoryImportPlan, Store[dict[str, int]]]:
    """Resolve whether this run must rebuild the year or can resume."""
    recorder = get_instance(hass)
    last_stats = await recorder.async_add_executor_job(
        get_last_statistics,
        hass,
        1,
        stat_id,
        True,
        {"sum"},
    )

    store: Store[dict[str, int]] = Store(
        hass,
        HISTORY_STORAGE_VERSION,
        f"{HISTORY_STORAGE_KEY_PREFIX}_{coordinator.cpe.lower()}",
    )
    import_state = await store.async_load()
    full_import_current = (
        isinstance(import_state, dict)
        and import_state.get("version") == HISTORY_IMPORT_VERSION
    )

    now_utc = datetime.now(tz=UTC)
    now_local = now_utc.astimezone(LISBON).replace(tzinfo=None)
    full_start_local = (now_local - timedelta(days=TOTAL_HISTORY_DAYS)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    full_start_utc = full_start_local.replace(tzinfo=LISBON).astimezone(UTC)
    needs_full_import = not full_import_current or not (
        last_stats and stat_id in last_stats and last_stats[stat_id]
    )

    if needs_full_import:
        baseline_stats = await recorder.async_add_executor_job(
            statistics_during_period,
            hass,
            full_start_utc - timedelta(hours=1),
            full_start_utc,
            {stat_id},
            "hour",
            None,
            {"sum"},
        )
        baseline_rows = baseline_stats.get(stat_id, [])
        initial_sum = 0.0
        if baseline_rows:
            initial_sum = baseline_rows[-1].get("sum") or 0.0
        _LOGGER.debug(
            "Importing full history window from %s (history import version %d)",
            full_start_local.isoformat(),
            HISTORY_IMPORT_VERSION,
        )
        return (
            _HistoryImportPlan(
                start_date=full_start_local,
                end_date=now_local,
                initial_sum=initial_sum,
                after=None,
                needs_full_import=True,
            ),
            store,
        )

    last_row = last_stats[stat_id][0]
    after = datetime.fromtimestamp(last_row["start"], tz=UTC)
    after_local = after.astimezone(LISBON).replace(tzinfo=None)
    start_date = max(
        full_start_local,
        after_local - timedelta(days=REFETCH_BUFFER_DAYS),
    )
    _LOGGER.debug("Resuming historical import after %s", after.isoformat())
    return (
        _HistoryImportPlan(
            start_date=start_date,
            end_date=now_local,
            initial_sum=last_row.get("sum") or 0.0,
            after=after,
            needs_full_import=False,
        ),
        store,
    )


def _month_start(value: datetime) -> datetime:
    """Return local midnight on the first day of ``value``'s month."""
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month_start(value: datetime) -> datetime:
    """Return midnight on the first day of the month after ``value``."""
    return (_month_start(value) + timedelta(days=32)).replace(day=1)


def _history_request_windows(
    start_date: datetime,
    end_date: datetime,
) -> list[tuple[datetime, datetime]]:
    """Build request windows accepted by the E-REDES EDM history endpoint.

    Completed months use the exact shape emitted by the portal: first interval
    end at 00:15 through next-month 00:00. The incomplete final month is split
    into daily windows, matching the request shape already used successfully by
    the live coordinator.
    """
    windows: list[tuple[datetime, datetime]] = []
    month_cursor = _month_start(start_date)

    while month_cursor < end_date:
        month_end = _next_month_start(month_cursor)
        if month_end <= end_date:
            windows.append((month_cursor + READING_INTERVAL, month_end))
            month_cursor = month_end
            continue

        day_cursor = max(
            month_cursor,
            start_date.replace(hour=0, minute=0, second=0, microsecond=0),
        )
        while day_cursor < end_date:
            day_end = min(day_cursor + timedelta(days=1), end_date)
            request_start = day_cursor + READING_INTERVAL
            if request_start <= day_end:
                windows.append((request_start, day_end))
            day_cursor += timedelta(days=1)
        break

    return windows


async def _async_fetch_history(
    coordinator: ERedesCoordinator,
    stat_id: str,
    start_date: datetime,
    end_date: datetime,
) -> list[ConsumptionReading] | None:
    """Fetch the requested history using portal-compatible request windows.

    Complete months are fetched in full even when the desired one-year cutoff
    lies inside the first month. Extra readings outside the desired interval are
    discarded locally after parsing.
    """
    all_readings: list[ConsumptionReading] = []

    for current_start, current_end in _history_request_windows(start_date, end_date):
        try:
            _LOGGER.debug(
                "Fetching %s to %s",
                current_start.isoformat(),
                current_end.isoformat(),
            )
            consumption = await coordinator.client.get_consumption(
                coordinator.cpe,
                current_start,
                current_end,
            )
        except Exception as ex:
            _LOGGER.error(
                "Failed to fetch history %s - %s: %s",
                current_start.isoformat(),
                current_end.isoformat(),
                ex,
            )
            _LOGGER.warning(
                "Historical import for %s was incomplete; no statistics were written "
                "and the missing window will be retried on the next integration setup",
                stat_id,
            )
            return None

        _LOGGER.debug("Got %d readings", len(consumption.readings))
        all_readings.extend(consumption.readings)

    # Readings are timestamped at interval end. Convert back to the Lisbon wall
    # clock used by request boundaries and retain only intervals inside the
    # originally requested range, excluding any extra days fetched from the
    # first calendar month.
    return [
        reading
        for reading in all_readings
        if start_date
        < reading.timestamp.astimezone(LISBON).replace(tzinfo=None)
        <= end_date
    ]


async def _async_verify_statistics_import(
    hass: HomeAssistant,
    recorder: Recorder,
    stat_id: str,
    statistics: list[StatisticData],
) -> bool:
    """Wait for the recorder commit and verify the imported boundary rows."""
    await recorder.async_block_till_done()

    for expected in (statistics[0], statistics[-1]):
        start = expected["start"]
        persisted = await recorder.async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            start + timedelta(hours=1),
            {stat_id},
            "hour",
            None,
            {"state", "sum"},
        )
        rows = persisted.get(stat_id, [])
        if not rows:
            _LOGGER.warning(
                "Recorder did not persist expected statistics for %s at %s",
                stat_id,
                start.isoformat(),
            )
            return False

        actual = rows[0]
        if (
            actual.get("state") != expected.get("state")
            or actual.get("sum") != expected.get("sum")
        ):
            _LOGGER.warning(
                "Recorder statistics verification failed for %s at %s: expected "
                "state=%s sum=%s, got state=%s sum=%s",
                stat_id,
                start.isoformat(),
                expected.get("state"),
                expected.get("sum"),
                actual.get("state"),
                actual.get("sum"),
            )
            return False

    return True


async def _async_persist_statistics(
    hass: HomeAssistant,
    stat_id: str,
    metadata: StatisticMetaData,
    statistics: list[StatisticData],
) -> bool:
    """Queue, commit, and verify one statistics import."""
    recorder = get_instance(hass)
    try:
        async_add_external_statistics(hass, metadata, statistics)
        _LOGGER.debug(
            "Queued %d hourly stats (%.3f kWh) for %s; waiting for recorder commit",
            len(statistics),
            statistics[-1]["sum"],
            stat_id,
        )
        if not await _async_verify_statistics_import(
            hass,
            recorder,
            stat_id,
            statistics,
        ):
            return False
    except Exception:
        _LOGGER.exception("Failed to persist external statistics for %s", stat_id)
        return False

    _LOGGER.debug(
        "Recorder committed and verified %d hourly stats for %s",
        len(statistics),
        stat_id,
    )
    return True


async def async_import_historical_data(
    hass: HomeAssistant,
    coordinator: ERedesCoordinator,
) -> bool:
    """Import historical energy data from E-REDES.

    A versioned full-window import is required before normal resume mode is
    allowed. That makes upgrades self-heal older partial imports: if any chunk
    of the year fails, nothing from that run is written and the full backfill is
    retried later. Once a complete window has succeeded, later runs only append
    after the latest stored statistic.
    """
    stat_id = statistic_id(coordinator.cpe)
    _LOGGER.debug("Historical import starting for %s", stat_id)

    plan, store = await _async_build_import_plan(hass, coordinator, stat_id)
    all_readings = await _async_fetch_history(
        coordinator,
        stat_id,
        plan.start_date,
        plan.end_date,
    )
    if all_readings is None:
        return False

    if not all_readings:
        if plan.needs_full_import:
            _LOGGER.warning(
                "Full historical import for %s returned no readings; it was not "
                "marked complete",
                stat_id,
            )
            return False
        _LOGGER.debug("No new historical data found to import")
        return True

    all_readings.sort(key=lambda r: r.timestamp)

    # On a full rebuild `plan.after` is None and all rows in the year are
    # regenerated. Home Assistant updates existing external-statistics rows at
    # matching timestamps, repairing partial imports and cumulative sums.
    statistics = _aggregate_to_hourly_statistics(
        all_readings,
        plan.initial_sum,
        plan.after,
    )

    if not statistics:
        _LOGGER.debug("No statistics generated from %d readings", len(all_readings))
        return not plan.needs_full_import

    metadata = StatisticMetaData(
        has_mean=False,
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=f"E-REDES Energy ({coordinator.cpe[-8:]})",
        source=DOMAIN,
        statistic_id=stat_id,
        unit_class=EnergyConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )

    if not await _async_persist_statistics(hass, stat_id, metadata, statistics):
        return False

    if plan.needs_full_import:
        await store.async_save({"version": HISTORY_IMPORT_VERSION})
        _LOGGER.debug(
            "Completed full history import version %d for %s",
            HISTORY_IMPORT_VERSION,
            stat_id,
        )

    return True


def _aggregate_to_hourly_statistics(
    readings: list[ConsumptionReading],
    initial_sum: float = 0.0,
    after: datetime | None = None,
) -> list[StatisticData]:
    """Aggregate 15-minute readings to hourly statistics.

    Args:
        readings: 15-minute interval readings (timestamps are aware UTC).
        initial_sum: cumulative sum of previously imported hours; the returned
            stats continue from here so the series stays monotonic across runs.
        after: if set, hours at or before this instant are skipped (they were
            already imported). Must be timezone-aware UTC.
    """
    if not readings:
        _LOGGER.debug("No readings to aggregate")
        return []

    statistics: list[StatisticData] = []
    cumulative_sum = initial_sum

    # Group readings by hour
    hourly_data: dict[datetime, float] = {}

    for reading in readings:
        # Timestamps arrive as aware UTC (converted from Lisbon local time at
        # parse time), so this only truncates — stamping tzinfo here would
        # reinterpret a local wall clock as UTC and shift every summer reading
        # an hour late.
        # The API timestamp is the end of the quarter-hour interval. Shift to
        # the interval start before truncating so 01:00 is counted in 00:00-01:00.
        interval_start = reading.timestamp - READING_INTERVAL
        hour_start = interval_start.replace(minute=0, second=0, microsecond=0)

        if after is not None and hour_start <= after:
            continue

        if hour_start not in hourly_data:
            hourly_data[hour_start] = 0.0

        hourly_data[hour_start] += reading.value_kwh

    _LOGGER.debug(
        "Aggregated %d readings into %d hourly buckets",
        len(readings),
        len(hourly_data),
    )

    # Convert to sorted list of statistics
    for hour_start in sorted(hourly_data.keys()):
        hour_kwh = hourly_data[hour_start]
        cumulative_sum += hour_kwh

        statistics.append(
            StatisticData(
                start=hour_start,
                state=hour_kwh,
                sum=cumulative_sum,
            )
        )

    _LOGGER.debug(
        "Created %d statistics, final cumulative sum: %.3f kWh",
        len(statistics),
        cumulative_sum,
    )

    return statistics
