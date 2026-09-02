"""Connection verification and endpoint identity helpers for Davis consoles."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import socket
import time
from typing import Any, Protocol

from pyvantagepro.device import VantageProCRC

from .const import (
    DEFAULT_BAUD_RATE,
    IDENTITY_STRONG,
    IDENTITY_WEAK,
    PROTOCOL_NETWORK,
    PROTOCOL_SERIAL,
    SUPPORTED_BAUD_RATES,
)

DEFAULT_WEATHERLINK_PORT = 22222
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


class _SocketProbe:
    """Adapt a connected socket to the verifier transport surface."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def write(self, data: bytes) -> None:
        self._sock.sendall(data)

    def read(self, size: int) -> bytes:
        return self._sock.recv(size)

    def close(self) -> None:
        self._sock.close()


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


def split_network_endpoint(endpoint: str) -> tuple[str, int]:
    """Parse a WeatherLink host and port, including bracketed IPv6."""
    value = endpoint.strip()
    if not value:
        raise DavisCannotConnectError("A network endpoint is required")

    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            raise DavisCannotConnectError("Invalid bracketed IPv6 endpoint")
        host = value[1:closing]
        remainder = value[closing + 1 :]
        try:
            port = (
                int(remainder[1:])
                if remainder.startswith(":")
                else DEFAULT_WEATHERLINK_PORT
            )
        except ValueError as err:
            raise DavisCannotConnectError("Invalid WeatherLink port") from err
    else:
        host, separator, port_text = value.rpartition(":")
        if not separator:
            host = value
            port = DEFAULT_WEATHERLINK_PORT
        elif ":" in host:
            raise DavisCannotConnectError("IPv6 addresses must use [address]:port")
        else:
            try:
                port = int(port_text)
            except ValueError as err:
                raise DavisCannotConnectError("Invalid WeatherLink port") from err

    if not host or not 1 <= port <= 65535:
        raise DavisCannotConnectError("Invalid WeatherLink host or port")
    return host.lower(), port


def canonicalize_network_endpoint(endpoint: str) -> str:
    """Return canonical host:port form."""
    host, port = split_network_endpoint(endpoint)
    display_host = f"[{host}]" if ":" in host else host
    return f"{display_host}:{port}"


def _serial_identity(endpoint: str) -> tuple[str, str, str]:
    """Return the strongest locally available serial/logger identity."""
    try:
        from serial.tools.list_ports import comports

        endpoint_resolved = Path(endpoint).resolve(strict=False) if "://" not in endpoint else None
        for port in comports():
            matches = port.device == endpoint
            if not matches and endpoint_resolved is not None:
                try:
                    matches = Path(port.device).resolve(strict=False) == endpoint_resolved
                except OSError:
                    pass
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
    return bytes(chunks)


def _wake(transport: _ProbeTransport) -> bool:
    """Perform the Davis wake exchange, including the documented retry."""
    for _ in range(2):
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
    return len(packet) == 99 and VantageProCRC(packet).check()


def verify_serial(endpoint: str) -> VerificationResult:
    """Open only the submitted serial endpoint and verify Davis behavior."""
    import serialx

    canonical = canonicalize_serial_endpoint(endpoint)
    opened = False
    last_error: Exception | None = None
    for baud_rate in SUPPORTED_BAUD_RATES:
        serial_port: _ProbeTransport | None = None
        try:
            serial_port = serialx.serial_for_url(
                canonical,
                baudrate=baud_rate,
                timeout=VERIFY_TIMEOUT,
                write_timeout=VERIFY_TIMEOUT,
            )
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
                try:
                    serial_port.close()
                except OSError:
                    pass

    if not opened and last_error is not None:
        raise DavisCannotConnectError(str(last_error)) from last_error
    raise DavisNotFoundError("No Davis console acknowledged the wake exchange")


def verify_network(endpoint: str) -> VerificationResult:
    """Verify a submitted WeatherLink raw-TCP endpoint."""
    canonical = canonicalize_network_endpoint(endpoint)
    host, port = split_network_endpoint(canonical)
    try:
        sock = socket.create_connection((host, port), timeout=VERIFY_TIMEOUT)
        sock.settimeout(VERIFY_TIMEOUT)
    except (OSError, TimeoutError) as err:
        raise DavisCannotConnectError(str(err)) from err

    transport = _SocketProbe(sock)
    try:
        if not _wake(transport):
            raise DavisNotFoundError("The endpoint did not acknowledge the Davis wake exchange")
        loop2_supported = _probe_loop2(transport)
    finally:
        transport.close()

    return VerificationResult(
        protocol=PROTOCOL_NETWORK,
        endpoint=canonical,
        identity=f"network:{canonical}",
        identity_source="canonical_endpoint",
        identity_strength=IDENTITY_WEAK,
        loop2_supported=loop2_supported,
    )


def verify_connection(protocol: str, endpoint: str) -> VerificationResult:
    """Verify one submitted interface."""
    if protocol == PROTOCOL_SERIAL:
        return verify_serial(endpoint)
    if protocol == PROTOCOL_NETWORK:
        return verify_network(endpoint)
    raise DavisCannotConnectError(f"Unsupported connection method: {protocol}")


def default_verification_result(protocol: str, endpoint: str) -> VerificationResult:
    """Build migration-safe weak identity data without opening a transport."""
    if protocol == PROTOCOL_SERIAL:
        canonical = canonicalize_serial_endpoint(endpoint)
        identity, source, strength = _serial_identity(canonical)
        return VerificationResult(
            protocol=protocol,
            endpoint=canonical,
            identity=identity,
            identity_source=source,
            identity_strength=strength,
            loop2_supported=False,
            baud_rate=DEFAULT_BAUD_RATE,
        )
    canonical = canonicalize_network_endpoint(endpoint)
    return VerificationResult(
        protocol=protocol,
        endpoint=canonical,
        identity=f"network:{canonical}",
        identity_source="canonical_endpoint",
        identity_strength=IDENTITY_WEAK,
        loop2_supported=False,
    )

