# Load-curve timestamps are Lisbon local time, despite the `Z` suffix

`loadCurveTimestamp` values look like `2026-08-06T14:30:00Z`, but the `Z` is wrong:
they are **Europe/Lisbon wall-clock** times. The client parses them naively and
converts the series to UTC against `Europe/Lisbon`, disambiguating the repeated
autumn hour by order of arrival. Do not "simplify" this to `tzinfo=UTC`.

## Context

An earlier version stamped `tzinfo=UTC` straight onto the parsed wall clock. That is
correct for exactly half the year — Lisbon is WET (UTC+0) in winter — and silently
wrong for the other half, placing every reading between late March and late October
**one hour late** in the Energy Dashboard.

Measured against a live token on 2026-08-09, counting 15-minute points per day:

| Day | Points | Shape |
|---|---|---|
| 2026-03-28 | 96 | normal |
| **2026-03-29** (spring forward) | **92** | `00:45` jumps straight to `02:00` |
| 2026-03-30 | 96 | normal |
| 2025-10-25 | 96 | normal |
| **2025-10-26** (fall back) | **100** | `01:00/01:15/01:30/01:45` each appear **twice** |
| 2025-10-27 | 96 | normal |

True UTC is a flat 96 points every day of the year. A gap of exactly one hour at the
spring transition and a duplicate of exactly one hour at the autumn transition is only
possible if the clock is local. This also matches the transitions themselves: Lisbon
switches at 01:00 UTC, and the gap and duplicate both sit at 01:00–01:59 local.

The fork at `rjbmanvr/ha-eredes` diagnosed this correctly (`1d7d062`, parsing as
`Europe/Lisbon`) and then reverted it 14 minutes later (`5e9ad98`, back to UTC),
landing on the same wrong behaviour as upstream. The evidence above settles it.

### The autumn hour needs `fold`

`replace(tzinfo=ZoneInfo("Europe/Lisbon"))` defaults to `fold=0`, so **both** copies
of the repeated hour resolve to the same instant. They then collapse into one hourly
bucket, double-counting one hour of consumption and leaving the adjacent hour empty.
Resolving them requires marking the second occurrence `fold=1`, which needs the series
in order — so the conversion happens over the whole load-curve group, not per
timestamp.

## Considered Options

- **Stamp `tzinfo=UTC` on the parsed wall clock** — rejected; the bug above.
- **Convert per timestamp as it is parsed** — rejected. Correct for the offset, but
  cannot see that a wall-clock value is a repeat, so it still collapses the autumn
  hour.
- **Convert the ordered series, disambiguating repeats by arrival order** — chosen.
  The only extra assumption is that the API returns each register's load curve
  chronologically, which is the only ordering under which duplicate timestamps carry
  information at all.

## Consequences

`ConsumptionReading.timestamp` is **timezone-aware UTC** from the client outwards.
The timestamp marks the end of its 15-minute interval, so historical aggregation
subtracts 15 minutes and then truncates to the hour (see
[0005](0005-edm-history-uses-calendar-month-windows.md)). Re-stamping `tzinfo` in the
aggregator would reintroduce the timezone bug.

Statistics imported before this fix are wrong for every summer hour. Historical
imports are now versioned (see [0002](0002-external-statistics-for-history.md)), so an
upgrade can force one complete one-year rebuild and correct matching rows without the
user manually deleting the statistic. Normal later runs still resume from the newest
stored hour.

Historical request boundaries are explicitly converted from UTC to naive
`Europe/Lisbon` wall-clock values before calling `get_consumption`, matching the clock
used by the E-REDES API regardless of the Home Assistant host timezone.
