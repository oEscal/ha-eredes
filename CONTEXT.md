# E-REDES Integration

A Home Assistant custom integration that fetches electricity consumption from the
E-REDES Balcão Digital portal. This glossary fixes the vocabulary of two areas that
are easy to confuse: the **metering domain** (what the data represents) and the
**authentication model** (how requests are credentialed).

## Metering

**CPE**:
The identifier of a single electricity delivery point (*Código de Ponto de Entrega*),
e.g. `PT0002000012345678AB`. One configured meter corresponds to one CPE.
_Avoid_: "meter id" (the meter and the delivery point are distinct — see **Meter**).

**Meter**:
The physical metering device serving a CPE, identified by its serial number
(`meterReaderSerialNumber`). Reported as a `utilitiesDevice` in API responses.

**Register**:
A metering channel identified by an energy-flow code. `A+` is active energy
**imported** from the grid (consumption); `A-` is active energy **exported** to the
grid (injection, e.g. rooftop solar). The integration currently reads only `A+`.
_Avoid_: "channel", "direction".

**Load curve**:
The time series of energy per fixed 15-minute interval for one register, as returned
by the `edm/get` endpoint (`meterLoadCurves` → `loadCurves`).

Timestamps are **Europe/Lisbon local time carrying a misleading `Z` suffix** — they
are *not* UTC. Verified on 2026-08-09: the spring-forward day `2026-03-29` returns 92
points with the 01:00–01:59 hour absent (00:45 jumps to 02:00), and the fall-back day
`2025-10-26` returns 100 points with 01:00/01:15/01:30/01:45 each appearing twice.
True UTC would be a flat 96 every day. Treating the suffix at face value shifts all
WEST-period (summer) data one hour late, and collapses the two ambiguous fall-back
hours into one bucket. See `docs/adr/0004`.

**Reading**:
A single load-curve point — the energy consumed (or exported) during one 15-minute
interval. The timestamp identifies the **end** of that interval: the portal's own
`request_type=3` history request starts a calendar month at `00:15` and ends it at the
following month's `00:00`. Hourly aggregation therefore subtracts 15 minutes before
choosing the hour bucket. Carries an optional `meterLoadCurveStatus` flag taking
values `0`, `1`, `2` or no value at all.

That flag does **not** separate real readings from estimates, and must not be filtered
on. Verified on 2026-08-09 against `request_type=1` cumulative meter indexes: across
five older multi-week spans, summing *every* load-curve point reproduced the index
delta to within its 1 kWh quantization (99.5%–101.2%). Keeping only status `0` yielded
0%–27% of true consumption; keeping `0` and unflagged yielded 0%–86%. One 14-day span
carried status `1` on 1343 of 1344 points, so any such filter would have silently
discarded the entire period. This historical agreement is not a guarantee for recent
data: on 2026-08-11 recent August load curves materially disagreed with valid real
midnight meter indexes. Historical statistics therefore reconcile complete days to
real index deltas when available; see `docs/adr/0006`.
_Avoid_: "measurement", "sample".

