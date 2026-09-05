# Davis Vantage Weather

[![GitHub Release](https://img.shields.io/github/v/release/trooperthorn/ha_int_davis?style=for-the-badge)](https://github.com/trooperthorn/ha_int_davis/releases)
[![GitHub Activity](https://img.shields.io/github/commit-activity/m/trooperthorn/ha_int_davis?style=for-the-badge)](https://github.com/trooperthorn/ha_int_davis/commits/main)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)

A feature-rich Home Assistant custom integration for Davis Vantage weather stations over a direct USB/serial connection, with advanced weather view support, LOOP2 protocol integration, and granular wind rose directions.

---

## Features

* **Native Weather Category:** Designed for seamless integration into Home Assistant weather cards.
* **LOOP2 Protocol Support:** Capability detection during setup with an explicit user option.
* **Granular Wind Rose:** Expanded directional reporting including **NNE, SSW, NNW**, etc.
* **Built-in Diagnostics & Testing:** Includes automatic connection pre-testing before adding serial devices and a dedicated debug connection option.
* **Comprehensive Actions:** Remotely set panel time, modify archive periods, update rain collectors, and pull raw diagnostic logs directly from Home Assistant.
* **Direct Serial Protocol:** Talks to the console directly over `serialx`, with no third-party Davis protocol library in the dependency chain (see [Console Communication](#console-communication)).
* **Accurate "No Data" Reporting:** Sensors report unavailable, not a fake reading, when the console has no data for a field (station disconnected, sensor not fitted, or not yet synced after a reboot).

---

## Supported Hardware

| Model | Compatible |
| :--- | :---: |
| Davis WeatherLink SER (6510SER) | Yes |
| Davis WeatherLink USB (6510USB) | Yes |
| Vantage Vue | Yes |
| Davis WeatherlinkIP (6555IP) | No [^3] |
| WeatherLink Live | No |
| Davis Weather Envoy8X (6318EU) | No |

---

## Prerequisites

Home Assistant 2026.8.3 or newer is required. Ensure your Davis console is running compatible firmware:

* **Davis Console 3.15 and newer** recommended for full feature support.

| Model | Min Version |
| :--- | :---: |
| Vantage Pro2 Console (Wired/Cabled) | 3.88 *(Tested and works on 3.15 via serial)* |
| Weather Envoy Wireless | 3.88 |
| Weather Envoy Cabled | 3.12 |

---

## Installation

### Via HACS (Recommended)
1. Open **HACS** in your Home Assistant instance.
2. Click **Integrations**, then click the three dots in the top right corner and select **Custom repositories**.
3. Add `https://github.com/trooperthorn/ha_int_davis` with category **Integration**.
4. Search for **Davis Vantage**, download, and restart Home Assistant.

---

## Setup & Configuration

The supported setup path is:

`Interface -> Verify Davis console/logger -> Davis options -> Complete`

* **Serial / USB:** Uses Home Assistant's native serial-port selector. Manual paths and serial URLs remain available for containers and remote serial bridges. Opening the form does not open a port; only the submitted interface is probed. On Linux, `/dev/serial/by-id` is preferred, with `/dev/serial/by-path` as a fallback. Windows `COM*` paths remain supported. Serial/USB is the only supported connection method; the earlier WeatherLink IP/network option has been removed (see [Console Communication](#console-communication)).
* **Verification:** Performs the Davis wake/ACK exchange, detects a supported serial baud, consumes and validates a LOOP2 response when available, and closes the temporary probe before continuing.
* **Davis options:** Owns polling interval, LOOP2 enablement, and persistent-connection behavior. Connection method, canonical endpoint, detected baud/capability, and device identity remain config-entry data.

Reconfigure uses the same selector and verifier. When a stable USB serial identity is available, reconfigure rejects a different physical logger or console. Existing runtime options are preserved.

> **Upgrading from a version with a WeatherLink network (IP) config entry?** Network support has been removed. Remove and re-add the integration on a serial/USB connection; there is no automatic migration.

### Connection lifecycle

Each config entry owns one serialized transport worker. Polling, wake-up, actions, archive reads, EEPROM access, and shutdown use that owner, so a coordinator timeout cannot release the port while the blocking operation is still running. Unload stops new work, drains in-flight work, and requires a controlled close result before a replacement client may be set up. A temporary disconnect marks updates unavailable; the next transaction reconstructs the transport for the same canonical endpoint.

---

## Console Communication

This integration talks to the console directly over [`serialx`](https://github.com/puddly/serialx); it does not depend on any third-party Davis protocol library. Earlier releases used a private fork of `pyvantagepro` (which itself required `pyserial`); that dependency has been removed entirely, along with the IP/network (WeatherLink raw-TCP) transport it required for its own reasons. Serial/USB is now the only connection path.

Every command in the Davis Serial Communication Reference Manual (Rev 2.6.1) that this integration uses — `LOOP`/`LPS` (LOOP2), `HILOWS`, `GETTIME`/`SETTIME`, `DMPAFT` archive downloads, `EEBRD`/`EEBWR`, `BAR=`, `TEST`, `WRD`, `RXTEST`, `RECEIVERS`, `CALED`/`CALFIX`, and more — is implemented and unit-tested against that manual, and the console/wind/temperature/rain/solar reporting path has been verified end to end against real Vantage Pro2 hardware, including a full disconnect-and-reconnect cycle. Two real discrepancies between the manual and actual console behavior were found and corrected in the process (documented in [`docs/decisions.md`](docs/decisions.md)); anywhere this integration's behavior had to diverge from the manual's literal text to match real hardware, that divergence is recorded there with the evidence.

A field the console has no data for (a sensor that isn't fitted, a station that hasn't synced since a reboot, or one that has lost contact) is reported as unavailable, not as a fake or out-of-range reading. A small number of items remain on extended deferral pending longer-term production testing and verification of the data (`SETPER`'s archive-clearing behavior against real hardware, and additional HILOWS Month/Year sensor entities); see [`docs/backlog.md`](docs/backlog.md) for what remains open and why. A guided calibration workflow for `CALED`/`CALFIX` was considered and rejected outright, not deferred — see [`docs/decisions.md`](docs/decisions.md) for why.

---

## Entities Created

### Weather & Environment
* **Barometric Pressure:** Current, Daily High/Low, High/Low Timestamps, and Trend (Stable, Rising/Falling Slowly/Rapidly).
* **Temperature & Humidity:** Outside Temperature, Inside Temperature, Feels Like, Heat Index, Wind Chill, Dew Point (with Daily Highs/Lows), Outside Humidity, and Extra Humidity/Temperature (Sensors 1–7).
* **Precipitation:** Current Rain Rate, Is Raining, Daily/Monthly/Yearly Rain totals, Rain Storm total, and Storm Start Date.
* **Solar & UV:** Solar Radiation, UV Level (with Daily Highs and Peaks).
* **Wind:** Current Wind Speed, 10-Minute Average, Archive Average, Wind Gust, Wind Direction (Degrees & Cardinal Rose), and Beaufort scale [^4] [^5].
* **Astronomical:** Sunrise and Sunset times, Forecast Icons, and Forecast Rules.

### Diagnostic Entities
* Archive Interval [^2], Battery Voltage, Console Elevation, Latitude, Longitude, Rain Collector Type, Last Error Message/Time, Last Fetch Time, and Last Success Time.

---

## Available Actions

* **`davis_vantage.set_davis_time`**: Synchronize the weather station's clock with Home Assistant.
* **`davis_vantage.get_davis_time`**: Retrieve the current clock reading from the console.
* **`davis_vantage.get_raw_data`**: Pull raw, unprocessed byte data from the most recent fetch cycle.
* **`davis_vantage.get_info`**: Fetch console firmware version and system diagnostics.
* **`davis_vantage.set_yearly_rain`**: Adjust yearly rainfall totals in calibration clicks.
* **`davis_vantage.set_yearly_et`**: Set yearly evapotranspiration in 100ths of an inch.
* **`davis_vantage.set_archive_period`**: Change archive logging intervals (1, 5, 10, 15, 30, 60, 120 mins). *Warning: This clears archived console memory.*
* **`davis_vantage.set_rain_collector`**: Configure tipping bucket collector size (`0.01"`, `0.2 mm`, or `0.1 mm`).
* **`davis_vantage.set_console_lamps`**: Turn the console's physical LCD backlight on or off.
* **`davis_vantage.clear_alarms`**: Silence sounding alarms and reset active hardware alarm bits.
* **`davis_vantage.set_barometer_calibration`**: Set station elevation and, optionally, a known-good local barometer reading.
* **`davis_vantage.run_test`**: Sends the `TEST` command and confirms the console echoes it back; a basic connection sanity check.
* **`davis_vantage.get_station_type`**: Reads the `WRD` station-type byte (Vantage Pro/Pro2 vs. Vantage Vue).
* **`davis_vantage.rxtest`**: Moves the console off its "Receiving From..." screen and clears the `RXCHECK` CRC-error count.
* **`davis_vantage.get_receivers`**: Reads the bitmap of transmitter station IDs the console can currently hear.
* **`davis_vantage.get_eeprom`** / **`davis_vantage.set_eeprom`**: Advanced/expert use. Read or write raw console EEPROM bytes; factory calibration fields and command-managed settings (`BAR=`, `SETPER`) are blocked on write.
* **`davis_vantage.get_calibrated_values`** / **`davis_vantage.set_calibrated_values`**: Advanced/expert use only, raw calibration data. See [Console Communication](#console-communication) and [`docs/decisions.md`](docs/decisions.md) before using `set_calibrated_values` — it is easy to get wrong.

When more than one Davis entry exists, pass its `entry_id` to an action so the request is routed to that entry's transport owner.

---

## Diagnostics and release qualification

Design rationale, console protocol facts, operations, and dated decisions live under
`docs/`; start at [`docs/README.md`](docs/README.md).

Downloaded diagnostics omit the connection endpoint, credentials, raw EEPROM contents, and raw packet bytes. They include connection state, detected baud, LOOP2 capability and selection, last success/failure, failure streak, reconnect count, and the result of the most recent close.

Automated release gates run against the exact declared Home Assistant version and include pytest, ruff, mypy, hassfest, and HACS validation. CI success is automated evidence only; it is not live console qualification.

Before calling a release hardware-qualified, validate LOOP1 and LOOP2 consoles, Home Assistant reboot, console power cycle, USB reassignment, port contention, and a 24-hour soak. Record the hardware/firmware, transport, Home Assistant version, and result for each run.

---

## Footnotes

[^1]: If values show as "Unknown", ensure the Davis console time is set correctly using the *Get/Set Davis Time* actions.
[^2]: Archive intervals can be modified via the *Set Archive Period* action.
[^3]: WeatherlinkIP relied on this integration's IP/network (WeatherLink raw-TCP) transport, which has been removed; see [Console Communication](#console-communication). A WeatherlinkIP logger connected via its own serial/USB path may still work, but that combination is untested.
[^4]: Wind direction entities report as `Unknown` if current wind speed is `0.0`.
[^5]: Mean calculation adjustments for wind direction may require clearing historical long-term statistics in Home Assistant database if migrating from older versions.
