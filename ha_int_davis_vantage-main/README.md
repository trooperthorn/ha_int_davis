# Davis Vantage Weather

[![GitHub Release](https://img.shields.io/github/v/release/trooperthorn/ha_int_elkm1?style=for-the-badge)](https://github.com/trooperthorn/ha_int_elkm1/releases)
[![GitHub Activity](https://img.shields.io/github/commit-activity/m/trooperthorn/ha_int_elkm1?style=for-the-badge)](https://github.com/trooperthorn/ha_int_elkm1/commits/main)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://github.com/hacs/integration)

A feature-rich Home Assistant custom integration for Davis Vantage weather stations, supporting both serial and network connections with advanced weather view support, LOOP2 protocol integration, and granular wind rose directions.

---

## Features

* **Native Weather Category:** Designed for seamless integration into Home Assistant weather cards.
* **LOOP2 Protocol Support:** Automatic selection for newer firmware models.
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

Ensure your Davis console is running compatible firmware:

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
3. Add `https://github.com/trooperthorn/ha_int_elkm1` with category **Integration**.
4. Search for **Davis Vantage**, download, and restart Home Assistant.

---

## Setup & Configuration

During setup, choose your connection method:

* **Serial / USB:** Select your device port. The integration automatically performs a pre-connection test to verify the Davis device responds before finishing configuration.
* **Network:** Provide the hostname or IP address and port number (typically port `22222`). 
  > *Tip: If unsure, browse to the IP address of your WeatherLink IP logger to verify the active port number on its configuration page.*

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
* **`davis_vantage.get_information`**: Fetch console firmware version and system diagnostics.
* **`davis_vantage.set_yearly_rain`**: Adjust yearly rainfall totals in calibration clicks.
* **`davis_vantage.set_archive_period`**: Change archive logging intervals (1, 5, 10, 15, 30, 60, 120 mins). *Warning: This clears archived console memory.*
* **`davis_vantage.set_rain_collector`**: Configure tipping bucket collector size (`0.01"`, `0.2 mm`, or `0.1 mm`).

---

## Footnotes

[^1]: If values show as "Unknown", ensure the Davis console time is set correctly using the *Get/Set Davis Time* actions.
[^2]: Archive intervals can be modified via the *Set Archive Period* action.
[^3]: Using WeatherLinkIP while simultaneously streaming data to WeatherLink.com may cause socket conflicts; disabling cloud forwarding is recommended for local polling stability.
[^4]: Wind direction entities report as `Unknown` if current wind speed is `0.0`.
[^5]: Mean calculation adjustments for wind direction may require clearing historical long-term statistics in Home Assistant database if migrating from older versions.
