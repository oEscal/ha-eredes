# EDM historical load curves use calendar-month request windows

The historical importer requests `request_type=3` load curves one calendar month at
a time, following the E-REDES portal's own request shape. Adjacent requests do not
share a load-curve timestamp.

## Context

The original importer assumed the EDM endpoint accepted arbitrary ranges up to 31
days and generated windows such as `2025-08-10 00:00:00` to
`2025-09-10 00:00:00`. On 2026-08-10 that request returned HTTP 200 with
`Body.Success=false`, preventing the one-year backfill.

Inspection of the current Balcão Digital frontend showed that consumption history
uses `request_type=3` with one calendar month per request. For a complete month it
builds the range from `startOf("month").add(15, "m")` through
`endOf("month").add(1, "s")`, which formats effectively as `00:15` on the first day
through `00:00` on the first day of the next month.

That shape also establishes the load-curve timestamp semantics: timestamps identify
the **end** of each 15-minute energy interval. The first interval belonging to a day
ends at `00:15`; the interval ending at next-day `00:00` is the final quarter-hour of
the previous day. Requests are endpoint-inclusive, so reusing `00:00` as the next
request's start would duplicate that reading.

## Decision

- Split historical fetches at calendar-month boundaries rather than by a fixed day
  count.
- Treat the import plan's `start_date` as the beginning of the desired consumption
  period and request its first load-curve point at `start_date + 15 minutes`.
- End a complete month at next-month `00:00` and begin the next request at
  next-month `00:15`, preventing endpoint overlap.
- Before hourly aggregation, subtract 15 minutes from each reading timestamp and then
  truncate to the hour. Thus a reading timestamped `01:00` contributes to the
  `00:00-01:00` statistic.
- Bump the history-import version so previously imported hourly rows are rebuilt with
  the corrected interval assignment.

## Consequences

Historical requests now match the production portal's request semantics rather than
an undocumented 31-day assumption. Month boundaries are not double-counted, and
hourly/daily statistics attribute each quarter-hour to the interval in which its
energy was consumed. The existing atomic backfill behavior still applies: if any
month request fails, no partial run is committed and the backfill is retried later.
