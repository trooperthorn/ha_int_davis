"""Connection verification and endpoint identity helpers for Davis consoles."""

from __future__ import annotations

import contextlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .const import (
    DEFAULT_BAUD_RATE,
    IDENTITY_STRONG,
    IDENTITY_WEAK,
    PROTOCOL_SERIAL,
    SUPPORTED_BAUD_RATES,
)
from .protocol import VantageProCRC

VERIFY_TIMEOUT = 2.0


class DavisVerificationError(Exception):
    """Base error raised while verifying a submitted interface."""


class DavisCannotConnectError(DavisVerificationError):
    """The submitted interface could not be opened."""


class DavisNotFoundError(DavisVerificationError):
    """The submitted interface did not answer the Davis wake exchange."""


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Verified connection facts stored in config-entry data."""

    protocol: str
    endpoint: str
    identity: str
    identity_source: str
    identity_strength: str
    loop2_supported: bool
    baud_rate: int | None = None


class _ProbeTransport(Protocol):
    """Minimal transport surface required by the Davis verifier."""

    def write(self, data: bytes) -> Any:
        """Write bytes."""

    def read(self, size: int) -> bytes:
        """Read up to size bytes."""

    def close(self) -> Any:
        """Close the transport."""


def canonicalize_serial_endpoint(endpoint: str) -> str:
    """Return a stable serial path where the host exposes one."""
    endpoint = endpoint.strip()
    if not endpoint:
        raise DavisCannotConnectError("A serial endpoint is required")

    if "://" in endpoint:
        return endpoint
    if os.name == "nt":
        return endpoint.upper() if endpoint.lower().startswith("com") else endpoint

    selected = Path(endpoint)
    try:
        resolved = selected.resolve(strict=False)
    except OSError:
        return endpoint

    for directory in (Path("/dev/serial/by-id"), Path("/dev/serial/by-path")):
        try:
            candidates = sorted(directory.iterdir())
        except OSError:
            continue
        for candidate in candidates:
            try:
                if candidate.resolve(strict=True) == resolved:
                    return str(candidate)
            except OSError:
                continue
    return endpoint


def _serial_identity(endpoint: str) -> tuple[str, str, str]:
    """Return the strongest locally available serial/logger identity."""
    try:
        import serialx

        endpoint_resolved = Path(endpoint).resolve(strict=False) if "://" not in endpoint else None
        for port in serialx.list_serial_ports():
            matches = port.device == endpoint
            if not matches and endpoint_resolved is not None:
                with contextlib.suppress(OSError):
                    matches = Path(port.device).resolve(strict=False) == endpoint_resolved
            if not matches:
                continue
            serial_number = getattr(port, "serial_number", None)
            if serial_number:
                vid = getattr(port, "vid", None)
                pid = getattr(port, "pid", None)
                identity = f"usb:{vid or 0:04x}:{pid or 0:04x}:{serial_number}"
                return identity, "usb_serial_number", IDENTITY_STRONG
    except (ImportError, OSError):
        pass
    return f"serial:{endpoint}", "canonical_endpoint", IDENTITY_WEAK


def _read_exact(transport: _ProbeTransport, size: int, timeout: float) -> bytes:
    """Read exactly size bytes or return the partial result at timeout."""
    deadline = time.monotonic() + timeout
    chunks = bytearray()
    while len(chunks) < size and time.monotonic() < deadline:
        chunk = transport.read(size - len(chunks))
        if chunk:
            chunks.extend(chunk)
        else:
            # A non-blocking transport returns b"" immediately; sleep so the loop does not spin.
            time.sleep(0.01)
    return bytes(chunks)


def _wake(transport: _ProbeTransport) -> bool:
    """Perform the Davis wake exchange, including the documented retry."""
    for _ in range(3):
        transport.write(b"\n")
        if _read_exact(transport, 2, VERIFY_TIMEOUT) == b"\n\r":
            return True
    return False


def _probe_loop2(transport: _ProbeTransport) -> bool:
    """Request and fully consume one LOOP2 packet."""
    transport.write(b"LPS 2 1\n")
    if _read_exact(transport, 1, VERIFY_TIMEOUT) != b"\x06":
        return False
    packet = _read_exact(transport, 99, VERIFY_TIMEOUT * 2)
    return (
        len(packet) == 99
        and packet[:3] == b"LOO"
        and packet[4] == 1
        and packet[95:97] == b"\n\r"
        and VantageProCRC(packet).check()
    )


def verify_serial(endpoint: str) -> VerificationResult:
    """Open only the submitted serial endpoint and verify Davis behavior."""
    import serialx

    canonical = canonicalize_serial_endpoint(endpoint)
    opened = False
    last_error: Exception | None = None
    for baud_rate in SUPPORTED_BAUD_RATES:
        serial_port: _ProbeTransport | None = None
        try:
            serial_device = serialx.serial_for_url(
                canonical,
                baudrate=baud_rate,
                timeout=VERIFY_TIMEOUT,
                write_timeout=VERIFY_TIMEOUT,
            )
            # serialx constructs a closed transport; open it explicitly.
            serial_device.open()
            serial_port = serial_device
            opened = True
            if not _wake(serial_port):
                continue
            loop2_supported = _probe_loop2(serial_port)
            identity, source, strength = _serial_identity(canonical)
            return VerificationResult(
                protocol=PROTOCOL_SERIAL,
                endpoint=canonical,
                identity=identity,
                identity_source=source,
                identity_strength=strength,
                loop2_supported=loop2_supported,
                baud_rate=baud_rate,
            )
        except (OSError, TimeoutError, ValueError) as err:
            last_error = err
        finally:
            if serial_port is not None:
                with contextlib.suppress(OSError):
                    serial_port.close()

    if not opened and last_error is not None:
        raise DavisCannotConnectError(str(last_error)) from last_error
    raise DavisNotFoundError("No Davis console acknowledged the wake exchange")


def verify_connection(protocol: str, endpoint: str) -> VerificationResult:
    """Verify one submitted interface. Serial/USB is the only supported transport."""
    if protocol == PROTOCOL_SERIAL:
        return verify_serial(endpoint)
    raise DavisCannotConnectError(f"Unsupported connection method: {protocol}")


def default_verification_result(protocol: str, endpoint: str) -> VerificationResult:
    """Build migration-safe weak identity data without opening a transport."""
    canonical = canonicalize_serial_endpoint(endpoint)
    identity, source, strength = _serial_identity(canonical)
    return VerificationResult(
        protocol=protocol or PROTOCOL_SERIAL,
        endpoint=canonical,
        identity=identity,
        identity_source=source,
        identity_strength=strength,
        loop2_supported=False,
        baud_rate=DEFAULT_BAUD_RATE,
    )
