# Protocol and console facts

Wire-level and console behavior the code depends on, from the Davis Vantage Serial
Communication Reference Manual Rev 2.6.1 unless a row says otherwise. Each table row says
whether the fact is verified in code, verified against the manual section cited in the
original code, or carried over unverified.

## Sentinel values

| Field type | Sentinel | Meaning | Status |
| --- | --- | --- | --- |
| one-byte percent/UV fields (`HumIn`, `HumOut`, `UV`) | 255 | dashed or invalid: lost sync, unplugged, or sensor not fitted | verified in `protocol.py` (`unpack_humidity`, `unpack_uv`) |
| one-byte wind speed (`WindSpeed`, `WindSpeed10Min`) | 255 | dashed; the manual says "forced to 0" but real hardware with no ISS data reported 255 instead (2026-09-04) | verified in `protocol.py` (`unpack_wind_speed`) |
| signed two-byte temperature fields (`TempIn`, `TempOut`) | 32767 | dashed or invalid | verified in `protocol.py` (`unpack_temp`) |
| unsigned two-byte solar radiation (`SolarRad`) | 32767 | dashed or invalid | verified in `protocol.py` (`unpack_solar_rad`) |
| unsigned two-byte rain rate (`RainRate`) | 65535 (0xFFFF) | dashed or invalid | verified in `protocol.py` (`unpack_rain_rate`) |
| wind direction | 0 or 32767 | 0 is the manual's own "no data" code (north is 360); 32767 is an additional sentinel confirmed on real hardware (2026-09-04) | verified in `protocol.py` (`unpack_wind_dir`) and `sensor.py` (`_wind_dir_or_none`) |
| packed storm-start-date | 0xFFFF (decodes to month=15, day=31) | dashed; a month/day range check catches this, not just a zero check | verified in `protocol.py` (`unpack_storm_date`) |
| forecast icon | 255, 32767, -32768 | missing | verified in `weather.py` |
| barometer | raw 0 (0.0 inHg) | dashed; out of the console's 20.0-32.5" valid range | verified in `sensor.py` (`_barometer_or_none`) |
| barometer trend | any code outside `{-60, -20, 0, 20, 60, 196, 236}` | "insufficient 3-hr bar history" per the manual, or dashed | verified in `utils.py` (`get_baro_trend`, returns `None`) |

All of the `protocol.py`-level sentinel handling except `UV` and `WindDir` was added or
corrected on 2026-09-04, discovered by rebooting the console and capturing LOOP1/LOOP2
while it had no live ISS data at all — every affected field was previously passing the
raw sentinel straight through as a fake reading (`TempOut` showing `3276.7`, `RainRate`
showing `65535`, etc.). See docs/decisions.md for the full account.

## LOOP1 packet

Sent as `LOOP 1` after wake-up; 99 bytes. Validation: exactly 99 bytes, valid CRC, bytes
0 to 2 equal `LOO`, byte 4 (packet type) equals 0, bytes 95 and 96 equal `\n\r`. Rev A
packets are not supported. LOOP1 is the only packet that carries alarm bits, battery
status, the forecast icon, and sunrise and sunset.

Alarm bytes sit at offsets 70 to 85, bits numbered least-significant first:

| Byte | Contents |
| --- | --- |
| 70 | inside alarms: falling and rising bar trend, low and high inside temperature, low and high inside humidity, time |
| 71 | rain alarms: high rate, 15 minute, 24 hour, storm total, ET daily |
| 72 | outside alarms: low and high temperature, wind speed, ten-minute average speed, low and high dew point, high heat, low wind chill |
| 73 | high THSW, high solar, high UV, UV dose, UV dose enabled |
| 74 bits 2 and 3 | outside low and high humidity |
| 75 to 81 | extra temperature and humidity sensors 1 to 7: low and high temperature, low and high humidity |
| 82 to 85 | soil and leaf sensors 1 to 4: low and high leaf wetness, soil moisture, leaf temperature, soil temperature |

All rows verified in `protocol.py`. Byte 86 is the transmitter battery status: 1 means
low, 0 means normal (`_evaluate_tx_battery`).

## LOOP2 packet

