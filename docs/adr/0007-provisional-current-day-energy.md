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
4. Track only `device_consumption` entries whose `stat_consumption` is a live Home
   Assistant entity id. Reconstruct today's consumption from raw Recorder state
   history plus the latest `hass.states` values, convert cumulative energy to kWh,
   treat a cumulative decrease as a `total_increasing` reset, and bucket each positive
   delta into the UTC hour in which the entity changed. Do not use Recorder's 5-minute
   statistics for the provisional path.
5. Seed the provisional day's cumulative `sum` from the latest persisted E-REDES
   statistic before local midnight. Because E-REDES can lag by more than 24 hours,
   find that seed from daily-reduced statistics across the supported history window,
   not only the immediately preceding day. Write the provisional hourly rows to the
   same E-REDES external statistic, allowing later runs to update those hour starts.
6. Subscribe to `state_changed` for the selected top-level device entities. Apply
   each live cumulative-state delta in memory and rewrite the provisional E-REDES
   statistic after a 250 ms debounce, coalescing devices that report together. Do not
   use a periodic provisional refresh interval. Rebuild the in-memory tracker from raw
   state history on startup, at local midnight, and after authoritative E-REDES
   history updates. Queue the initial reconciliation as ConfigEntry-managed background
   work after structural setup; do not await Recorder persistence from
   `async_setup_entry()`.
7. Serialize provisional and authoritative history writes with one integration-level
   lock. After every E-REDES historical synchronization, refresh the provisional
   current day inline in the already-managed historical task so a correction to
   yesterday's cumulative sum propagates into today's provisional cumulative rows.
   External-statistic imports are asynchronous. Verify a write by polling the
   imported boundary rows until the expected `state` and cumulative `sum` become
   visible. Do not await Recorder's commit barrier: during Home Assistant startup it
   can remain unresolved while Recorder is not ready, which would hold the statistics
   lock and suppress later provisional ticks. The polling window is strictly bounded.
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
11. The live tracker owns exactly one indexed state-change subscription and one
    optional debounce timer, both removed on config-entry unload. Home Assistant's
    interval scheduler owns fallback reconciliation coroutines, and the historical
    importer performs its follow-up reconciliation inline. Do not create a detached
    task for every device event or interval tick.
12. Remote E-REDES availability is not a prerequisite for loading the integration.
    `async_setup_entry()` performs structural setup only, then queues the initial
    coordinator `async_refresh()` as ConfigEntry-managed background work. Authentication
    failures still mark remote entities unavailable and initiate Home Assistant
    reauthentication, but they cannot block integration setup. Start the initial
    historical backfill only after that background remote refresh succeeds; the local
    provisional scheduler and initial local background refresh operate independently.

## Consequences

The Energy Dashboard can show a useful estimate for today instead of an empty grid
consumption series. The value may be lower than actual grid import because untracked
loads are absent. With solar or batteries it represents the sum of configured device
loads, not necessarily physical grid import.

The estimate is deliberately temporary. Once E-REDES data for that completed day is
fetched, the existing history/reconciliation path replaces the provisional hourly
rows with E-REDES measurements and, when available, real cumulative-meter totals.
