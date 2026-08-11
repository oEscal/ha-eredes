"""Historical data import for E-REDES integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
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
    from .eredes_api.models import ConsumptionReading, MeterIndex

_LOGGER = logging.getLogger(__name__)

# E-REDES load-curve timestamps identify the end of each 15-minute interval.
READING_INTERVAL = timedelta(minutes=15)

# Total days of history to import
TOTAL_HISTORY_DAYS = 365  # 1 year

# Historical import state is versioned independently from the integration. A
# version bump forces one successful full-window rebuild before append/resume
# mode is allowed again. This repairs older partial imports without repeatedly
# downloading a year of data on every Home Assistant restart.
HISTORY_IMPORT_VERSION = 6
HISTORY_STORAGE_VERSION = 1
HISTORY_STORAGE_KEY_PREFIX = f"{DOMAIN}.historical_import"
HISTORY_PENDING_DAYS_KEY = "pending_reconciliation_days"
HISTORY_LAST_REAL_DATA_DAY_KEY = "last_real_data_day"

# E-REDES request boundaries are Lisbon wall-clock values.
LISBON = ZoneInfo("Europe/Lisbon")

# Rebuild a recent rolling tail on every synchronization. Real cumulative meter
# indexes commonly trail the 15-minute load curve by several days, so an
# append-only importer would permanently retain an estimated day before its real
# endpoint becomes available. Seven days covers the observed delay while keeping
# normal synchronization bounded.
REFETCH_BUFFER_DAYS = 7


@dataclass(slots=True)
class _HistoryImportPlan:
    """Resolved boundaries and cumulative state for one historical import."""

    start_date: datetime
    end_date: datetime
    initial_sum: float
    after: datetime | None
    needs_full_import: bool
    pending_days: set[date]
    last_real_data_day: date | None


@dataclass(frozen=True, slots=True)
class _DailyRealTotal:
    """Real daily delta and its integer-register quantization tolerance."""

    value_kwh: float
    tolerance_kwh: float


@dataclass(slots=True)
class _ReconciliationResult:
    """Reconciled readings and days that still require a later raw refetch."""

    readings: list[ConsumptionReading]
    pending_days: set[date]


def _pending_days_from_state(state: Any) -> set[date]:
    """Parse persisted reconciliation days, ignoring malformed state entries."""
    if not isinstance(state, dict):
        return set()
    raw_days = state.get(HISTORY_PENDING_DAYS_KEY, [])
    if not isinstance(raw_days, list):
        return set()

    pending_days: set[date] = set()
    for raw_day in raw_days:
        if not isinstance(raw_day, str):
            continue
        try:
            pending_days.add(date.fromisoformat(raw_day))
        except ValueError:
            _LOGGER.warning("Ignoring invalid pending reconciliation date %r", raw_day)
    return pending_days


def _last_real_data_day_from_state(state: Any) -> date | None:
    """Parse the persisted latest reliable calendar day."""
    if not isinstance(state, dict):
        return None
    raw_day = state.get(HISTORY_LAST_REAL_DATA_DAY_KEY)
    if not isinstance(raw_day, str):
        return None
    try:
        return date.fromisoformat(raw_day)
    except ValueError:
        _LOGGER.warning("Ignoring invalid last real data date %r", raw_day)
        return None


def _history_state(
    pending_days: set[date],
    last_real_data_day: date | None,
) -> dict[str, Any]:
    """Return persisted state for a successful historical synchronization."""
    return {
        "version": HISTORY_IMPORT_VERSION,
        HISTORY_PENDING_DAYS_KEY: sorted(day.isoformat() for day in pending_days),
        HISTORY_LAST_REAL_DATA_DAY_KEY: (
            last_real_data_day.isoformat() if last_real_data_day else None
        ),
    }


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
) -> tuple[_HistoryImportPlan, Store[dict[str, Any]]]:
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

    store: Store[dict[str, Any]] = Store(
        hass,
        HISTORY_STORAGE_VERSION,
        f"{HISTORY_STORAGE_KEY_PREFIX}_{coordinator.cpe.lower()}",
    )
    import_state = await store.async_load()
    pending_days = _pending_days_from_state(import_state)
    last_real_data_day = _last_real_data_day_from_state(import_state)
    if last_real_data_day is not None:
        coordinator.set_last_real_data_day(last_real_data_day)
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
    needs_full_import = not full_import_current or not (
        last_stats and stat_id in last_stats and last_stats[stat_id]
    )

    if needs_full_import:
        _LOGGER.debug(
            "Importing full history window from %s (history import version %d)",
            full_start_local.isoformat(),
            HISTORY_IMPORT_VERSION,
        )
        return (
            _HistoryImportPlan(
                start_date=full_start_local,
                end_date=now_local,
                initial_sum=0.0,
                after=None,
                needs_full_import=True,
                pending_days=pending_days,
                last_real_data_day=last_real_data_day,
            ),
            store,
        )

    last_row = last_stats[stat_id][0]
    last_start = datetime.fromtimestamp(last_row["start"], tz=UTC)
    last_start_local = last_start.astimezone(LISBON).replace(tzinfo=None)
    rolling_start = (last_start_local - timedelta(days=REFETCH_BUFFER_DAYS)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    if pending_days:
        pending_start = datetime.combine(min(pending_days), datetime.min.time())
        rolling_start = min(rolling_start, pending_start)
    start_date = max(full_start_local, rolling_start)

    # The rolling tail is rewritten, not merely appended. Seed its cumulative
    # sum from the last persisted hour before the rebuilt range so any daily
    # correction propagates through all later sums without introducing a jump.
    start_utc = start_date.replace(tzinfo=LISBON).astimezone(UTC)
    seed_stats = await recorder.async_add_executor_job(
        statistics_during_period,
        hass,
        start_utc - timedelta(days=1),
        start_utc,
        {stat_id},
        "hour",
        None,
        {"sum"},
    )
    seed_rows = seed_stats.get(stat_id, [])
    initial_sum = seed_rows[-1].get("sum") or 0.0 if seed_rows else 0.0

    _LOGGER.debug(
        "Rebuilding historical tail from %s after latest stored hour %s",
        start_date.isoformat(),
        last_start.isoformat(),
    )
    return (
        _HistoryImportPlan(
            start_date=start_date,
            end_date=now_local,
            initial_sum=initial_sum,
            after=None,
            needs_full_import=False,
            pending_days=pending_days,
            last_real_data_day=last_real_data_day,
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


def _meter_index_request_windows(
    start_date: datetime,
    end_date: datetime,
) -> list[tuple[datetime, datetime]]:
    """Build month-sized request-type-1 windows like the readings portal."""
    windows: list[tuple[datetime, datetime]] = []
    cursor = start_date.replace(hour=0, minute=0, second=0, microsecond=0)

    while cursor < end_date:
        next_month = _next_month_start(cursor)
        current_end = min(next_month - timedelta(seconds=1), end_date)
        windows.append((cursor, current_end))
        if next_month >= end_date:
            break
        cursor = next_month

    return windows


async def _async_fetch_meter_indexes(
    coordinator: ERedesCoordinator,
    stat_id: str,
    start_date: datetime,
    end_date: datetime,
) -> list[MeterIndex] | None:
    """Fetch valid real cumulative meter indexes for daily reconciliation."""
    indexes: list[MeterIndex] = []
    for current_start, current_end in _meter_index_request_windows(
        start_date, end_date
    ):
        try:
            _LOGGER.debug(
                "Fetching real meter indexes %s to %s",
                current_start.isoformat(),
                current_end.isoformat(),
            )
            indexes.extend(
                await coordinator.client.get_meter_indexes(
                    coordinator.cpe,
                    current_start,
                    current_end,
                )
            )
        except Exception as ex:
            _LOGGER.error(
                "Failed to fetch real meter indexes %s - %s: %s",
                current_start.isoformat(),
                current_end.isoformat(),
                ex,
            )
            _LOGGER.warning(
                "Historical import for %s was incomplete; no statistics were written "
                "because the real meter-index reconciliation could not be verified",
                stat_id,
            )
            return None

    indexes.sort(key=lambda index: (index.timestamp, index.meter_serial))
    return indexes


def _daily_real_totals(indexes: list[MeterIndex]) -> dict[date, _DailyRealTotal]:
    """Derive real daily kWh and quantization tolerance from midnight indexes."""
    by_meter: dict[str, dict[date, MeterIndex]] = {}
    for index in indexes:
        local_timestamp = index.timestamp.astimezone(LISBON)
        if (
            local_timestamp.hour != 0
            or local_timestamp.minute != 0
            or local_timestamp.second != 0
            or local_timestamp.microsecond != 0
        ):
            continue
        by_meter.setdefault(index.meter_serial, {})[local_timestamp.date()] = index

    candidates: dict[date, list[_DailyRealTotal]] = {}
    for endpoints in by_meter.values():
        for day, start_index in endpoints.items():
            next_index = endpoints.get(day + timedelta(days=1))
            if next_index is None:
                continue
            consumption_kwh = next_index.value_kwh - start_index.value_kwh
            if consumption_kwh < 0:
                continue
            register_count = max(
                start_index.register_count,
                next_index.register_count,
                1,
            )
            candidates.setdefault(day, []).append(
                _DailyRealTotal(
                    value_kwh=consumption_kwh,
                    tolerance_kwh=float(register_count),
                )
            )

    # Multiple meters supplying a delta for the same day is ambiguous (for
    # example around a physical meter replacement), so do not guess by adding or
    # selecting one of them.
    return {
        day: values[0]
        for day, values in candidates.items()
        if len(values) == 1
    }


def _is_complete_load_curve_day(
    readings: list[ConsumptionReading],
    positions: list[int],
    day: date,
) -> bool:
    """Return whether positions contain every 15-minute interval of a local day."""
    expected_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=LISBON)
    expected_end = datetime.combine(
        day + timedelta(days=1), datetime.min.time()
    ).replace(tzinfo=LISBON)
    expected_first_end_utc = expected_start.astimezone(UTC) + READING_INTERVAL
    expected_end_utc = expected_end.astimezone(UTC)
    expected_count = int(
        (expected_end_utc - expected_start.astimezone(UTC)) / READING_INTERVAL
    )
    if len(positions) != expected_count:
        _LOGGER.debug(
            "Not reconciling incomplete load-curve day %s: got %d of %d intervals",
            day.isoformat(),
            len(positions),
            expected_count,
        )
        return False

    actual_timestamps = [readings[position].timestamp for position in positions]
    for index, timestamp in enumerate(actual_timestamps):
        if timestamp != expected_first_end_utc + READING_INTERVAL * index:
            _LOGGER.debug(
                "Not reconciling discontinuous load-curve day %s at %s",
                day.isoformat(),
                timestamp.isoformat(),
            )
            return False
    return actual_timestamps[-1] == expected_end_utc


def _reconcile_with_meter_indexes(
    readings: list[ConsumptionReading],
    indexes: list[MeterIndex],
    *,
    pending_days: set[date] | None = None,
) -> _ReconciliationResult:
    """Reconcile implausible complete days and track them until raw data recovers.

    Real cumulative indexes are integer-valued per tariff register, so their
    daily delta has a quantization envelope of roughly ±1 kWh per register. Raw
    15-minute data inside that envelope is accepted as the more precise source.
    A day outside the envelope is scaled to the real delta and kept pending until
    a later complete refetch falls back inside the envelope.
    """
    remaining_pending = set(pending_days or ())
    daily_totals = _daily_real_totals(indexes)
    if not daily_totals or not readings:
        return _ReconciliationResult(readings, remaining_pending)

    groups: dict[date, list[int]] = {}
    for position, reading in enumerate(readings):
        interval_start = (reading.timestamp - READING_INTERVAL).astimezone(LISBON)
        groups.setdefault(interval_start.date(), []).append(position)

    reconciled = list(readings)
    corrected_days = 0
    resolved_days = 0
    for day, real_total in daily_totals.items():
        positions = groups.get(day)
        if not positions or not _is_complete_load_curve_day(readings, positions, day):
            remaining_pending.add(day)
            continue

        curve_kwh = sum(readings[position].value_kwh for position in positions)
        deviation_kwh = abs(curve_kwh - real_total.value_kwh)
        if deviation_kwh <= real_total.tolerance_kwh:
            if day in remaining_pending:
                remaining_pending.remove(day)
                resolved_days += 1
                _LOGGER.info(
                    "Raw load curve for %s now agrees with real meter indexes "
                    "within ±%.0f kWh; reconciliation no longer pending",
                    day.isoformat(),
                    real_total.tolerance_kwh,
                )
            continue

        remaining_pending.add(day)
        if curve_kwh == 0:
            _LOGGER.warning(
                "Cannot distribute %.3f kWh real total for %s because its "
                "15-minute load curve sums to zero",
                real_total.value_kwh,
                day.isoformat(),
            )
            continue

        scale = real_total.value_kwh / curve_kwh
        for position in positions:
            reconciled[position] = replace(
                readings[position],
                value_wh=readings[position].value_wh * scale,
            )
        corrected_days += 1
        _LOGGER.info(
            "Reconciled %s load curve from %.3f kWh to %.3f kWh; raw deviation "
            "%.3f kWh exceeds ±%.0f kWh quantization tolerance",
            day.isoformat(),
            curve_kwh,
            real_total.value_kwh,
            deviation_kwh,
            real_total.tolerance_kwh,
        )

    if corrected_days:
        _LOGGER.info(
            "Reconciled %d historical day(s) to real meter indexes", corrected_days
        )
    if resolved_days:
        _LOGGER.info("Resolved %d pending reconciliation day(s)", resolved_days)
    return _ReconciliationResult(reconciled, remaining_pending)


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
    *,
    replace_existing: bool = False,
) -> bool:
    """Queue, commit, and verify one statistics import."""
    recorder = get_instance(hass)
    try:
        if replace_existing:
            recorder.async_clear_statistics([stat_id])
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

    A versioned full-window import is required before rolling-tail mode is
    allowed. That makes upgrades self-heal older partial imports: if any chunk
    of the year fails, nothing from that run is written and the full backfill is
    retried later. Once complete, later runs rewrite a recent tail so delayed
    real meter indexes can correct days that were initially estimated.
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
    if not all_readings:
        if all_readings is None or plan.needs_full_import:
            if all_readings is not None:
                _LOGGER.warning(
                    "Full historical import for %s returned no readings; it was not "
                    "marked complete",
                    stat_id,
                )
            return False
        _LOGGER.debug("No new historical data found to import")
        return True

    meter_indexes = await _async_fetch_meter_indexes(
        coordinator,
        stat_id,
        plan.start_date,
        plan.end_date,
    )
    if meter_indexes is None:
        return False

    real_daily_totals = _daily_real_totals(meter_indexes)
    last_real_data_day = plan.last_real_data_day
    if real_daily_totals:
        newest_real_day = max(real_daily_totals)
        if last_real_data_day is None or newest_real_day > last_real_data_day:
            last_real_data_day = newest_real_day
        coordinator.set_last_real_data_day(last_real_data_day)

    all_readings.sort(key=lambda r: r.timestamp)
    reconciliation = _reconcile_with_meter_indexes(
        all_readings,
        meter_indexes,
        pending_days=plan.pending_days,
    )
    all_readings = reconciliation.readings

    # On a full rebuild `plan.after` is None and all rows in the year are
    # regenerated. Persistence replaces the old external statistic entirely so
    # obsolete rows from older timestamp semantics cannot survive the repair.
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

    if not await _async_persist_statistics(
        hass,
        stat_id,
        metadata,
        statistics,
        replace_existing=plan.needs_full_import,
    ):
        return False

    await store.async_save(
        _history_state(reconciliation.pending_days, last_real_data_day)
    )
    if plan.needs_full_import:
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