Requested with `LPS 2 1` (manual section IX.2); 99 bytes, packet type 1, same `LOO`
signature, terminator, and CRC as LOOP1. `protocol.py`'s `LOOP2_FORMAT` is little-endian:
LOO (3s), BarTrend (B), PacketType (B), 1 unused (2s), Barometer (H, /1000 inHg), TempIn
(h, /10 F), HumIn (B), TempOut (h, /10 F), WindSpeed (B, mph), 1 unused (1s), WindDir (H),
WindSpeed10Min (H, /10), WindSpeed2Min (H, /10), WindGust10Min (H, /10), WindGustDir10Min
(H), **two** 2-byte unused fields (offsets 26 and 28), DewPoint (h, offset 30), 1 unused
(1s), HumOut (B), 1 unused (1s), HeatIndex (h), WindChill (h), THSWIndex (h), RainRate (H,
raw clicks/hr), UV (B, /10), SolarRad (H), RainStorm (H, raw clicks), StormStartDate (H),
RainDay (H, raw clicks), RainLast15Min (H, raw clicks), RainLastHour (H, raw clicks), ETDay
(H, /1000), RainLast24Hr (H, raw clicks), BarReductionMethod (B), UserBarOffset (h),
BarCalNumber (h), BarSensorRaw (H), BarAbsolute (H), AltimeterSetting (H), GraphPointers
(10s), 1 unused (14s), EOL (2s), CRC (H). A module-level self-check
(`_assert_loop2_offsets`) asserts the offsets of several milestone fields at import time.

**Verified against live hardware on 2026-09-04 (see docs/decisions.md).** The Davis
manual's own LOOP2 table documents **three** 2-byte unused fields at offsets 26, 28, and
30, placing DewPoint at offset 32. That is what an earlier pass of this rewrite
implemented, reasoning from the manual alone. A live capture against a real Vantage Pro2
(firmware 3.88) on COM3 proved the manual wrong for this console: the three-unused-field
layout decoded DewPoint, HumOut, HeatIndex, WindChill, and RainRate to physically
impossible values, while the two-unused-field layout above (DewPoint at offset 30) decoded
the identical captured bytes to values matching a simultaneous LOOP1 reading exactly
(Barometer, TempOut, HumOut, RainRate) and physically plausible values for everything
else. `tests/test_protocol.py` locks this in with both a synthetic-frame test built from
these confirmed offsets and a golden-frame test that decodes the actual captured bytes.

