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

The scanner flags `pyserial==3.5` under the serialx migration. The integration's own
transport already uses serialx, but the PyVantagePro fork still does `import serial`, and
`verification.py` lists ports through `serial.tools.list_ports`. A clean venv with only
serialx installed has no `serial` module, so dropping the pin would break both. Rejected:
removing the pin and relying on a shim that does not exist. Revisit when the fork stops
importing `serial`.

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
