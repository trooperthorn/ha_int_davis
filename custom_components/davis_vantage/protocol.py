"""Davis Vantage wire-protocol corrections and validation.

The upstream client remains pinned as a transport/parser dependency.  This
module owns the stricter behavior required by the Davis Serial Communication
Reference Manual Rev. 2.6.1.
"""

from __future__ import annotations

import struct
import sys
from datetime import datetime, timedelta
from typing import Any

from pyvantagepro import VantagePro2
from pyvantagepro.device import BadAckException, BadCRCException, BadDataException
from pyvantagepro.parser import (
    ArchiveDataParserRevB,
    HighLowParserRevB,
    LoopDataParserRevB,
    VantageProCRC,
    pack_dmp_date_time,
    unpack_datetime,
)

VALID_ARCHIVE_PERIODS = frozenset({1, 5, 10, 15, 30, 60, 120})
EEPROM_SIZE = 0x1000

# Factory calibration or command-managed fields; see docs/protocol.md.
_PROTECTED_EEPROM_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x01, 0x04, "factory barometer calibration"),
    (0x05, 0x06, "BAR= managed barometer calibration"),
    (0x07, 0x0A, "factory humidity calibration"),
    (0x0F, 0x10, "BAR= managed elevation"),
    (0x2D, 0x2D, "SETPER managed archive period"),
)


def _bytes(value: str | bytes) -> bytes:
    """Normalize the legacy link's text-or-bytes return type."""
    return value.encode("latin-1") if isinstance(value, str) else value


def _bit(value: int, number: int) -> int:
    """Return a Davis bit by its least-significant-bit-first number."""
    return int(bool(value & (1 << number)))


def validate_eeprom_range(address_hex: str, size: int) -> tuple[int, int]:
    """Validate and return an inclusive EEPROM address range."""
    try:
        start = int(address_hex, 16)
    except ValueError as err:
        raise ValueError("EEPROM address must be hexadecimal") from err
    if size < 1:
        raise ValueError("EEPROM transfer size must be at least one byte")
    end = start + size - 1
    if start < 0 or end >= EEPROM_SIZE:
        raise ValueError("EEPROM transfer exceeds the 4 KiB address space")
    return start, end


def validate_eeprom_write(address_hex: str, size: int) -> None:
    """Reject writes to manufacturer-protected or command-managed fields."""
    start, end = validate_eeprom_range(address_hex, size)
    for protected_start, protected_end, reason in _PROTECTED_EEPROM_RANGES:
        if start <= protected_end and end >= protected_start:
            raise ValueError(
                f"EEPROM write overlaps protected range "
                f"0x{protected_start:02X}-0x{protected_end:02X}: {reason}"
            )


def validate_crc_frame(data: bytes, expected_size: int, label: str) -> None:
    """Reject short, long, or CRC-invalid binary responses."""
    if len(data) != expected_size:
        raise BadDataException(
            f"Expected {expected_size}-byte {label}, got {len(data)} bytes"
        )
    if not VantageProCRC(data).check():
        raise BadCRCException(f"Invalid CRC in {label}")


def validate_loop_frame(data: bytes, packet_type: int) -> None:
    """Validate the common LOOP/LOOP2 envelope and CRC."""
    validate_crc_frame(data, 99, f"LOOP{packet_type + 1} packet")
    if data[:3] != b"LOO" or data[4] != packet_type or data[95:97] != b"\n\r":
        raise BadDataException("Invalid LOOP packet signature, type, or terminator")


