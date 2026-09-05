# Design

Architecture and rationale for the Davis Vantage integration. Wire and console facts live
in `protocol.md`, configuration and troubleshooting in `operations.md`, dated decisions in
`decisions.md`.

## Entry lifecycle

`async_setup_entry` runs in a fixed order: read the connection configuration (protocol,
link, persistent connection), build the client (baud rate from the entry data, LOOP2 only
when the option is on and the entry data records that the console supports it), verify
connectivity with `connect_to_station` and `get_station_info`, create the device registry
entry, create the coordinator, run the first refresh, store the runtime data, forward the
sensor, binary_sensor, and weather platforms, register the options listener that reloads
the entry, and finally register the domain services. Services are registered once per
Home Assistant instance, and each handler resolves the entry it was asked about rather
than binding to whichever entry loaded last.

If the first refresh fails, the client is shut down and closed before
`ConfigEntryNotReady` is raised. Without that release, Home Assistant's automatic retry
would hit "Resource busy" on a serial port this entry never let go of.

On unload, when `async_unload_platforms` refuses, the entry stays loaded, so the shutdown
begun just before is cancelled with `async_cancel_shutdown`; otherwise the client would
reject every future poll for an entry Home Assistant still considers active. The same
undo happens when the close does not confirm. A successful unload deletes the
`connection_lost` repair issue so removing the integration never leaves a stale issue.

## Client and transport

One config entry owns one single-worker `ThreadPoolExecutor`. Cancelling a Home
Assistant waiter never cancels or releases the blocking Davis operation, and
`_async_run_io` shields the future for the same reason. `serialx.serial_for_url` returns
a configured but closed transport, so the link opens it explicitly; `verification.py`
does the same. Serial/USB is the only supported transport (see `decisions.md`).

`protocol.py` is self-contained: it implements the wake-up/send/ACK handshake, CRC-16,
every parser, and every documented command directly against the Davis manual, with no
third-party base class. `DavisProtocolClient` (`protocol.py`) is the wire-protocol layer;
`DavisVantageClient` (`client.py`) is the facade `coordinator.py`/`__init__.py`/
`services.py`/`diagnostics.py` actually call, and it owns the transport lifecycle
(reconnect, persistent-connection option, executor shutdown) on top of it. The client
keeps its `_protocol_client` attribute name generic rather than naming a specific vendor
library, since it now owns that logic directly rather than wrapping a third party's class.

`LoopData2Parser` (`protocol.py`) is its own class built from `LOOP2_FORMAT`, a tuple of
`(name, struct_code)` pairs read directly off the manual's LOOP2 offset table, not
derived from or subclassing the LOOP1 parser. A module-level self-check
(`_assert_loop2_offsets`) asserts the byte offset of several documented milestone fields
every time the module is imported, so a future edit that miscounts an "Unused" field's
size fails immediately at import time instead of silently shifting every field after it
(see `protocol.md` and `decisions.md` for the LOOP2 bug this specifically guards against).
LOOP2 carries no alarm bits, battery status, forecast icon, or sunrise and sunset, so
those keys are filled with None to keep downstream lookups from raising. The parser
format is selected by mode in `client.py`: parsing LOOP2 bytes with the LOOP1 layout
would silently misread them and corrupt the invalid-value masking that follows. The raw
bytes are attached as `_raw_bytes` for diagnostics.

In LOOP2 mode the archive fetch is skipped: the packet already carries a rolling
ten-minute gust and average, so the DMPAFT round trip that LOOP1 needs to derive them
would be redundant. The rain collector type rarely changes and costs an extra wake-up to
read, so it is fetched once and cached; the `set_rain_collector` service clears the cache.
`clear_cached_property` is safe when nothing was cached or no connection exists.

`add_additional_info` fills dew point, heat index, and wind chill only when they are
None, because LOOP2 already supplies console-computed values and LOOP1 supplies none.
`FeelsLike` prefers the console's THSW figure over the generic estimate when LOOP2
provides it. Wind direction accepts a compass string or a numeric bearing, normalizes the
rose to lowercase, and reports 0 and "n" for calm air with no direction. Battery status
and alarm bits come from `protocol.py`'s own LOOP1 parser, which calls
`apply_loop1_alarm_bits` directly against the validated raw frame (no intermediate
"alarm-safe" copy is needed now that the decoder is owned by this repository and already
uses the correct LSB-first bit order); in LOOP2 mode those keys are None and the
consumers already handle that. LOOP2 has no ten-minute average direction, only the gust
direction, so that is exposed as `WindGustDir` rather than mislabeled as an average.

`_read_exact` (`verification.py`) sleeps ten milliseconds on an empty read so a
non-blocking transport does not spin the loop at full CPU for the whole deadline.

## Coordinator

