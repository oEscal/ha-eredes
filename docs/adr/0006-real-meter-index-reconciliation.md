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
5. When a complete 15-minute day and a real daily total both exist, scale that day's
   15-minute values proportionally so their sum equals the real cumulative-index
   delta. This keeps the best available intraday shape while making the Home Assistant
   daily total authoritative.
6. When no real daily pair exists yet, retain the 15-minute load curve unchanged.
7. Rebuild a seven-day rolling statistics tail on normal synchronizations rather than
   only appending. Seed the cumulative `sum` from the persisted hour immediately
   before that tail. This allows delayed real readings to correct already-imported
   estimated days and propagates the correction through subsequent cumulative sums.
8. Bump the historical import version so existing installations perform one full
   rebuild and remove already-stored incorrect recent totals.

## Register layouts

The formatted active-import cumulative index is calculated according to the register
layout exposed by the portal:

- simple: `S`
- bi-hourly: `V + FV`
- tri-hourly: `V + P + C`
- four-period: `SV + VN + P + C`

## Consequences

Recent days may initially appear using estimated 15-minute totals and then change when
the corresponding real midnight endpoint arrives. This is intentional: the real
cumulative register is authoritative for the daily total.

The real index is commonly displayed with coarser precision than the 15-minute curve,
so reconciling a day can sacrifice some sub-kWh total precision. The 15-minute relative
shape is preserved.

A physical meter replacement, missing midnight endpoint, incomplete load-curve day,
or ambiguous multiple-meter day is left unreconciled rather than guessed.