def apply_loop1_alarm_bits(data: Any, raw: bytes) -> None:
    """Decode LOOP1 alarm bytes using Davis LSB-first bit numbering."""
    mappings: tuple[tuple[int, tuple[str, ...]], ...] = (
        (
            70,
            (
                "AlarmInFallBarTrend",
                "AlarmInRisBarTrend",
                "AlarmInLowTemp",
                "AlarmInHighTemp",
                "AlarmInLowHum",
                "AlarmInHighHum",
                "AlarmInTime",
            ),
        ),
        (
            71,
            (
                "AlarmRainHighRate",
                "AlarmRain15min",
                "AlarmRain24hour",
                "AlarmRainStormTotal",
                "AlarmRainETDaily",
            ),
        ),
        (
            72,
            (
                "AlarmOutLowTemp",
                "AlarmOutHighTemp",
                "AlarmOutWindSpeed",
                "AlarmOut10minAvgSpeed",
                "AlarmOutLowDewpoint",
                "AlarmOutHighDewPoint",
                "AlarmOutHighHeat",
                "AlarmOutLowWindChill",
            ),
        ),
        (
            73,
            (
                "AlarmOutHighTHSW",
                "AlarmOutHighSolarRad",
                "AlarmOutHighUV",
                "AlarmOutUVDose",
                "AlarmOutUVDoseEnabled",
            ),
        ),
    )
    for offset, names in mappings:
        for number, name in enumerate(names):
            data[name] = _bit(raw[offset], number)

    data["AlarmOutLowHum"] = _bit(raw[74], 2)
    data["AlarmOutHighHum"] = _bit(raw[74], 3)

    for sensor in range(1, 8):
        value = raw[74 + sensor]
        for number, suffix in enumerate(
            ("LowTemp", "HighTemp", "LowHum", "HighHum")
        ):
            data[f"AlarmEx{sensor:02d}{suffix}"] = _bit(value, number)

    soil_leaf_suffixes = (
        "LowLeafWet",
        "HighLeafWet",
        "LowSoilMois",
        "HighSoilMois",
        "LowLeafTemp",
        "HighLeafTemp",
        "LowSoilTemp",
        "HighSoilTemp",
    )
    for sensor in range(1, 5):
        value = raw[81 + sensor]
        for number, suffix in enumerate(soil_leaf_suffixes):
            data[f"Alarm{sensor:02d}{suffix}"] = _bit(value, number)


def _alarm_safe_loop1_frame(raw: bytes) -> bytes:
    """Return a parser-safe copy with the alarm bytes (70-85) zeroed.

    The upstream alarm decoder raises TypeError on Python 3 and uses reversed bit order.
    """
    parser_frame = bytearray(raw)
    parser_frame[70:86] = bytes(16)
    return bytes(parser_frame)


