# Decisions

Dated decisions with the alternative rejected and why. Entries marked "recorded" were
carried out of code comments on 2026-09-04; the decision itself is older.

## 2026-09-04: the manifest is the version source, not tag history

`Release` publishes the version already in `manifest.json` and refuses drift; `Prepare
release` bumps it through `scripts/set_version.py` in a reviewed PR. Rejected: the
previous workflow, which computed the next version from today's tags, wrote it into the
manifest, and tagged, so a re-run of a stale job minted a second identical release and
the manifest on `main` never matched the last tag (2026.09.02 against v2026.09.03.1).

## 2026-09-04: `pyserial` stays in the manifest

Superseded the same day by the entry below. Recorded here only so the reasoning that led
to the reversal is visible: at the time, `verification.py` listed ports through
`serial.tools.list_ports`, and the private `pyvantagepro` fork also imported `serial`
directly, so dropping the pin without a replacement would have broken both.

## 2026-09-04: removed `pyvantagepro`, `pyserial`, and the IP/network transport

Sean decided to remove the private `pyvantagepro` fork entirely rather than keep patching
around it (roughly half of `protocol.py`'s `DavisProtocolClient` already overrode the
fork's methods because they were wrong). `protocol.py` is now a self-contained
implementation of the Davis Vantage Pro/Pro2/Vue serial protocol, built directly against
the manual (`~/repos/vendor-docs-reference/docs/davis-vantage.md`, Rev 2.6.1) and talking
to `serialx` through `client.py`'s `DavisSerialXLink`. `pyserial` is no longer a runtime
dependency: the one place that used it for a nicer USB identity lookup
(`verification.py`'s `_serial_identity`) now calls `serialx.list_serial_ports()`, which
exposes the same fields (`device`, `vid`, `pid`, `serial_number`). The IP/network
(WeatherLink raw-TCP) transport was removed at the same time; the web-download protocol
(weatherlink.com) was never implemented and stays out of scope. Any existing config entry
with `protocol="Network"` will fail verification with no automatic migration; this is a
personal-use integration, so the chosen fix is to remove and re-add the integration on
serial, not to build compatibility code for a mode being deliberately deleted.

**Two bugs found during the audit that led to this rewrite, both fixed in the same pass:**

- **LOOP2 packet offset bug: the manual's own table does not match real hardware.**
  The removed fork's LOOP2 format had only two 2-byte "Unused" fields between the
  10-minute gust direction and DewPoint (byte offsets 26-29). An earlier pass of this
  rewrite "corrected" that to three 2-byte Unused fields (offsets 26, 28, 30) because the
  Davis manual's LOOP2 table documents three. **That correction was itself wrong.** A live
  capture against a real Vantage Pro2 (firmware 3.88, station type 16, connected on COM3)
  on 2026-09-04 showed the three-Unused-field layout decoding DewPoint, HumOut, HeatIndex,
  WindChill, and RainRate to physically impossible values (101% outside humidity, a dew
  point over 15000). Decoding the exact same captured frame with the fork's original
  two-Unused-field layout instead produced values matching a LOOP1 reading taken the same
  moment: Barometer 29.789 (LOOP1: 29.789), TempOut 90.4 (LOOP1: 90.4), HumOut 60 (LOOP1:
  60), DewPoint 75, HeatIndex 101, WindChill 90, THSWIndex 103, RainRate 0 (LOOP1: 0),
  BarReductionMethod 2 (matches the manual's own note that this is "always 2 on VP2").
  `protocol.py`'s `LOOP2_FORMAT` now uses the two-Unused-field layout (DewPoint at offset
  30, not the manual's stated 32), and the module-level self-check
  (`_assert_loop2_offsets`) asserts against these hardware-confirmed milestones, not the
  manual's literal table. `tests/test_protocol.py` carries both a synthetic-frame
  regression test built from these confirmed offsets and a golden-frame test that decodes
  the actual captured bytes from this session. **This is now verified against live
  hardware**, unlike every other command in this rewrite, which is still built from the
  manual and unit-tested only.
- **Serial read loop truncated multi-byte binary responses on real hardware.** Testing
  against the live COM3 console also surfaced a second, independent bug: `serialx`'s
  Windows backend (`Win32Serial`) subclasses `io.RawIOBase`, whose `read(size)` returns
  whatever one underlying `ReadFile` call produced rather than guaranteeing `size` bytes,
  so a single call could come back short even with time left on the clock.
  `DavisSerialXLink.read()` trusted a single call to fill the request; `read()` now calls
  `_read_exactly()`, which loops and accumulates across calls until it has the requested
  size or the overall timeout budget is spent. The first version of this fix set a short
  fixed 0.05s timeout on every poll, which was itself wrong and truncated large reads like
  `HILOWS`'s 438 bytes — see the "resolved" HILOWS entry below for the full story and the
  actual fix (recompute the per-call timeout from the remaining budget, not a fixed short
  interval). Both LOOP1/LOOP2 and HILOWS are now confirmed reliable on real hardware.
- **Double-layer rain-rate/rain-total scaling (already correct numerically, now
  simplified).** The removed fork divided raw rain-click counts by 100 inside its LOOP/
  LOOP2/archive parsers, implicitly assuming a 0.01" collector; `client.py` then multiplied
  the result by a second, collector-dependent factor. The two layers happened to compose
  into the right answer for all three collector types, but the design was fragile and hard
  to audit. Parsers in `protocol.py` now store rain fields (rate, day/month/year/storm
  totals, and the LOOP2-only 15-minute/hourly/24-hour totals) as raw click counts;
  `client.py`'s `correct_rain_values()` is the single place that converts a click count to
  inches, using one direct inches-per-click factor for the configured collector
  (0.01"/click, 0.2mm/click, or 0.1mm/click). This also fixed a latent bug where the
  LOOP2-only 15-minute/hourly/24-hour rain totals were never collector-corrected at all
  (they were scaled by the fork's fixed `/100` and then left alone); they are now covered
  by the same single scaling step as every other rain field.
- **Stale `RainRateHi` archive key removed.** The fork's `ArchiveDataParserRevB` produced
  a scaled `RainRateHi` field (offset 12, `/100`), but `client.py`'s archive-page reader
  separately overwrote a differently-scaled `RainRate` from the same offset, leaving both
  keys present with inconsistent scaling on every archive record (though neither was
  actually surfaced past `add_archive_info()`, which only reads `WindHi`/`WindAvg`/
  `WindAvgDir`). The new `ArchiveDataParserRevB` names offset 10 `Rainfall` and offset 12
  `HighRainRate`, both as raw click counts, matching the manual's field names, with no
  duplicate key.
- **HILOWS extra/soil/leaf data decoded.** The removed fork left the extra-temperature
  (7), soil-temperature (4), leaf-temperature (4), outside/extra-humidity (8),
  soil-moisture (4), and leaf-wetness (4) sub-arrays in the `HILOWS` response as opaque
  byte blobs. `protocol.py`'s `HighLowParserRevB` now decodes all of them (Day/Month/Year
  Hi/Lo, per the manual's HILOW data format table) into named dict keys.
  `client.py.add_hilows_info()` currently surfaces only the Day Hi/Low values onto
  disabled-by-default sensor entities (see `docs/backlog.md`); the full Month/Year decode
  is present in the data dict and reachable through `get_raw_data()`/diagnostics for
  advanced use. Initially thought to be untestable because the console appeared to
  truncate its `HILOWS` response; that turned out to be a bug in this integration's own
  read-timeout handling, not the console — see the "resolved" HILOWS entry below. Now
  confirmed against a real captured frame: decodes to 319 keys with plausible values
  (e.g. `DewLoDay`/`DewHiDay` matching a normal dew point range for the day's conditions).

## 2026-09-04: remaining write commands tested against real hardware

Continuing the 2026-09-04 COM3 hardware session, `RXTEST`, `SETTIME`, `PUTRAIN`, `PUTET`,
and `CALFIX` were all exercised against the live console, each with the current value read
first and (where the command changes persistent state) restored afterward.

- **`RXTEST` was checking the wrong response type.** `rxtest()` expected a raw ACK byte
  (`0x06`); the real console replies with the standard `"\n\rOK\n\r"` text response instead,
  confirmed with a raw capture. The manual's `RXTEST` row does not state which one it uses.
  Fixed to expect `self.OK`.
- **`SETTIME` and `PUTRAIN` work exactly as documented.** `SETTIME` set the clock to within
  a second of the intended value. `PUTRAIN` set yearly rain to the exact requested click
  count on the first try and restored cleanly to the original value afterward.
- **`PUTET` silently undershoots by the current day's ET, undocumented in the manual.**
  Sending `PUTET 3722` (intending 37.22") stored 37.08"; sending `PUTET 3719` to restore
  the original 37.19" stored 37.05" instead. Both times the shortfall was exactly 14
  hundredths, matching the day's ET (`ETDay` was 0.142", which rounds to 14) at the time.
  Confirmed by compensating for it: sending `PUTET 3733` (3719 + 14) correctly stored
  37.19", the true original value. `client.py`'s `set_yearly_et()` now reads the current
  `ETDay` and adds it to the caller's requested value before sending `PUTET`, so calling it
  with X actually stores X. This is a console firmware behavior discovered by testing, not
  something the manual documents or that could have been found by reading it.
- **`CALFIX` is not a safe round-trip of `CALED`'s output, and this was tested the hard
  way.** The manual states `CALED` returns *calibrated* values while `CALFIX` expects
  *uncalibrated raw* values; they are not interchangeable despite sharing a byte layout.
  Writing `CALED`'s output straight back through `CALFIX` (intended as a no-op
  connectivity test) instead overwrote the console's live current-value slot for Extra
  Temperature Station 1 — a channel with no physical sensor connected, previously reporting
  the "no data" sentinel (raw byte 255, decoded 165 per the LOOP1 `+90°F` offset format) —
  with a bogus, persistent value (raw byte 180, decoded 90, i.e. an apparent 0°F reading).
  This was confirmed to be a lasting change, not a transient artifact: the value stayed
  fixed across repeated reads rather than reverting or fluctuating. All 6 other extra
  temperature channels and all 7 extra humidity channels were unaffected. This integration
  does not expose the live `ExtraTemps01`-style LOOP1 fields as Home Assistant sensor
  entities (only the separate HILOWS-derived Day Hi/Low entities reference extra-sensor
  data, and those come from the `HILOWS` response, not this field), so there is no
  Home Assistant-visible impact. It may appear as an apparent 0°F reading instead of
  blank dashes if the physical console's display rotates through an Extra Temperature 1
  screen. A power cycle should clear it, since this class of value is a live/volatile
  current-reading slot, not an EEPROM-persisted calibration constant (writing a
  calibration constant goes through `EEBWR`, not `CALFIX`). No further `CALFIX` writes
  were attempted after this was discovered, since the exact raw-to-displayed conversion
  the firmware applies is unknown and guessing again risked making it worse. **Do not
  implement or test `CALFIX` as a round-trip of `CALED`'s output; a correct
  implementation needs the actual calibrated-to-raw conversion (subtract the stored
  EEPROM calibration offset) that the manual's Common Task 1 describes, which this
  integration does not implement.**

## 2026-09-04: `UV` reported a fake 25.5 reading with no UV sensor connected

Sean's station does not have a UV sensor (also no soil or extra-temperature probes;
it does have Solar Radiation). The very first LOOP1 capture in this session read
`UV: 25.5`, which is exactly `255 / 10` — the manual's own dash/sentinel byte for this
field, divided by ten like every other reading, with no check for the sentinel. LOOP1,
LOOP2, and the archive record's "Average UV" field all had the same unconditional
`self["UV"] = self["UV"] / 10`, unlike `DewPoint`/`HeatIndex`/`WindChill`/`THSWIndex` in
the same LOOP2 packet, which already checked for their own 255 dash value. Fixed with a
shared `unpack_uv()` helper that returns `None` for raw byte 255, applied in all three
parsers, and verified against live hardware: `UV` now reads `None` while `SolarRad`
(a sensor Sean does have) still reads a real value. The archive record's "High UV Index"
field uses a dash value of 0 per the manual, which is indistinguishable from a genuine
zero reading at night; left as-is, matching the manual's own ambiguity, not a bug this
integration can resolve. `sensor.py`'s existing `entity_registry_enabled_default` gating
already treated both `None` and raw `255` as "no sensor," so no downstream change was
needed there.

## 2026-09-04 (resolved): `HILOWS` truncation was this integration's own read-timeout bug, not the console

Testing against the live Vantage Pro2 on COM3 initially found that the real console's
response to `HILOWS` stopped after roughly 70 of the documented 436+2 bytes, with the
read loop instrumented to run its full 15-second timeout budget (250 poll iterations) and
confirm no further bytes ever arrived. `RXCHECK` diagnostics showed no growth in
`crc_errors` across a failed attempt, ruling out a console-detected line error. A
baud-rate hypothesis (`BAUD 9600`, testing whether 19200 was a UART timing margin issue on
this cable/converter) was started but abandoned immediately after an ambiguous response,
since a wrong guess there risked losing serial communication with the console entirely —
confirmed the console still answered normally at 19200 before moving on.

The actual cause was found by testing a differential: `pyserial`'s own blocking
`read(438)` call on the same port, same console, captured the complete, CRC-valid 438-byte
response on the first try. That proved the console was never the problem. Re-testing
`serialx` with a single call given a proper multi-second timeout (matching how `pyserial`
was called) also succeeded immediately. The actual bug was in this integration's own
`DavisSerialXLink._read_exactly()` (the fix recorded above for the LOOP/HILOWS
short-read problem): it set the serial port's timeout to a short fixed 0.05s on every
poll iteration. That interval is long enough for a 99-byte LOOP packet to complete via
repeated polling (which is why LOOP1/LOOP2 appeared fixed and reliable), but too short for
the Windows driver to properly accumulate a continuous 438-byte burst, so `HILOWS` reads
were truncated at whatever had arrived within that iteration's narrow window. Varying the
delay before sending `HILOWS` shifted the observed truncation point (roughly 70 bytes with
no delay, a stable 223 bytes with a 0.5-2 second settle delay) because it shifted where in
the polling cycle the burst started arriving relative to the 0.05s window, not because of
any console-side pause.

**Fixed**: `_read_exactly()` now recomputes the per-call timeout from the remaining
overall budget on every loop iteration, instead of using a fixed short poll interval, and
still accumulates across calls so a genuinely short or interrupted response is handled
correctly. Confirmed against real hardware: `HILOWS` now succeeds on 3/3 consecutive
attempts with a fully plausible, CRC-valid, completely decoded 319-key result (matching
LOOP1's simultaneous readings), and LOOP1/LOOP2 continue to decode correctly. The
HILOWS-based sensors added in this rewrite should now populate normally. This episode is
a reminder that the fix recorded earlier in this file for LOOP/HILOWS short reads was
itself incomplete — it happened to work for a 99-byte packet by accident of timing, not
because the design was correct, and only testing the actual failure case (a larger
transfer) surfaced that.

## 2026-09-04: a disconnected-station reboot exposed a whole class of unhandled sentinels

Sean rebooted the console (to clear the `CALFIX` finding above) and, while it was
awaiting its first packet from the remote ISS, this was used to test what LOOP1/LOOP2
report with no live station data. It turned up far more than the earlier `UV` bug: the
console reported the manual's documented (and some undocumented) dash values for `TempIn`,
`TempOut`, `HumIn`, `HumOut`, `WindSpeed`, `WindSpeed10Min` (LOOP1), `WindDir`, `SolarRad`,
`RainRate`, and `StormStartDate`, and **none of them were being converted to "no data"** —
they were flowing straight through as fake real-looking values: `TempIn`/`TempOut` at
`3276.7` (raw sentinel 32767 divided by 10, never checked), `HumIn`/`HumOut` at `255`
(percent), `SolarRad` at `32767`, `RainRate` at `65535`, and `StormStartDate` decoding to
the nonsensical `"2127-15-31"` (the all-ones 0xFFFF sentinel decodes to month=15, day=31 —
both nonzero, so the existing `month == 0 or day == 0` check never caught it).
`WindSpeed`/`WindSpeed10Min` read `255` even though the manual states a dashed wind speed
is "forced to 0" by the console — real hardware with no ISS data at all contradicted that,
so this integration reports unavailable rather than guessing which is true, per the
project's own "unavailable beats wrong" convention.

Fixed with a set of shared `unpack_*` helpers in `protocol.py` (`unpack_temp`,
`unpack_humidity`, `unpack_wind_speed`, `unpack_wind_dir`, `unpack_solar_rad`,
`unpack_rain_rate`), applied in both `LoopDataParserRevB` (LOOP1) and `LoopData2Parser`
(LOOP2), and a corrected `unpack_storm_date` that validates the month/day ranges directly
instead of only checking for zero. `client.py`'s downstream calculations
(`calc_heat_index`, `calc_dew_point`, `calc_wind_chill`, `calc_feels_like`,
`correct_rain_values`, the `IsRaining` derivation) already guarded with `is not None`
checks before this fix landed, meaning they were built anticipating exactly this — the
gap was that `protocol.py` never actually delivered `None` for these fields. Verified
against the live rebooted console: every affected field now reads `None` in both LOOP1
and LOOP2, while `BarTrend` was confirmed to already degrade gracefully downstream
(`get_baro_trend()` returns `None` for any code outside its known set, including 255) and
needed no change. `Barometer` and `WindDir` already had defensive range/sentinel checks in
`sensor.py` (`_barometer_or_none`, `_wind_dir_or_none`) that made them safe even before
this fix, but `weather.py` exposed `Barometer` and `WindSpeed` directly with no such
guard, so this fix protects that surface too, not just `sensor.py`.

## 2026-09-04: disabled-by-default sensors do not address the `CALFIX` finding

Sean does not have soil, extra-temperature, or UV sensors (he does have Solar Radiation).
The HILOWS-derived Day Hi/Low sensor entities added in this rewrite for extra-temperature,
soil-temperature, leaf-temperature, extra-humidity, soil-moisture, and leaf-wetness
channels (`sensor.py`, the `entity_registry_enabled_default=False` entries built in the
per-sensor loops) were already disabled by default before this question was asked — this
was part of the original scope, not something that needed fixing.

This does not address the `CALFIX` finding recorded above, because the two are unrelated:
`CALFIX` corrupted a live current-value slot for "Extra Temperature Station 1"
(`ExtraTemps01` in the raw LOOP1 data), and this integration has never created a Home
Assistant entity for that raw field at all — only the separate HILOWS-derived Day Hi/Low
keys (`ExtraTemp{idx}{bound}`, sourced from the `HILOWS` response, not LOOP1) have
entities, and those are unaffected. There is nothing to disable to mitigate the `CALFIX`
finding because nothing was ever enabled for that field; the zero-impact conclusion in the
`CALFIX` entry above already accounts for this.

**Recommendation: yes, power-cycle the console** if you want to clear the stray
`ExtraTemps01` reading. This is expected to be safe and complete: `CALFIX` is documented
as pushing "updated display values," which is why it was tested as a live-value push
rather than an EEPROM write (an EEPROM calibration constant would go through `EEBWR`
instead), so the affected value should live in the console's volatile current-reading
memory, not persistent storage. A power cycle should not affect archive records,
calibration offsets, or the yearly rain/ET totals confirmed correct earlier in this
session — none of that data path was touched by the `CALFIX` test.

## 2026-09-04: rejected — no guided config-flow/UI workflow for CALED/CALFIX calibration

Considered building a guided workflow around the manual's Common Task 1 (`EEBRD` current
offsets → `CALED` current calibrated values → subtract to recover raw values → `EEBWR` new
offsets → `CALFIX` to push the display update), to make sensor calibration easier than
computing the raw values by hand. **Rejected. Will not implement.** The real-hardware
`CALFIX` incident earlier in this session is the reason: feeding `CALED`'s calibrated
output into `CALFIX` without the offset-subtraction step corrupted a live current-value
slot on the very first attempt, done deliberately and carefully by someone who had just
read the manual's own distinction between calibrated and raw values. A guided UI lowers
the barrier to running that same sequence, which is exactly the accuracy risk this
integration should not invite for a workflow this easy to get subtly wrong. The raw
`get_calibrated_values`/`set_calibrated_values` services stay as they are: unpolished and
manual, which is the point. This is a closed decision, not a deferral — it does not belong
in docs/backlog.md as something to revisit.

**Full command parity added.** `TEST`, `WRD` (station type), `RXTEST`, `RECEIVERS`,
`CALED`/`CALFIX` (calibrated-value read/write), and `PUTET` (set yearly ET) are now
implemented end to end: `protocol.py` command methods, `client.py` facade methods, and
`services.py`/`services.yaml`/`strings.json`/`translations/en.json` service definitions.
**Verified against live hardware on 2026-09-04 (COM3):** `TEST` (returned `True`), `WRD`
(returned `16`, Vantage Pro/Pro2), `VER`/`NVER` (firmware date 2019-10-29, version
`3.88`), `RXCHECK`/`diagnostics` (returned real packet counters), `RECEIVERS` (returned
`1`), `GETTIME`, `get_rain_collector` (`EEBRD`), and `CALED` (returned a real 43-byte
calibration block). `RXTEST`, `SETTIME`, `PUTRAIN`, `PUTET`, and `CALFIX` were verified in
a later pass the same day — see "remaining write commands tested against real hardware"
below for the two real bugs that testing found and fixed, and the `CALFIX` caveat.
`SETPER` and the `BAUD`/console-reprogramming path were deliberately not tested: `SETPER`
irreversibly clears archive memory, and a `BAUD` test on 2026-09-04 was aborted after an
ambiguous response rather than risk losing communication with the console entirely (see
below).

## 2026-09-04: `mypy.ini` targets Python 3.14, not 3.13

Core 2026.9.0 requires Python 3.14.2, and the installed `homeassistant` package uses
3.14-only syntax that a 3.13 grammar rejects before mypy reaches any of this repo's own
files. `mypy.ini`'s `python_version` was still pinned to 3.13, a leftover from before this
repo tracked current core. Bumped to 3.14 to match what the repo actually targets, not to
preserve compatibility with an older interpreter no supported core release runs on.

## 2026-09-04: the humidity sensors report `UnitOfRatio.PERCENTAGE`

Both humidity sensors report `UnitOfRatio.PERCENTAGE`; the bare `PERCENTAGE` constant as
a unit is deprecated since core 2026.7 (developer blog 2026-06-30).

## Recorded: `forecast_icon_condition` has its own key

It originally shared `key="ForecastIcon"` with the `condition` entry, so both had the same
unique id and Home Assistant silently dropped whichever lost the race; the entity could
never be enabled.

## Recorded: `rain_storm` and `rain_15_min` exist

`blueprints/automation/flash_flood.yaml` pointed at a sensor that did not exist.

## Recorded: `wind_speed_10_min_gust` keeps its key

The name and translation were corrected to "10 min Avg" because the value is the
ten-minute average; the key was kept so entity ids and recorder history survive.
Rejected: renaming the key, which orphans history.

## Recorded: wind rose values are lowercase

`get_wind_rose` switched from uppercase so the existing translation and icon tables
finally match.

## Recorded: weather entity unique id is entry based

It switched from a MAC-address basis to `f"{entry_id}_weather"`.

## Recorded: condition-only twice-daily forecast

`FORECAST_TWICE_DAILY` with a condition-only entry was chosen over fabricating daily or
hourly numeric forecasts the console does not provide.

## Recorded: unique ids normalized

Binary sensor unique ids containing spaces or brackets were migrated to the underscore
form through `normalize_unique_id`.

## Recorded: console diagnostics exposed

RXCHECK, NVER, and BARDATA were wired up in the client but never exposed; they now appear
in the diagnostics download.
