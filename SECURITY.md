# Security Policy

## Reporting a vulnerability

Do not open a public issue containing exploit details, credentials, private
addresses, or logs. Use GitHub's private vulnerability-reporting feature for
this repository. If private reporting is unavailable, open a minimal issue
asking the maintainer to establish a private channel; omit technical details.

Include the affected version/commit, prerequisites, impact, a minimal
reproduction, and suggested remediation. Remove serial-bridge addresses and
private network details.

## Response targets

These are project targets, not an SLA: acknowledge critical/high reports in
three business days, establish severity and containment in seven, and publish
a coordinated fix as soon as safely validated. Lower-severity issues are
prioritized by exploitability and impact.

## Supported version

Only the latest published release and the default branch receive security
fixes. Operators should update Home Assistant and this integration promptly
and retain a tested rollback/backup.

## Security boundaries

This integration talks to a Davis Vantage console over RS-232, directly or
through a serial-to-network bridge. The console protocol has no
authentication: anyone who can reach the serial port or the bridge can read
the weather data and change console settings, the same authority this
integration has. The integration does not harden the port, the bridge, or the
Home Assistant host; it validates what it parses and keeps the connection
settings in the config entry, which Home Assistant stores unencrypted on
disk.
