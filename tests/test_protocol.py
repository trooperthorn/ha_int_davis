"""Wire-level tests for the Davis Rev. 2.6.1 protocol contract."""

import struct
from datetime import datetime

import pytest
from pyvantagepro.device import BadCRCException, BadDataException
from pyvantagepro.parser import VantageProCRC

from custom_components.davis_vantage.protocol import (
    DavisProtocolClient,
    validate_eeprom_write,
    validate_loop_frame,
)


class FakeLink:
    """Minimal PyVantagePro link with deterministic reads and captured writes."""

    def __init__(self, reads: list[str | bytes]) -> None:
        self.reads = list(reads)
        self.writes: list[str | bytes] = []

    def open(self) -> None:
        pass

    def write(self, data: str | bytes) -> None:
        self.writes.append(data)

    def read(
        self,
        _size: int | None = None,
        timeout: float | None = None,
        binary: bool = False,
    ) -> str | bytes:
        del timeout, binary
        return self.reads.pop(0)


def make_client(link: FakeLink) -> DavisProtocolClient:
    client = object.__new__(DavisProtocolClient)
    client.link = link
    client.RevA = False
    client.RevB = True
    return client


def with_crc(payload: bytes) -> bytes:
    return VantageProCRC(payload).data_with_checksum


def loop_packet(packet_type: int = 0) -> bytearray:
    payload = bytearray(97)
    payload[:3] = b"LOO"
    payload[4] = packet_type
    payload[95:97] = b"\n\r"
    return bytearray(with_crc(bytes(payload)))


def test_crc_matches_manufacturer_reference_vector() -> None:
    payload = bytes.fromhex("C6 CE A2 03")
    assert VantageProCRC(payload).checksum == 0xE2B4
    assert VantageProCRC(payload + bytes.fromhex("E2 B4")).check()


def test_loop_validation_checks_crc_signature_type_and_terminator() -> None:
    packet = bytes(loop_packet(1))
    validate_loop_frame(packet, 1)

    wrong_type = bytearray(packet)
    wrong_type[4] = 0
    wrong_type = bytearray(with_crc(bytes(wrong_type[:-2])))
    with pytest.raises(BadDataException):
        validate_loop_frame(bytes(wrong_type), 1)

    bad_crc = bytearray(packet)
    bad_crc[-1] ^= 1
    with pytest.raises(BadCRCException):
        validate_loop_frame(bytes(bad_crc), 1)


def test_loop1_alarm_bits_are_lsb_first_and_all_soil_bits_are_distinct() -> None:
    packet = loop_packet(0)
    packet[71] = (1 << 1) | (1 << 3)
    packet[73] = 1 << 0
    packet[74] = (1 << 2) | (1 << 3)
    packet[75] = (1 << 0) | (1 << 3)
    packet[82] = 0b10101010
    packet = bytearray(with_crc(bytes(packet[:-2])))
    link = FakeLink(["\n\r", "\x06", bytes(packet)])

    parsed = make_client(link).get_current_data()

    assert parsed["AlarmRain15min"] == 1
    assert parsed["AlarmRainStormTotal"] == 1
    assert parsed["AlarmOutHighTHSW"] == 1
    assert parsed["AlarmOutLowHum"] == 1
    assert parsed["AlarmOutHighHum"] == 1
    assert parsed["AlarmEx01LowTemp"] == 1
    assert parsed["AlarmEx01HighTemp"] == 0
    assert parsed["AlarmEx01HighHum"] == 1
    assert parsed["Alarm01LowLeafWet"] == 0
    assert parsed["Alarm01HighLeafWet"] == 1
    assert parsed["Alarm01LowSoilMois"] == 0
    assert parsed["Alarm01HighSoilMois"] == 1
    assert parsed["Alarm01LowLeafTemp"] == 0
    assert parsed["Alarm01HighLeafTemp"] == 1
    assert parsed["Alarm01LowSoilTemp"] == 0
    assert parsed["Alarm01HighSoilTemp"] == 1