`CONNECTION_ISSUE_THRESHOLD` is three consecutive failed polls: high enough that one
serial hiccup, which this hardware produces routinely, does not nag, and low enough to
notice a real outage. The poll interval comes from `ConfigEntry.options` only, so runtime
tuning has one owner. Each poll runs under a fifteen-second timeout to cover wake-up
retries and parsing. The client catches its own errors and returns a dict with
`LastError` set instead of raising; without the explicit check an unreachable console
would count as a successful update forever, with stale entities, no repair issue, and
nothing to tell the user.

## Entities

Every platform sets `PARALLEL_UPDATES = 0` because entities only read the coordinator's
already-polled data. `_attr_has_entity_name = True` lets Home Assistant compose names
from the device name plus the translation key. Unique ids are built from the entry id;
the binary sensor platform migrates old ids that contained spaces or brackets to the
underscore form when no entity already holds the new id, and the weather entity's id
replaced an earlier MAC-based one.

`get_wind_rose` returns lowercase compass points so they match the state keys already in
`translations/en.json` and `icons.json`, which never matched while the function returned
uppercase. Sectors are 22.5 degrees shifted by 11.25 so north is centered on 0 and 360.

`SENSOR_TYPES` maps directly onto the Davis serial protocol field names through
`protocol.py`'s parser dictionary keys. Notable entries:

- `wind_speed_10_min_gust` is really the ten-minute average per the LOOP format; its
  name and translation say "average" but the key stays so entity ids and recorder history
  survive. `wind_gust` is the true gust, the high wind speed of the last archive interval.
- `wind_gust_direction` is LOOP2 only, the direction of the ten-minute gust, disabled by
  default. `wind_direction_rose` has no unit or state class because its state is text.
- `rain_rate` is `MEASUREMENT`; `rain_day`, `rain_month`, and `rain_year` are
  `TOTAL_INCREASING` so the Energy and Water dashboards accept them. `rain_storm` exists
  in both modes; `rain_15_min` is LOOP2 only and disabled by default. Both feed
  `blueprints/automation/flash_flood.yaml`.
- `et_day` (evapotranspiration, disabled by default) exists for irrigation integrations
  that expect it next to rainfall. `uv_index` has no device class because core does not
  define one. `THSWIndex` is Davis's own apparent temperature, LOOP2 only, disabled by
  default.
- `forecast_icon_condition` and `ForecastIcon` both carry the icon table; the first
  (lowercase condition strings) is disabled by default, the second (display strings) is
  enabled, and `forecast_icon_raw` exposes the code. The canonical table is in
  `weather.py`.
- Only `console_battery` is `EntityCategory.DIAGNOSTIC`, which hides it from the main
  dashboard and voice assistants.

`entity_registry_enabled_default` disables the optional solar and UV sensors when their
value at setup is None or the Davis "missing" value 255. `native_value` applies four
rules in order: the sentinel values 255, 32767, 32768, and -32768 mean lost sync or an
unplugged sensor; for wind direction, a sentinel, 0, or "N" returns the previously stored
value so calm air does not swing the graph, and a fresh boot returns None; a missing rain
rate becomes 0 rather than unknown; every other sensor becomes None. The missing-value
warning fires only on the transition into missing, ignores the expected-missing keys
(solar, UV, wind direction and rose, rain rate), and dumps the coordinator's data keys
when the outdoor temperature itself is missing, so a sensor that legitimately stays None
does not warn on every poll.

## Weather entity

The console offers a single "next roughly twelve hours" icon plus a canned text rule, no
numeric daily or hourly forecast. `FORECAST_TWICE_DAILY` with a condition-only entry is
the closest honest fit; anything more would be fabricated. Home Assistant converts the
native Fahrenheit, inHg, mph, and inch units for metric users. Barometer readings outside
20.000 to 32.500 inHg are discarded as bad reads, the same rule `_barometer_or_none`
applies in `sensor.py`. `native_wind_gust_speed` reads the archive-derived `WindGust`,
never the ten-minute average. `native_apparent_temperature` prefers THSW and falls back
to the heat index, which exists in both modes.

## Diagnostics

The diagnostics download surfaces the console's own RXCHECK report, NVER firmware
version, and BARDATA calibration, which the client had wired up but never exposed,
alongside the last polled data. `_try_console_command` degrades to an "unavailable"
string rather than raising, because diagnostics are downloaded precisely when the console
is unreachable and the rest of the payload is still useful then.

## Unverified

- `sensor.py` once claimed core "does not currently enforce a UV device class"; not
  re-checked against the current core release.
- `sensor.py` cites Davis serial protocol v2.61 while the rest of the code cites manual
  Rev 2.6.1; assumed to be the same document.
- `utils.convert_to_mm` multiplies by 20.0 with a note about a metric tipping bucket; the
  factor's derivation is unverified and the function is unused.
- The coordinator hard-codes the fifteen-second timeout although `const.py` defines
  `DEFAULT_IO_TIMEOUT = 15`.
