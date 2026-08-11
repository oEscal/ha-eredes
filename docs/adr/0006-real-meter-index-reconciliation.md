# ADR 0006: Reconcile load curves with real cumulative meter indexes

## Status

Accepted.

## Context

The E-REDES portal exposes two distinct metering products through the EDM data-usage
endpoint:

- `request_type=3`: 15-minute active-energy load curves. These provide the intraday
  shape used by Home Assistant hourly statistics, but recent values can be marked
  estimated and can materially disagree with the meter's real daily advance.
- `request_type=1`, `formatted=true`: cumulative meter readings shown by the portal's
  **Leituras > Consultar histórico** view. Valid records with `mrType=1` or `2` are
  labelled Real by the portal; `mrType=1` is sourced from the distribution network
  operator. For a meter with consecutive midnight readings, their difference is the
  real consumption of the intervening local calendar day.

On 2026-08-11 the portal showed, for example, 2026-08-01 00:00 cumulative registers
`V=4062`, `P=1910`, `C=4637` and 2026-08-02 00:00 registers `V=4064`, `P=1912`,
`C=4645`. The real daily advance is therefore 12 kWh, while the imported 15-minute
curve produced about 24 kWh in Home Assistant. Similar disagreement affected the
other recent August days.

Real meter indexes can arrive several days after the corresponding 15-minute curve.
An append-only statistics importer therefore cannot correct an estimated day once
that day's real endpoint becomes available.

## Decision

1. Fetch `request_type=1` formatted meter-index history alongside the 15-minute load
   curve used for historical statistics.
2. Accept only valid (`activa` or `corrigida`) real (`mrType=1` or `2`) active-import
   readings. Prefer operator (`mrType=1`) readings when duplicate real readings exist.
3. Keep each reading associated with its physical meter serial. Never form a daily
   delta across different serial numbers.
4. Derive a real daily total only from two consecutive local-midnight indexes for the
   same physical meter. Gaps are not interpolated.
5. Treat the cumulative register delta as quantized rather than exact to sub-kWh
   precision. Each integer-valued tariff register contributes up to roughly ±1 kWh of
   daily-delta uncertainty because two integer endpoints are subtracted. The accepted
   envelope is therefore ±1 kWh for simple, ±2 kWh for bi-hourly, ±3 kWh for
   tri-hourly (`V + P + C`), and ±4 kWh for four-period meters.
6. When a complete 15-minute day falls outside that envelope, scale its 15-minute
   values proportionally so their sum equals the real cumulative-index delta. Mark the
   local calendar day as pending reconciliation in persistent integration storage.
7. A pending day stays pending across restarts and synchronizations. It is cleared only
   after a later fetch contains a complete, continuous 15-minute day whose raw total
   falls inside the applicable quantization envelope. The newly credible raw curve is
   then used without scaling.
8. A day with a real cumulative delta but an incomplete/discontinuous 15-minute curve
   is also marked pending; it is never considered repaired merely because its first and
   last timestamps exist.
9. When no real daily pair exists yet, retain the 15-minute load curve unchanged and do
   not invent a daily total.
10. Rebuild a seven-day rolling statistics tail on normal synchronizations. If a
    pending day is older than seven days, extend the rebuild back to the oldest pending
    day. Seed cumulative `sum` from the persisted hour immediately before that range so
    any later replacement of scaled data with credible raw data propagates through all
    subsequent sums.
11. Bump the historical import version so existing installations perform one full
    rebuild under the tolerance and pending-day rules.
12. Persist the latest local calendar day for which a valid consecutive-midnight real
    delta exists and expose it as a Home Assistant `date` sensor named **Last Real
    Data Day**. The sensor reports the consumption day, not the endpoint date: a real
    index at August 8 00:00 paired with August 7 00:00 makes August 7 the sensor value.

## Register layouts

The formatted active-import cumulative index is calculated according to the register
layout exposed by the portal:

- simple: `S`
- bi-hourly: `V + FV`
- tri-hourly: `V + P + C`
- four-period: `SV + VN + P + C`

## Consequences

Recent days may initially appear using estimated 15-minute totals and then change when
the corresponding real midnight endpoint arrives. A materially inconsistent day is
corrected immediately but remains explicitly pending, so it keeps being revisited even
if E-REDES takes longer than the normal seven-day rolling window to publish credible
15-minute data.

The real index is displayed with coarser precision than the 15-minute curve, so small
mismatches are expected and are not reconciled. For the observed tri-hourly meter,
three integer registers (`V`, `P`, `C`) give a ±3 kWh acceptance envelope. Once a
pending day's raw 15-minute total returns inside that envelope, the flag is removed and
the more precise raw curve replaces the temporary scaled curve.

A physical meter replacement, missing midnight endpoint, incomplete load-curve day,
or ambiguous multiple-meter day is left unreconciled rather than guessed.

The **Last Real Data Day** sensor lets automations and dashboards distinguish the
latest day backed by real E-REDES register data from newer days that still depend only
on the 15-minute load curve. Its value is persisted so it survives restarts even when
a subsequent synchronization cannot obtain a newer real endpoint.