def test_gettime_rejects_corrupt_crc() -> None:
    invalid = bytes((0, 27, 15, 4, 6, 103, 0, 0))
    link = FakeLink(["\n\r", "\x06", invalid])
    with pytest.raises(BadCRCException):
        make_client(link).gettime()


def test_eeprom_read_formats_count_as_hex_and_validates_crc() -> None:
    data = bytes(range(16))
    link = FakeLink(["\n\r", "\x06", with_crc(data)])

    assert make_client(link).read_from_eeprom("32", 16) == data
    assert link.writes == ["\n", "EEBRD 32 10\n"]


def test_eeprom_write_has_one_lf_and_crc_payload() -> None:
    data = bytes(range(16))
    link = FakeLink(["\n\r", "\x06", "\x06"])

    make_client(link).write_to_eeprom("32", len(data), data)

    assert link.writes[0] == "\n"
    assert link.writes[1] == "EEBWR 32 10\n"
    assert link.writes[2] == with_crc(data)


@pytest.mark.parametrize("address", ["01", "05", "07", "0F", "2D"])
def test_eeprom_protected_and_command_managed_ranges_are_blocked(address: str) -> None:
    with pytest.raises(ValueError, match="protected range"):
        validate_eeprom_write(address, 1)


def test_eeprom_spanning_write_cannot_cross_into_protected_range() -> None:
    with pytest.raises(ValueError, match="protected range"):
        validate_eeprom_write("2C", 2)


def test_setper_requires_ack_and_uses_decimal_period() -> None:
    link = FakeLink(["\n\r", "\x06"])
    make_client(link).set_archive_period(15)
    assert link.writes == ["\n", "SETPER 15\n"]


def test_rain_collector_wakes_and_uses_documented_read_modify_write() -> None:
    current = with_crc(bytes((0xC3,)))
    link = FakeLink(
        [
            "\n\r",  # explicit operation wake
            "\n\r",  # EEPROM read wake
            "\x06",
            current,
            "\n\r",  # EEPROM write wake
            "\x06",
            "\x06",
            "\n\r",  # NEWSETUP wake
            "\x06",
        ]
    )

    make_client(link).set_rain_collector(0x10)

    assert "EEBRD 2B 01\n" in link.writes
    assert "EEBWR 2B 01\n" in link.writes
    written = next(value for value in link.writes if isinstance(value, bytes))
    assert written[:-2] == bytes((0xD3,))
    assert link.writes[-1] == "NEWSETUP\n"


def archive_record(when: datetime, rainfall: int, rain_rate: int) -> bytes:
    record = bytearray(52)
    date_stamp = when.day + when.month * 32 + (when.year - 2000) * 512
    time_stamp = when.hour * 100 + when.minute
    struct.pack_into("<HH", record, 0, date_stamp, time_stamp)
    struct.pack_into("<HH", record, 10, rainfall, rain_rate)
    return bytes(record)


def test_dmpaft_honors_first_record_and_does_not_ack_after_final_page() -> None:
    start = datetime(2026, 9, 1, 12, 0)
    record = archive_record(datetime(2026, 9, 1, 12, 5), 7, 11)
    records = b"\xff" * 104 + record + b"\xff" * 104
    page = with_crc(b"\x00" + records + b"\x00" * 4)
    header = with_crc(struct.pack("<HH", 1, 2))
    link = FakeLink(["\n\r", "\x06", "\x06", header, page])
    client = make_client(link)
    client.__dict__["archive_period"] = 5

    result = list(
        client._get_archives_generator(
            start, datetime(2026, 9, 1, 12, 10)
        )
    )

    assert len(result) == 1
    assert result[0]["Rainfall"] == 7
    assert result[0]["RainRate"] == 11
    # One ACK begins the download; there is no second ACK after its last page.
    assert link.writes.count("\x06") == 1
    assert link.writes[-1] == "\x1b"
