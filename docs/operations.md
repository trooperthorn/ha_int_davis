# Operations

Configuration knobs, troubleshooting, the test gate, and the release path.

## Runtime options

Runtime tuning (`interval`, `use_loop2`, `persistent_connection`) lives in
`ConfigEntry.options`; `async_migrate_entry` moves those keys out of legacy `data` for
version 2 entries. LOOP2 mode is used only when the option is on and the entry data
records `loop2_supported` as true. On the network protocol the link is closed after every
transaction regardless of the persistent-connection option, because WeatherLink IP must
periodically release TCP port 22222 for cloud uploads.

Each poll is limited to fifteen seconds. Three consecutive failed polls raise the
`connection_lost` repair issue (non-fixable, non-persistent, WARNING severity, with the
entry title as placeholder); the first successful poll afterward clears it. A sustained
outage usually means the console is powered off or the USB device came back as a
different `/dev/ttyUSBx` after a reboot; the fix is to reconfigure the integration with
the right port or link.

The rain collector type is cached after the first poll; change it with the
`set_rain_collector` service, which invalidates the cache. The `set_eeprom` service is
for expert use only: consult the manual's EEPROM table before writing, because the
console offers no protection (see `protocol.md`).

Diagnostics dumps redact station latitude and longitude and every EEPROM key, because
users attach those dumps to public issues.

## Blueprints

- `close_windows_when_raining.yaml`: the condition entity list (living room window,
  bedroom window, garage door) is a placeholder for the user's own window, door, and
  cover entities; the automation fires only when at least one is open. Uncomment the
  `cover.close_cover` action to auto-close motorized covers instead of, or as well as,
  notifying. Only covers support close; a plain binary sensor cannot be closed.
- `console_alert.yaml`: turns on the console backlight through
  `davis_vantage.set_console_lamps` when the barometric trend enters `falling_rapidly`.
- `high_wind_protection.yaml`: the commented-out `cover.close_cover` on
  `cover.patio_awning` is an optional hardware action for gusts above 40 mph.
- `flash_flood.yaml`: the threshold of 0.75 inches in fifteen minutes is roughly a
  3.0 in/hr rate; the `rain_15_min` sensor is LOOP2 only and disabled by default.
- `solar_underperformance.yaml`: the sun-elevation condition (above 40 degrees) excludes
  dawn and dusk, where low radiation is normal; the weather-state condition (sunny or
  partly cloudy) exists because low radiation under cloud is weather, not a fault.

Installs set up before the entity translations were fixed may still carry the generic
entity id `binary_sensor.davis_vantage_connectivity` rather than
`binary_sensor.davis_vantage_outdoor_sensor_suite_status`, which the console connection
blueprint description names.

## Test gate

The gate is the same on a workstation and in CI: `ruff check custom_components/davis_vantage
tests scripts`, `mypy --python-version 3.14 custom_components/davis_vantage/`,
`pytest tests/ -v`, then `python scripts/build_release_artifacts.py --validate-only`.
Pins live in `requirements_test.txt`; the harness pin (0.13.363) itself pins core
2026.9.0, and the CI job asserts the installed core and serialx versions. The harness
imports `fcntl`, so on Windows the suite runs under WSL. `pyserial` stays in the manifest
because the PyVantagePro fork still imports `serial` and serialx does not provide that
module (see `decisions.md`).

## Release path

A merge to `main` is the only release path. Nobody edits the manifest version or pushes a
tag by hand.

1. `Release` runs on every push to `main`. It calls the Test and Validate workflows, then
   reads the version from `custom_components/davis_vantage/manifest.json` through
   `.release.json`. If a published release for that version already exists it stops.
   Otherwise it builds the deterministic archive, generates the SPDX SBOM and checksums,
   attests both, creates the `v<version>` tag on the exact commit, drafts the release with
   every asset, and publishes it. HACS installs the tagged tree; the archive is the same
   tree, built deterministically so the attestations have a fixed subject.
2. `Prepare release` runs after every successful `Release` on `main`. When the manifest
   version equals the latest published release and `custom_components/davis_vantage`
   changed since that tag, it runs `scripts/set_version.py --next-from-tags`, pushes the
   bump to `automation/calver-release` with a GitHub App token, opens a PR, and arms
   squash auto-merge. The merge triggers `Release` again. Docs, tests, and workflow
   changes do not bump the version.
3. Without the GitHub App credentials the second step fails at its credential check and
   nothing else happens. The repository still releases: run
   `python -m scripts.set_version --next-from-tags` on a branch, open the PR, and the
   merge publishes.

`.release.json` is the single statement of what ships. `scripts/set_version.py` is the
only writer of the version field; `scripts/release_config.py` behind
`build_release_artifacts.py --validate-only` is the independent reader. Versions are
`YYYY.MM.DD.N` in `America/Chicago`. Tags cut before 2026-09-04 include one without a
sequence (`v2026.09.02`); the reader accepts that form and never writes it.

### GitHub App for zero-touch version PRs

`Prepare release` needs the release GitHub App (Contents: Read and write, Pull requests:
Read and write) installed on this repository, plus the repository variable
`RELEASE_AUTOMATION_CLIENT_ID` and the Actions secret `RELEASE_AUTOMATION_PRIVATE_KEY`.
The same App serves every trooperthorn Home Assistant repository; each holds its own
copy of the variable and secret.

### Branch and protection settings

`main` is the only long-lived branch. Work happens on short-lived branches that end in a
squash-merged PR and are deleted on merge. Branch protection requires the job display
names `pytest (Python 3.14)`, `HACS validation`, `hassfest (manifest sanity)`,
`CodeQL (python)`, and `Python static security checks`, with strict up-to-date checks,
enforced for administrators, no force pushes, no deletions, and no required approvals.

## Line endings

`.gitattributes` pins every text file to LF so Windows checkouts and WSL or Linux tools
see the same bytes.
