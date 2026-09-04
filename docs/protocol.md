# Protocol and console facts

Wire-level and console behavior the code depends on, from the Davis Vantage Serial
Communication Reference Manual Rev 2.6.1 unless a row says otherwise. Each table row says
whether the fact is verified in code, verified against the manual section cited in the
original code, or carried over unverified.

## Sentinel values

| Field type | Sentinel | Meaning | Status |
| --- | --- | --- | --- |
| one-byte fields | 255 | dashed or invalid: lost sync, unplugged, or sensor not fitted | verified in `is_incorrect_value` |
| unsigned two-byte fields | 32767, 65535 | dashed or invalid | verified in code |
| signed two-byte fields | 32767, -32768 | dashed or invalid | verified in code |
| wind direction | 0 or 32767 | calm air or lost sync | verified in code |
| forecast icon | 255, 32767, -32768 | missing | verified in code |

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
signature, terminator, and CRC as LOOP1. `LoopData2Parser.LOOP2_FORMAT` is little-endian:
LOO (3s), BarTrend (B), PacketType (B), 2 unused, Barometer (H, /1000 inHg), TempIn (h,
/10 F), HumIn (B), TempOut (h, /10 F), WindSpeed (B, mph), 1 unused, WindDir (H),
WindSpeed10Min (H, /10), WindSpeed2Min (H, /10), WindGust10Min (H, /10), WindGustDir10Min
(H), 4 unused, DewPoint (h), 1 unused, HumOut (B), 1 unused, HeatIndex (h), WindChill (h),
THSWIndex (h), RainRate (H, /100 in/hr), UV (B, /10), SolarRad (H), RainStorm (H, /100),
StormStartDate (H), RainDay (H, /100), RainLast15Min (H, /100), RainLastHour (H, /100),
ETDay (H, /1000), RainLast24Hr (H, /100), BarReductionMethod (B), UserBarOffset (h),
BarCalNumber (h), BarSensorRaw (H), BarAbsolute (H), AltimeterSetting (H), 2 unused,
GraphPointers (10s), 12 unused, EOL (2s), CRC (H). Verified in `client.py`.

DewPoint, HeatIndex, WindChill, and THSWIndex read 255 when missing. LOOP2 carries
console-computed dew point, heat index, wind chill, and THSW (Davis's apparent
temperature), a rolling ten-minute gust and average, the direction of the ten-minute gust
but no ten-minute average direction, RainLast15Min, and ETDay. It carries no alarm bits,
battery status, forecast icon, or sunrise and sunset.

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
30, 60, and 120 minutes), `GETTIME` (8 bytes with CRC), `HILOWS` (438 bytes with CRC),
and `DMPAFT`. A response starting with 0x21 is a NAK. Valid baud rates, fastest first,
are 19200, 14400, 9600, 4800, 2400, and 1200; the default is 19200, 8N1. WeatherLink IP
consoles listen on TCP port 22222, and that port must periodically be released for the
console's cloud uploads. Verified in code.

## Archive records (DMPAFT)

`_get_archives_generator` sends `DMPAFT`, then the packed start date and time, expects
ACK, then a 6-byte header (page count, first record offset, CRC). Pages are 267 bytes:
an index byte, five 52-byte records, and a CRC; a bad page is NAKed for retransmission up
to three times. Download stops with ESC when a record's time is None or beyond the stop
date; ACK requests the next page. In each record, bytes 10 and 11 are the rainfall click
count and bytes 12 and 13 the rain rate; both are kept as raw click semantics because the
upstream parser misnames offset 10 as `RainRate`. Verified in code.