**Meter index**:
A cumulative active-import register reading from `request_type=1`, `formatted=true`,
as shown under **Leituras > Consultar histórico**. Valid real readings have `mrType=1`
(operator) or `mrType=2` (customer) and status `activa`/`corrigida`. For the same
physical meter, the difference between consecutive Europe/Lisbon midnight indexes is
the authoritative consumption total for the intervening local calendar day. The
integration compares the raw 15-minute total with this real daily delta. Because each
cumulative tariff register is integer-valued, the delta carries roughly ±1 kWh of
quantization uncertainty per register: ±1 kWh for simple, ±2 kWh for bi-hourly, ±3 kWh
for tri-hourly (`V + P + C`), and ±4 kWh for four-period meters. A complete raw day
inside that envelope is accepted unchanged. A day outside it is scaled to the real
delta and persisted as pending reconciliation until a later complete, continuous raw
15-minute fetch falls back inside the envelope. Incomplete days with a real delta are
also pending. Historical synchronization normally rewrites a seven-day rolling tail,
but extends back to the oldest pending day so delayed corrections are never abandoned.
The latest calendar day with a valid consecutive-midnight real delta is persisted and
exposed through the **Last Real Data Day** sensor. Exact duplicate `(timestamp, value)`
rows from repeated API `A+` groups are collapsed before totals are evaluated; conflicting
duplicates are kept as ambiguous. Separately, every complete 15-minute day that falls
inside the applicable tolerance without real-total scaling is persisted as a matching
day; the newest is exposed through **Last Matching 15-Min Data Day**. A later refetch
that makes a previously matching day incomplete or inconsistent removes it from that
set, so the sensor can move backward. For example, a latest real meter index at
2026-08-08 00:00 makes 2026-08-07 the latest reliable consumption day. See
`docs/adr/0006`.

**Provisional current-day energy**:
A temporary estimate written to the same `eredes:energy_<cpe suffix>` external
statistic while E-REDES has no load curve for the current Europe/Lisbon calendar day.
It is derived from Home Assistant Energy Dashboard `device_consumption` statistics,
summing only top-level entries (those without `included_in_stat`) and excluding the
E-REDES statistic itself. Recorder finalized hourly `change` values are combined for
past hours, while 5-minute short-term `change` values keep the open current hour fresh;
the cumulative sum is seeded from the latest E-REDES statistic before local midnight,
searching daily-reduced history rather than only the preceding 24 hours because
E-REDES can lag by multiple days. If no prior cumulative sum is available, no
provisional rows are written instead of starting a new zero-based series. Recorder
imports are verified by polling the persisted boundary rows because an asynchronous
import can already be dequeued while its database transaction is still in flight. The
estimate refreshes every 15 minutes using local Home Assistant data only. It is a lower bound,
because untracked loads are absent; solar/battery installations can also
make household device load differ from grid import. Completed-day provisional rows
are replaced by the normal E-REDES history/reconciliation path once E-REDES data is
fetched. Provisional data never affects **Last Real Data Day** or **Last Matching
15-Min Data Day**. See `docs/adr/0007`.
_Avoid_: "real-time E-REDES data", "real grid consumption".

## Authentication

**Access token (`aat`)**:
The JWT credential minted at login. Carries a hard `exp` claim **91 minutes** after
issue, and is **never** re-issued — not by `/session`, not by page loads, not by the
portal's own `token-check` or `reserved-area-token` endpoints. It therefore cannot be
refreshed without logging in again. Travels in the `Cookie` header; the data endpoint
equally accepts it as `Authorization: Bearer`.
_Avoid_: "session cookie", bare "token" (both are ambiguous — see below).

**Bot gate (`Authorization-Request`)**:
A header the portal fills with a **reCAPTCHA token**, checked independently of the
credential. On `/ms/*` data endpoints the gateway only verifies the header is
*present*, so this integration passes the `aat` there; on signin it validates a real
token, which is why login cannot be automated. A request carrying a valid credential
but no `Authorization-Request` is refused with `403` and a `recaptcha: true` response
header. See `docs/adr/0003`.
_Avoid_: reading the name as "authorization" — it authorizes nothing.

**Server session (`PHPSESSID`)**:
A **secondary** cookie the server issues and rolls on every response (90-minute
sliding window). It rides along with requests and is bootstrapped even from a
bare-`aat` first call, but it is not a credential — keeping it fresh does not extend
access past the access token's expiry.
_Avoid_: bare "session" (collides with the aiohttp HTTP client session).

**Login**:
The interactive, browser-based sign-in that mints an `aat`. Protected by Google
reCAPTCHA and therefore not automatable — the reason the `aat` is pasted in by hand.
The E-REDES mobile app offers no way around this: it is a Capacitor shell that loads
this same web portal in a WebView (see `docs/adr/0001`).
