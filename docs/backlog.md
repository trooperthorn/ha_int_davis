# Backlog

Residual gaps left after a change deliberately scoped down. Not a general TODO list; only
entries with a scoping decision behind them belong here.

## Extended deferral (2026-09-04)

Both entries below are on extended deferral, not a near-term revisit: Sean wants long-term
production running time and verification of the data this rewrite produces before either
is picked back up. Neither blocks the pyvantagepro-removal change from shipping.

## HILOWS Month/Year Hi/Lo sensor entities

`protocol.py`'s `HighLowParserRevB` fully decodes the extra-temperature, soil-temperature,
leaf-temperature, outside/extra-humidity, soil-moisture, and leaf-wetness Day/Month/Year
Hi/Lo sub-arrays into the hilows dict (see `docs/decisions.md`, 2026-09-04). `client.py`'s
`add_hilows_info()` currently surfaces only the Day Hi/Low values onto disabled-by-default
sensor entities in `sensor.py`. The Month and Year Hi/Lo values (and every "time of" field
for all three ranges) are present in the decoded hilows dict and reachable through
`get_raw_data()`/diagnostics, but do not yet have their own sensor entities.

This was a deliberate scoping choice, not an oversight: adding Day/Month/Year Hi/Lo
entities for all 7 extra-temperature, 4 soil-temperature, 4 leaf-temperature, 7
extra-humidity, 4 soil-moisture, and 4 leaf-wetness sensors would add roughly 200
additional disabled-by-default entities on top of the ~50 added for the Day-only case,
for a sensor family most installs do not have the hardware to populate at all. Revisit if
a user with the relevant probes (extra temperature/humidity stations, soil/leaf sensors)
asks for Month or Year values specifically.

## `SETPER` (set_archive_period) not tested against real hardware

Deliberately held back from the 2026-09-04 hardware session: `SETPER` auto-clears archive
memory per the manual, and Sean chose not to risk his console's archive data to confirm a
command whose wire format was already correct in the removed `pyvantagepro` fork (this
rewrite's `protocol.py` override of `set_archive_period` predates this session and matches
the manual's documented `<ACK>` response, unlike the fork's own buggy wait-on-`OK`).
Decision on 2026-09-04: extended deferral, pending long-term production running time and
verification of the data, not a fixed several-week timeline.
