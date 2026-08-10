# Historical consumption is imported as an external statistic

The one-year consumption backfill is written to Home Assistant long-term statistics
under an **external** statistic id, `eredes:energy_<cpe suffix>` (source `eredes`), via
`async_add_external_statistics` — not attached to the `sensor.…_daily_energy` entity.
The statistic declares Home Assistant's `energy` unit class in addition to `kWh` and
`has_sum=True`, so it is eligible for the Energy Dashboard's grid-consumption picker.

## Context

External statistics must use a `<source>:<object_id>` id; entity-style dotted ids
(`sensor.…`) are rejected by `valid_statistic_id`. An earlier version built the id in
the dotted form but still called `async_add_external_statistics`, so every import
fetched a full year and then failed with `Invalid statistic_id` — the backfill never
landed.

## Considered Options

- **Attach to the Daily Energy sensor** (`async_import_statistics`, source `recorder`,
  entity id) — rejected. That sensor is `state_class=TOTAL` with a daily `last_reset`,
  so the recorder already compiles its own statistics for it; importing an independent
  hourly cumulative series onto the same id would collide with the recorder's sums.
- **External statistic under a dedicated id** — chosen. It is independent of the live
  sensor, doesn't require the entity to exist, and is the idiomatic way to feed
  historical energy into the Energy Dashboard.

## Consequences

The Energy Dashboard consumption source is the `eredes:energy_…` statistic, not the
sensor (see README). History is synchronized once during integration setup and then
on a user-configurable schedule. The default is every day at 05:00 in Home
Assistant's local time; users can choose the local clock time and an interval of 1–30
days from the integration options. Normal incremental updates seed the cumulative
`sum` from the last imported hour and resume forward. Full-window imports
are separately versioned: when the history-import version changes (or no successful
marker exists), the integration
re-fetches the complete one-year window and rewrites matching hourly rows while
preserving the cumulative sum immediately before the window. A genuine failed API
chunk aborts the whole run, so partial backfills are never committed as successful.
E-REDES status `-1002` (`result is empty`) is different: it means that no consumption
data exists for the requested period (for example, before the contract began), so the
client returns an empty result and the historical importer continues with the next
window.

`async_add_external_statistics` only queues the recorder import; it does not mean the
rows have been committed. The importer therefore waits for the recorder queue to
commit and reads back the first and last generated hourly rows before marking a full
history version complete. A missing or mismatched boundary row leaves the version
marker unset so the repair is retried on a subsequent scheduled synchronization or
setup. The configured frequency counts successful synchronizations; a failed run does
not reset the interval counter.

The history-import version is independent from the integration version. It should be
incremented only when a code change requires already-stored historical statistics to
be regenerated; ordinary integration updates must leave it unchanged to avoid an
unnecessary one-year backfill for every user.