class DavisProtocolClient(VantagePro2):
    """VantagePro2 client with the manufacturer wire contract enforced."""

    def __init__(self, link: Any) -> None:
        if sys.byteorder != "little":
            raise RuntimeError("Davis binary parsing requires a little-endian host")
        super().__init__(link)

    def _read_binary(self, size: int, *, timeout: float | None = None) -> bytes:
        return _bytes(self.link.read(size, timeout=timeout, binary=True))

    def get_current_data(self) -> Any:
        """Read one validated LOOP1 packet."""
        self.wake_up()
        self.send("LOOP 1", self.ACK)
        raw = self._read_binary(99)
        validate_loop_frame(raw, 0)
        if not self.RevB:
            raise NotImplementedError("Rev A LOOP packets are not supported")
        parsed = LoopDataParserRevB(_alarm_safe_loop1_frame(raw), datetime.now())
        apply_loop1_alarm_bits(parsed, raw)
        return parsed

    def get_hilows(self) -> Any:
        """Read one validated HILOWS response."""
        self.wake_up()
        self.send("HILOWS", self.ACK)
        raw = self._read_binary(438)
        validate_crc_frame(raw, 438, "HILOWS packet")
        if not self.RevB:
            raise NotImplementedError("Rev A HILOWS packets are not supported")
        return HighLowParserRevB(raw, datetime.now())

    def gettime(self) -> datetime:
        """Read console time only when its CRC is valid."""
        self.wake_up()
        self.send("GETTIME", self.ACK)
        raw = self._read_binary(8)
        validate_crc_frame(raw, 8, "GETTIME response")
        return unpack_datetime(raw)

    def read_from_eeprom(self, hex_address: str, size: int) -> bytes:
        """Read EEPROM using hexadecimal address and count parameters."""
        validate_eeprom_range(hex_address, size)
        self.wake_up()
        self.send(f"EEBRD {hex_address.upper()} {size:02X}", self.ACK)
        raw = self._read_binary(size + 2)
        validate_crc_frame(raw, size + 2, "EEPROM response")
        return raw[:-2]

    def write_to_eeprom(self, hex_address: str, size: int, data: bytes) -> None:
        """Write EEPROM with one LF terminator and a validated safe range."""
        if len(data) != size:
            raise ValueError("EEPROM byte count does not match the payload length")
        validate_eeprom_write(hex_address, size)
        self.wake_up()
        self.send(f"EEBWR {hex_address.upper()} {size:02X}", self.ACK)
        self.send(VantageProCRC(data).data_with_checksum, self.ACK)

    def set_archive_period(self, archive_period: int) -> None:
        """Set the archive period and require the documented ACK response."""
        period = int(archive_period)
        if period not in VALID_ARCHIVE_PERIODS:
            raise ValueError(f"Unsupported Davis archive period: {period}")
        self.wake_up()
        self.send(f"SETPER {period}", self.ACK)

    def set_rain_collector(self, collector_type: int) -> None:
        """Set rain collector bits through the documented read-modify-write."""
        if collector_type not in (0x00, 0x10, 0x20):
            raise ValueError("Unsupported Davis rain collector type")
        self.wake_up()
        setup_bits = struct.unpack("<B", self.read_from_eeprom("2B", 1))[0]
        updated_bits = (setup_bits & 0xCF) | collector_type
        self.write_to_eeprom("2B", 1, struct.pack("<B", updated_bits))
        self.newsetup()

    def _read_archive_page(self, expected_index: int) -> bytes:
        """Read a DMPAFT page, requesting retransmission on bad data."""
        for attempt in range(3):
            raw = self._read_binary(267)
            valid = (
                len(raw) == 267
                and raw[0] == expected_index % 256
                and VantageProCRC(raw).check()
            )
            if valid:
                return raw
            if attempt < 2:
                self.link.write(self.NACK)
        raise BadCRCException("DMPAFT page failed validation after 3 attempts")

    def _get_archives_generator(
        self, start_date: datetime | None = None, stop_date: datetime | None = None
    ) -> Any:
        """Download DMPAFT pages without acknowledging beyond the final page."""
        self.wake_up()
        start = start_date or datetime(2001, 1, 1, 1, 1, 1)
        stop = stop_date or datetime.now()
        period = self.archive_period
        start -= timedelta(minutes=start.minute % period)

        self.send("DMPAFT", self.ACK)
        self.link.write(pack_dmp_date_time(start))
        if _bytes(self.link.read(1, timeout=2, binary=True)) != b"\x06":
            raise BadAckException()

        header = self._read_binary(6)
        if len(header) != 6 or not VantageProCRC(header).check():
            self.link.write(self.CANCEL)
            raise BadCRCException("Invalid DMPAFT header")
        pages, first_record, _crc = struct.unpack("<HHH", header)
        if first_record > 4:
            self.link.write(self.CANCEL)
            raise BadDataException("Invalid first-record offset in DMPAFT header")
        if pages == 0:
            self.link.write(self.ESC)
            return

        self.link.write(self.ACK)
        for page_number in range(pages):
            try:
                raw_page = self._read_archive_page(page_number)
            except (BadCRCException, BadDataException):
                self.link.write(self.ESC)
                raise

            stop_download = False
            first_slot = first_record if page_number == 0 else 0
            for slot in range(first_slot, 5):
                raw_record = raw_page[1 + slot * 52 : 1 + (slot + 1) * 52]
                record = ArchiveDataParserRevB(raw_record)
                record_time = record["Datetime"]
                if record_time is None or record_time > stop:
                    stop_download = True
                    break
                if record_time > start:
                    # Raw click counts; the upstream parser misnames offset 10 as RainRate.
                    record["Rainfall"] = int.from_bytes(
                        raw_record[10:12], "little", signed=False
                    )
                    record["RainRate"] = int.from_bytes(
                        raw_record[12:14], "little", signed=False
                    )
                    yield record

            if stop_download:
                self.link.write(self.ESC)
                break
            if page_number < pages - 1:
                self.link.write(self.ACK)
