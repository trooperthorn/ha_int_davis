# Davis Vantage Weather

[![GitHub Release](https://img.shields.io/github/v/release/trooperthorn/ha_int_davis?style=for-the-badge)](https://github.com/trooperthorn/ha_int_davis/releases)
[![GitHub Activity](https://img.shields.io/github/commit-activity/m/trooperthorn/ha_int_davis?style=for-the-badge)](https://github.com/trooperthorn/ha_int_davis/commits/main)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)

A feature-rich Home Assistant custom integration for Davis Vantage weather stations, supporting both serial and network connections with advanced weather view support, LOOP2 protocol integration, and granular wind rose directions.

---

## Features

* **Native Weather Category:** Designed for seamless integration into Home Assistant weather cards.
* **LOOP2 Protocol Support:** Capability detection during setup with an explicit user option.
* **Granular Wind Rose:** Expanded directional reporting including **NNE, SSW, NNW**, etc.
* **Built-in Diagnostics & Testing:** Includes automatic connection pre-testing before adding serial devices and a dedicated debug connection option.
* **Comprehensive Actions:** Remotely set panel time, modify archive periods, update rain collectors, and pull raw diagnostic logs directly from Home Assistant.

---

## Supported Hardware

| Model | Compatible |
| :--- | :---: |
| Davis WeatherLink SER (6510SER) | Yes |
| Davis WeatherLink USB (6510USB) | Yes |
| Davis WeatherlinkIP (6555IP) | Yes [^3] |
| Vantage Vue | Yes |
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
| WeatherLinkIP Data Logger | 1.1.5 |

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

`Connection method -> Interface -> Verify Davis console/logger -> Davis options -> Complete`

* **Serial / USB:** Uses Home Assistant's native serial-port selector. Manual paths and serial URLs remain available for containers and remote serial bridges. Opening the form does not open a port; only the submitted interface is probed. On Linux, `/dev/serial/by-id` is preferred, with `/dev/serial/by-path` as a fallback. Windows `COM*` paths remain supported.
* **WeatherLink network:** Enter a hostname or IP address and optional port (default `22222`). IPv6 must use `[address]:port` form.
* **Verification:** Performs the Davis wake/ACK exchange, detects a supported serial baud, consumes and validates a LOOP2 response when available, and closes the temporary probe before continuing.
* **Davis options:** Owns polling interval, LOOP2 enablement, and persistent-connection behavior. Connection method, canonical endpoint, detected baud/capability, and device identity remain config-entry data.

Reconfigure uses the same selector and verifier. When a stable USB serial identity is available, reconfigure rejects a different physical logger or console. Existing runtime options are preserved.

### Connection lifecycle

Each config entry owns one serialized transport worker. Polling, wake-up, actions, archive reads, EEPROM access, and shutdown use that owner, so a coordinator timeout cannot release the port while the blocking operation is still running. Unload stops new work, drains in-flight work, and requires a controlled close result before a replacement client may be set up.

The persistent option applies to serial transports. WeatherLink TCP connections are deliberately released after each transaction even when the option is enabled so local polling does not indefinitely starve WeatherLink cloud uploads. A temporary disconnect marks updates unavailable; the next transaction reconstructs the transport for the same canonical endpoint.

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
* **`davis_vantage.set_archive_period`**: Change archive logging intervals (1, 5, 10, 15, 30, 60, 120 mins). *Warning: This clears archived console memory.*
* **`davis_vantage.set_rain_collector`**: Configure tipping bucket collector size (`0.01"`, `0.2 mm`, or `0.1 mm`).

When more than one Davis entry exists, pass its `entry_id` to an action so the request is routed to that entry's transport owner.

---

## Diagnostics and release qualification

Downloaded diagnostics omit the connection endpoint, credentials, raw EEPROM contents, and raw packet bytes. They include connection state, detected baud, LOOP2 capability and selection, last success/failure, failure streak, reconnect count, and the result of the most recent close.

Automated release gates run against the exact declared Home Assistant version and include pytest, ruff, mypy, hassfest, and HACS validation. CI success is automated evidence only; it is not live console qualification.

Before calling a release hardware-qualified, validate LOOP1 and LOOP2 consoles, Home Assistant reboot, console power cycle, USB reassignment, WeatherLink network loss, port contention, and a 24-hour soak. Record the hardware/firmware, transport, Home Assistant version, and result for each run.

---

## Footnotes

[^1]: If values show as "Unknown", ensure the Davis console time is set correctly using the *Get/Set Davis Time* actions.
[^2]: Archive intervals can be modified via the *Set Archive Period* action.
[^3]: WeatherLinkIP port 22222 is shared with logger activity. This integration releases the TCP connection after each transaction; unusually aggressive polling can still contend with cloud uploads.
[^4]: Wind direction entities report as `Unknown` if current wind speed is `0.0`.
[^5]: Mean calculation adjustments for wind direction may require clearing historical long-term statistics in Home Assistant database if migrating from older versions.
