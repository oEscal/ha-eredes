# ADR 0007: Provisional current-day energy from Home Assistant devices

## Status

Accepted.

## Context

E-REDES does not expose the current local day's `A+` load curve through the portal API.
The `eredes:energy_<cpe suffix>` external statistic therefore has no current-day rows
until E-REDES publishes that day later. Home Assistant's Energy Dashboard can already
contain individual electrical-device consumption statistics that update during the
current day.

Those device statistics cannot reconstruct the physical grid meter exactly. Devices
that are not individually measured are absent, and solar/battery systems can make
household load differ from grid import. They are nevertheless useful as a temporary
lower-bound estimate while the E-REDES day is unavailable.

Home Assistant's Energy preferences expose individual electrical consumers under
`device_consumption`. Each item has a `stat_consumption` statistic id and may have an
`included_in_stat` parent. Recorder's `statistics_during_period` exposes hourly
`change` values for those cumulative energy statistics.

## Decision

1. Keep `eredes:energy_<cpe suffix>` as the single statistic used by the Energy
   Dashboard. Do not create a second provisional statistic that users would need to
   switch between.
2. For the current Europe/Lisbon calendar day only, read the Energy Dashboard's
   configured `device_consumption` entries through `EnergyManager`.
3. Sum only top-level device entries: a device with `included_in_stat` is excluded
   because its consumption is already represented by its parent. Also exclude the
   E-REDES statistic itself to prevent recursive feedback if it is accidentally added
   as an individual device.
4. Query Recorder for finalized hourly `change` values in kWh for earlier hours and
   5-minute short-term `change` values for the still-open current hour, then combine
   the selected device statistics into hourly buckets.
5. Seed the provisional day's cumulative `sum` from the latest persisted E-REDES
   statistic before local midnight. Because E-REDES can lag by more than 24 hours,
   find that seed from daily-reduced statistics across the supported history window,
   not only the immediately preceding day. Write the provisional hourly rows to the
   same E-REDES external statistic, allowing later runs to update those hour starts.
6. Refresh the provisional current day every 15 minutes using only local Home
   Assistant data. This refresh does not make additional E-REDES API requests.
7. Serialize provisional and authoritative history writes with one integration-level
   lock. After every E-REDES historical synchronization, refresh the provisional
   current day again so a correction to yesterday's cumulative sum propagates into
   today's provisional cumulative rows.
8. Never synthesize provisional rows for completed historical days. At midnight the
   fallback moves to the new current day. The completed day's provisional rows remain
   only until the normal E-REDES historical synchronization receives and overwrites
   them with E-REDES data.
9. Provisional Home Assistant-derived rows do not participate in real-meter
   reconciliation and do not qualify a day for **Last Real Data Day** or **Last
   Matching 15-Min Data Day**. Those sensors continue to depend exclusively on
   E-REDES data.
10. If no usable top-level device statistics exist, leave the current day absent
    rather than inventing zero consumption. Likewise, if no prior E-REDES cumulative
    statistic can be found, do not write a zero-based provisional series: this would
    create a large negative discontinuity relative to any older cumulative history.

## Consequences

The Energy Dashboard can show a useful estimate for today instead of an empty grid
consumption series. The value may be lower than actual grid import because untracked
loads are absent. With solar or batteries it represents the sum of configured device
loads, not necessarily physical grid import.

The estimate is deliberately temporary. Once E-REDES data for that completed day is
fetched, the existing history/reconciliation path replaces the provisional hourly
rows with E-REDES measurements and, when available, real cumulative-meter totals.