DewPoint, HeatIndex, WindChill, and THSWIndex read 255 when missing. Rain fields (rate,
storm, day, 15-minute, hourly, 24-hour) are raw click counts, not pre-scaled inches; see
"Rain-rate and rain-total scaling" below. LOOP2 carries console-computed dew point, heat
index, wind chill, and THSW (Davis's apparent temperature), a rolling ten-minute gust and
average, the direction of the ten-minute gust but no ten-minute average direction,
RainLast15Min, and ETDay. It carries no alarm bits, battery status, forecast icon, or
sunrise and sunset.

## Rain-rate and rain-total scaling

`protocol.py`'s LOOP1, LOOP2, and archive-record parsers all store rain fields (rate, day,
month, year, storm, and the LOOP2-only 15-minute/hourly/24-hour totals) as **raw click
counts**, not pre-scaled inches. `client.py`'s `correct_rain_values()` is the single place
that converts a click count to inches, using one direct inches-per-click factor for the
console's configured rain collector: 0.01 in/click (imperial), 0.2 mm/click, or 0.1
mm/click. An earlier version of this integration divided click counts by 100 inside the
parser (assuming a 0.01" collector) and then applied a second, collector-dependent factor
in `client.py` on top of that; the two layers happened to compose into the correct answer,
but the design was fragile, and the LOOP2-only 15-minute/hourly/24-hour totals were never
actually collector-corrected under the old design. Verified in code
(`tests/test_client.py::TestCorrectRainValues`).

## Forecast icons (manual section IX.1)

| Code | Console icon | Meaning | Home Assistant condition | Status |
| --- | --- | --- | --- | --- |
| 0 | none | default while the console waits for the three-hour barometric trend | sunny | unverified: code 0 is not in the section IX.1 table as far as the code shows |
| 8 | sun | mostly clear | sunny | verified in code |
| 6 | partial sun and cloud | partly cloudy | partlycloudy | verified in code |
| 2 | cloud | mostly cloudy | cloudy | verified in code |
| 7 | partial sun, cloud, rain | rain within 12 hours | rainy | verified in code |
| 3 | cloud and rain | rain within 12 hours | cloudy | verified in code |
| 18 | cloud and snow | snow within 12 hours | snowy | verified in code |
| 22 | partial sun, cloud, snow | snow within 12 hours | snowy | verified in code |
| 19 | cloud, rain, snow | rain or snow within 12 hours | snowy-rainy | verified in code |
| 23 | partial sun, cloud, rain, snow | rain or snow | snowy-rainy | verified in code |

The forecast is that single icon plus a canned text rule number (`ForecastRuleNo`); there
is no per-day or per-hour numeric forecast.

## Barometer

The console cannot log a reading outside 20.000 to 32.500 inHg; anything else is a bad
read. The `BAR=` calibration command (manual section VIII.5) takes the station elevation
in feet (-2000 to 15000), always required, and optionally a known-good local reading in
that range to fine-tune the console's adjusted pressure, or 0 to clear the offset. The
wire form is `BAR=<bar_inhg*1000 or 0> <elevation_ft>`. Verified in code.

## EEPROM (manual section XIII)

The address space is 4 KiB. The console has no hardware write protection and accepts any
bytes at any address, so `_PROTECTED_EEPROM_RANGES` refuses writes overlapping:

| Range | Contents |
| --- | --- |
| 0x01 to 0x04 | factory barometer calibration |
| 0x05 to 0x06 | barometer calibration managed by `BAR=` |
| 0x07 to 0x0A | factory humidity calibration |
| 0x0F to 0x10 | elevation managed by `BAR=` |
| 0x2D | archive period managed by `SETPER` |

Latitude, longitude, and elevation live at 0x0B as three little-endian signed shorts,
latitude and longitude in tenths of a degree. The rain collector type lives in the setup
bits at 0x2B: 0x00 for 0.01 in, 0x10 for 0.2 mm, 0x20 for 0.1 mm, masked with 0xCF on a
read-modify-write followed by `NEWSETUP`. Verified in code.

## Wake-up, commands, and baud rates

The wake exchange writes `\n` and expects `\n\r`, retried up to three times. Commands
used: `LAMPS 0` and `LAMPS 1` (backlight, expects `\n\rOK\n\r`), `CLRBITS` (clear alarm
bits, expects ACK 0x06), `RXCHECK`, `NVER` (firmware version, Vantage Pro2 and Vue only),
`BARDATA`, `BAR=`, `EEBRD` and `EEBWR`, `SETPER` (valid archive periods 1, 5, 10, 15,
30, 60, and 120 minutes), `GETTIME` (8 bytes with CRC), `HILOWS` (438 bytes with CRC), and
`DMPAFT`. A response starting with 0x21 is a NAK. Valid baud rates, fastest first, are
19200, 14400, 9600, 4800, 2400, and 1200; the default is 19200, 8N1. Serial/USB is the
only supported transport; the IP/network (WeatherLink raw-TCP) transport and the web
download protocol (weatherlink.com) were removed and are out of scope respectively. See
docs/decisions.md.

`DavisSerialXLink.read()` (`client.py`) does not trust a single call to fill a multi-byte
binary request: `serialx`'s Windows backend subclasses `io.RawIOBase`, whose `read(size)`
can return fewer bytes than requested even with time left on the clock. `_read_exactly()`
loops and accumulates across calls, recomputing the per-call timeout from the remaining
overall budget on every iteration, until it has the requested size or the budget is spent.
Confirmed against real hardware on 2026-09-04: LOOP (99 bytes) and HILOWS (438 bytes)
reads are both reliable with this design. An earlier version of this fix used a short
fixed 0.05s poll interval instead, which happened to work for LOOP (fast enough to
complete via many polls) but truncated HILOWS's larger continuous burst — see
docs/decisions.md's "resolved" HILOWS entry for the full story, including the `pyserial`
differential test that proved the console was never the problem.

## Full command parity

`protocol.py`/`client.py`/`services.py` implement every documented command, including
these added in the same pass that removed the `pyvantagepro` fork:

| Command | Purpose | Client method | Service |
| --- | --- | --- | --- |
| `TEST` | Connection sanity check; echoes `"TEST"` back | `test()` / `async_get_test()` | `run_test` |
| `WRD<0x12><0x4D>` | Station-type byte (16 = VP/Pro2, 17 = Vue) | `get_station_type()` / `async_get_station_type()` | `get_station_type` |
| `RXTEST` | Leaves the "Receiving From..." screen, clears the RXCHECK CRC-error count | `rxtest()` / `async_rxtest()` | `rxtest` |
| `RECEIVERS` | Bitmap of transmitter IDs the console can hear | `get_receivers()` / `async_get_receivers()` | `get_receivers` |
| `CALED` | Read the 43-byte calibrated sensor-value block | `get_calibrated_values()` / `async_get_calibrated_values()` | `get_calibrated_values` |
| `CALFIX` | Write a 43-byte uncalibrated raw sensor-value block | `set_calibrated_values()` / `async_set_calibrated_values()` | `set_calibrated_values` |
| `PUTET` | Set yearly ET (100ths of an inch) | `set_yearly_et()` / `async_set_yearly_et()` | `set_yearly_et` |

Each has a `tests/test_protocol.py` unit test built from the manual's documented
request/response framing. Verified against live hardware on 2026-09-04: `TEST`, `WRD`,
`RXCHECK`/diagnostics, `RECEIVERS`, `CALED`, `RXTEST`, `SETTIME`, `PUTRAIN`, `PUTET`, and
`CALFIX`. Two of these needed a real fix after testing, not just confirmation:

- `RXTEST` expected the wrong response type (a raw ACK instead of the standard OK text
  response); fixed.
- `PUTET` (`set_yearly_et`) undershoots by the current day's ET, an undocumented console
  quirk; `client.py` now compensates by adding the day's current ET before sending.

**`CALFIX` works as documented but is dangerous to test as a round-trip of `CALED`'s
output** — they share a byte layout but `CALED` returns calibrated values while `CALFIX`
expects uncalibrated raw ones. Doing so on real hardware overwrote a live current-value
slot with a persistent bogus reading (see docs/decisions.md for the full account and why
it has no Home Assistant-visible impact). Do not implement a "push calibration" feature
by round-tripping `CALED` into `CALFIX`; it needs the actual raw-to-calibrated conversion
the manual's Common Task 1 describes, which this integration does not implement. See
docs/decisions.md for the full verified/unverified callout.

## HILOWS extra/soil/leaf decode

`HighLowParserRevB` (`protocol.py`) fully decodes the extra-temperature (7 sensors),
soil-temperature (4), leaf-temperature (4), outside/extra-humidity (8), soil-moisture (4),
and leaf-wetness (4) sub-arrays documented in the manual's HILOW data format table
(byte offsets 126-436), rather than leaving them as opaque byte blobs. Decoded keys follow
the pattern `{SensorPrefix}{NN}{DayLow|DayHi|MonthHi|MonthLow|YearHi|YearLow|TimeDay...}`,
for example `ExtraTemp02DayHi`, `SoilMoist01DayLow`, `LeafWet04TimeDayHi`. `client.py`'s
`add_hilows_info()` currently surfaces only the Day Hi/Low values onto disabled-by-default
sensor entities in `sensor.py`; the full Month/Year decode lives in the hilows dict itself
and is reachable through `get_raw_data()`/diagnostics for advanced use (see
docs/backlog.md). Covered by synthetic-frame unit tests in `tests/test_protocol.py` and
confirmed against a real captured frame on 2026-09-04 (319 keys, plausible values
matching a simultaneous LOOP1 reading) once the read-timeout bug described above was
fixed.

## Archive records (DMPAFT)

`_get_archives_generator` sends `DMPAFT`, then the packed start date and time, expects
ACK, then a 6-byte header (page count, first record offset, CRC). Pages are 267 bytes:
an index byte, five 52-byte records, and a CRC; a bad page is NAKed for retransmission up
to three times. Download stops with ESC when a record's time is None or beyond the stop
date; ACK requests the next page. In each Rev B record, offset 10 is `Rainfall` (raw rain
clicks over the period) and offset 12 is `HighRainRate` (raw clicks/hour), named directly
per the manual with no separate scaled `RainRateHi` duplicate (an earlier version of this
integration produced both a raw `RainRate` override and a stale, differently-scaled
`RainRateHi` from the same bytes; see docs/decisions.md). Verified in code.
