# Documentation index

One line per document stating what it owns, so a reader knows where a fact belongs. Code
carries only what a reader needs at the point of reading; explanation lives here.

- `design.md`: architecture and rationale (entry lifecycle, client and executor model,
  LOOP2 parser, coordinator failure handling, entity mapping choices, the weather entity,
  diagnostics), with an Unverified section.
- `protocol.md`: console and wire facts (sentinel values, LOOP1 alarm bytes, the LOOP2
  layout, forecast icons, barometer limits and calibration, EEPROM ranges, commands and
  baud rates, archive records), each row marked verified or unverified.
- `operations.md`: runtime options and troubleshooting, blueprint options, the test gate,
  the release path and its GitHub App, branch protection, and line endings.
- `decisions.md`: dated decisions with the alternative rejected and why.
- `backlog.md`: residual gaps left after a change deliberately scoped down, with the
  reasoning for the scoping choice.
