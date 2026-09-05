"""Wire-level tests for the Davis Rev. 2.6.1 protocol contract.

The LOOP2 and HILOWS regression tests below build their synthetic frames
directly from the offsets documented in
`~/repos/vendor-docs-reference/docs/davis-vantage.md`, independently of
`protocol.py`'s own LOOP2_FORMAT/HILOWS_FORMAT tables, so they cannot simply
re-encode a bug the parser itself has.
"""

import struct
from datetime import datetime

import pytest

from custom_components.davis_vantage.protocol import (
    DavisBadCRCError,
    DavisBadDataError,
    DavisProtocolClient,
    HighLowParserRevB,
    LoopData2Parser,
    VantageProCRC,
    unpack_humidity,
    unpack_rain_rate,
    unpack_solar_rad,
    unpack_storm_date,
    unpack_temp,
    unpack_uv,
    unpack_wind_dir,
    unpack_wind_speed,
    validate_eeprom_write,
    validate_loop_frame,
)


class FakeLink:
    """Minimal link with deterministic reads and captured writes."""

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
    with pytest.raises(DavisBadDataError):
        validate_loop_frame(bytes(wrong_type), 1)

    bad_crc = bytearray(packet)
    bad_crc[-1] ^= 1
    with pytest.raises(DavisBadCRCError):
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


def test_unpack_uv_treats_255_as_no_sensor() -> None:
    """Confirmed against real hardware on 2026-09-04: a station without a UV
    sensor reported raw byte 255, which the parser divided into a fake 25.5
    UV Index reading instead of treating it as "no sensor" like the manual's
    other 255-dash fields.
    """
    assert unpack_uv(255) is None
    assert unpack_uv(32) == 3.2


def test_loop1_uv_dash_sentinel_becomes_none() -> None:
    packet = loop_packet(0)
    packet[43] = 255
    packet = bytearray(with_crc(bytes(packet[:-2])))
    link = FakeLink(["\n\r", "\x06", bytes(packet)])

    parsed = make_client(link).get_current_data()

    assert parsed["UV"] is None


def test_unpack_temp_treats_32767_as_no_data() -> None:
    assert unpack_temp(32767) is None
    assert unpack_temp(905) == 90.5


def test_unpack_humidity_treats_255_as_no_data() -> None:
    assert unpack_humidity(255) is None
    assert unpack_humidity(60) == 60


def test_unpack_wind_speed_treats_255_as_no_data() -> None:
    """The manual says a dashed wind speed is "forced to 0", but real hardware
    with no ISS data at all (2026-09-04) reported raw 255 instead; unavailable
    is safer than guessing which is true."""
    assert unpack_wind_speed(255) is None
    assert unpack_wind_speed(12) == 12


def test_unpack_wind_dir_treats_0_and_32767_as_no_data() -> None:
    """0 is the manual's own documented "no data" code (north is 360, not 0);
    32767 is an additional sentinel confirmed on real hardware, 2026-09-04."""
    assert unpack_wind_dir(0) is None
    assert unpack_wind_dir(32767) is None
    assert unpack_wind_dir(180) == 180


def test_unpack_solar_rad_treats_32767_as_no_data() -> None:
    assert unpack_solar_rad(32767) is None
    assert unpack_solar_rad(450) == 450


def test_unpack_rain_rate_treats_65535_as_no_data() -> None:
    assert unpack_rain_rate(65535) is None
    assert unpack_rain_rate(0) == 0


def test_unpack_storm_date_rejects_the_all_ones_sentinel() -> None:
    """Confirmed against real hardware with no ISS data, 2026-09-04: the
    0xFFFF sentinel decodes to month=15, day=31, both nonzero, so a
    zero-only validity check does not catch it."""
    assert unpack_storm_date(0xFFFF) is None
    assert unpack_storm_date(0) is None
    # month=9, day=4, year=2026: (9<<12)|(4<<7)|26
    assert unpack_storm_date((9 << 12) | (4 << 7) | 26) == "2026-09-04"


def test_loop1_reports_none_for_every_sentinel_with_no_iss_data() -> None:
    """Golden scenario: a console rebooted and awaiting first ISS packet
    reports the manual's dash values for every field that depends on the
    remote station, confirmed against real hardware on 2026-09-04. None of
    these should surface as a fake reading.
    """
    packet = loop_packet(0)
    struct.pack_into("<h", packet, 9, 32767)  # TempIn
    packet[11] = 255  # HumIn
    struct.pack_into("<h", packet, 12, 32767)  # TempOut
    packet[14] = 255  # WindSpeed
    packet[15] = 255  # WindSpeed10Min
    struct.pack_into("<H", packet, 16, 32767)  # WindDir
    packet[43] = 255  # UV
    struct.pack_into("<H", packet, 44, 32767)  # SolarRad
    struct.pack_into("<H", packet, 41, 65535)  # RainRate
    struct.pack_into("<H", packet, 48, 0xFFFF)  # StormStartDate
    packet = bytearray(with_crc(bytes(packet[:-2])))
    link = FakeLink(["\n\r", "\x06", bytes(packet)])

    parsed = make_client(link).get_current_data()

    for key in (
        "TempIn", "HumIn", "TempOut", "WindSpeed", "WindSpeed10Min",
        "WindDir", "UV", "SolarRad", "RainRate", "StormStartDate",
    ):
        assert parsed[key] is None, f"{key} should be None, got {parsed[key]!r}"


def test_gettime_rejects_corrupt_crc() -> None:
    invalid = bytes((0, 27, 15, 4, 6, 103, 0, 0))
    link = FakeLink(["\n\r", "\x06", invalid])
    with pytest.raises(DavisBadCRCError):
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
    assert result[0]["HighRainRate"] == 11
    assert "RainRateHi" not in result[0]
    # One ACK begins the download; there is no second ACK after its last page.
    assert link.writes.count("\x06") == 1
    assert link.writes[-1] == "\x1b"


def build_verified_loop2_frame(**field_values: int) -> bytes:
    """Build a 99-byte LOOP2 frame at the offsets confirmed against a real
    Vantage Pro2 (firmware 3.88) console on 2026-09-04 (see docs/decisions.md).

    The Davis manual's own LOOP2 table describes three 2-byte "Unused" fields
    between the 10-minute gust direction and DewPoint; this real console only
    sends two, placing DewPoint two bytes earlier than the manual states.
    These offsets were confirmed by decoding a live capture both ways and
    matching the result against a simultaneous, independently-correct LOOP1
    reading (DewPoint/HumOut/HeatIndex/WindChill/RainRate all landed on
    plausible values only under this two-unused-field layout).
    """
    frame = bytearray(97)
    frame[0:3] = b"LOO"
    frame[3] = field_values.get("BarTrend", 0)
    frame[4] = 1  # Packet Type: LOOP2
    struct.pack_into("<H", frame, 5, 0x7FFF)  # Unused
    struct.pack_into("<H", frame, 7, field_values.get("Barometer", 29910))
    struct.pack_into("<h", frame, 9, field_values.get("TempIn", 725))
    frame[11] = field_values.get("HumIn", 45)
    struct.pack_into("<h", frame, 12, field_values.get("TempOut", 825))
    frame[14] = field_values.get("WindSpeed", 5)
    frame[15] = 0xFF  # Unused
    struct.pack_into("<H", frame, 16, field_values.get("WindDir", 180))
    struct.pack_into("<H", frame, 18, field_values.get("WindSpeed10Min", 50))
    struct.pack_into("<H", frame, 20, field_values.get("WindSpeed2Min", 60))
    struct.pack_into("<H", frame, 22, field_values.get("WindGust10Min", 100))
    struct.pack_into("<H", frame, 24, field_values.get("WindGustDir10Min", 200))
    struct.pack_into("<H", frame, 26, 0x7FFF)  # Unused
    struct.pack_into("<H", frame, 28, 0x7FFF)  # Unused
    struct.pack_into("<h", frame, 30, field_values.get("DewPoint", 68))
    frame[32] = 0xFF  # Unused
    frame[33] = field_values.get("HumOut", 55)
    frame[34] = 0xFF  # Unused
    struct.pack_into("<h", frame, 35, field_values.get("HeatIndex", 90))
    struct.pack_into("<h", frame, 37, field_values.get("WindChill", 60))
    struct.pack_into("<h", frame, 39, field_values.get("THSWIndex", 92))
    struct.pack_into("<H", frame, 41, field_values.get("RainRate", 256))
    frame[43] = field_values.get("UV", 30)
    struct.pack_into("<H", frame, 44, field_values.get("SolarRad", 500))
    struct.pack_into("<H", frame, 46, field_values.get("RainStorm", 0))
    struct.pack_into("<H", frame, 48, field_values.get("StormStartDate", 0))
    struct.pack_into("<H", frame, 50, field_values.get("RainDay", 500))
    struct.pack_into("<H", frame, 52, field_values.get("RainLast15Min", 10))
    struct.pack_into("<H", frame, 54, field_values.get("RainLastHour", 20))
    struct.pack_into("<H", frame, 56, field_values.get("ETDay", 25))
    struct.pack_into("<H", frame, 58, field_values.get("RainLast24Hr", 30))
    frame[60] = field_values.get("BarReductionMethod", 2)
    struct.pack_into("<h", frame, 61, field_values.get("UserBarOffset", 0))
    struct.pack_into("<h", frame, 63, field_values.get("BarCalNumber", 0))
    struct.pack_into("<H", frame, 65, field_values.get("BarSensorRaw", 29910))
    struct.pack_into("<H", frame, 67, field_values.get("BarAbsolute", 29910))
    struct.pack_into("<H", frame, 69, field_values.get("AltimeterSetting", 29910))
    frame[71:81] = bytes(10)  # graph pointers, not exercised here
    frame[81:95] = b"\x7f\xff" * 7  # seven 2-byte Unused fields, 81 through 93
    frame[95:97] = b"\n\r"
    return bytes(with_crc(bytes(frame)))


def test_loop2_offsets_match_the_verified_layout_dewpoint_onward() -> None:
    """DewPoint and later fields must land at the hardware-confirmed offsets
    (30, 35, 37, 39, 41...), not the manual's literal (and wrong) 32/37/39/41."""
    raw = build_verified_loop2_frame(
        DewPoint=68, HeatIndex=91, WindChill=45, THSWIndex=95,
        HumOut=55, RainRate=256,
    )

    parsed = LoopData2Parser(raw)

    assert parsed["DewPoint"] == 68
    assert parsed["HeatIndex"] == 91
    assert parsed["WindChill"] == 45
    assert parsed["THSWIndex"] == 95
    assert parsed["HumOut"] == 55
    assert parsed["RainRate"] == 256  # raw click count; scaling happens in client.py


def test_loop2_dash_sentinel_255_still_becomes_none_at_correct_offset() -> None:
    raw = build_verified_loop2_frame(DewPoint=255, HeatIndex=255)
    parsed = LoopData2Parser(raw)
    assert parsed["DewPoint"] is None
    assert parsed["HeatIndex"] is None


def test_loop2_decodes_a_live_hardware_capture() -> None:
    """Golden-frame test: a real 99-byte LOOP2 response captured on COM3 from
    a Vantage Pro2 (firmware 3.88, station type 16) on 2026-09-04, at the
    same moment a LOOP1 read confirmed Barometer=29.789, TempOut=90.4,
    HumOut=60, ETDay=0.142. Locks in the hardware-verified offsets against
    real bytes, not just a hand-built synthetic frame.
    """
    raw = bytes.fromhex(
        "4c 4f 4f 00 01 ff 7f 5d 74 15 03 30 88 03 04 ff 4c 00 1f 00 26 00 "
        "09 00 5a 00 ff 7f ff 7f 4b 00 ff 3c ff 65 00 5a 00 67 00 00 00 ff "
        "89 00 00 00 ff ff 00 00 00 00 00 00 8e 00 00 00 02 00 00 ab ff 4e "
        "72 4e 72 80 74 ff 03 15 07 05 12 13 04 2b 02 0a 0a ff 7f ff 7f ff "
        "7f ff 7f ff 7f ff 7f 0a 0d dd 18"
    )
    assert VantageProCRC(raw).check()
    parsed = LoopData2Parser(raw)
    assert parsed["Barometer"] == 29.789
    assert parsed["TempOut"] == 90.4
    assert parsed["HumOut"] == 60
    assert parsed["DewPoint"] == 75
    assert parsed["HeatIndex"] == 101
    assert parsed["WindChill"] == 90
    assert parsed["THSWIndex"] == 103
    assert parsed["RainRate"] == 0
    assert parsed["BarReductionMethod"] == 2


def test_get_current_data_loop2_reads_and_validates_a_full_frame() -> None:
    raw = build_verified_loop2_frame()
    link = FakeLink(["\n\r", "\x06", raw])
    parsed = make_client(link).get_current_data_loop2()
    assert parsed["PacketType"] == 1
    assert link.writes == ["\n", "LPS 2 1\n"]


def test_test_command_echoes_back() -> None:
    link = FakeLink(["\n\r", "TEST\n\r"])
    assert make_client(link).test() is True
    assert link.writes == ["\n", "TEST\n"]


def test_get_station_type_reads_ack_then_one_byte() -> None:
    link = FakeLink(["\n\r", "\x06", bytes((17,))])
    assert make_client(link).get_station_type() == 17
    assert link.writes == ["\n", "WRD\x12\x4D\n"]


def test_rxtest_wakes_and_expects_the_ok_text_response() -> None:
    """Confirmed against real hardware on 2026-09-04: RXTEST replies with the
    standard OK text response, not a raw ACK byte."""
    link = FakeLink(["\n\r", "\n\rOK\n\r"])
    make_client(link).rxtest()
    assert link.writes == ["\n", "RXTEST\n"]


def test_get_receivers_reads_ok_then_bitmap_byte() -> None:
    link = FakeLink(["\n\r", "\n\rOK\n\r", bytes((0b00000101,))])
    assert make_client(link).get_receivers() == 0b00000101
    assert link.writes == ["\n", "RECEIVERS\n"]


def test_get_calibrated_values_reads_43_bytes_plus_crc() -> None:
    payload = bytes(range(43))
    link = FakeLink(["\n\r", "\x06", with_crc(payload)])
    assert make_client(link).get_calibrated_values() == payload
    assert link.writes == ["\n", "CALED\n"]


def test_set_calibrated_values_requires_exactly_43_bytes() -> None:
    link = FakeLink([])
    with pytest.raises(ValueError, match="43 bytes"):
        make_client(link).set_calibrated_values(bytes(10))


def test_set_calibrated_values_sends_payload_with_crc() -> None:
    payload = bytes(range(43))
    link = FakeLink(["\n\r", "\x06", "\x06"])
    make_client(link).set_calibrated_values(payload)
    assert link.writes[0] == "\n"
    assert link.writes[1] == "CALFIX\n"
    assert link.writes[2] == with_crc(payload)


def test_set_yearly_et_wakes_and_uses_putet_command() -> None:
    link = FakeLink(["\n\r", "\x06"])
    make_client(link).set_yearly_et(2483)
    assert link.writes == ["\n", "PUTET 2483\n"]


def build_manual_hilows_frame() -> bytes:
    """Build a 436-byte HILOWS payload directly from the manual's offset
    table (independent of HighLowParserRevB.HILOWS_FORMAT)."""
    frame = bytearray(436)

    # Extra/Soil/Leaf temps block: offset 126, size 150.
    # Day Low(126,15) / Day Hi(141,15) / ... indices 0-6=Extra2-8, 7-10=Soil1-4, 11-14=Leaf1-4.
    day_low = list(range(90, 105))  # raw bytes; -90 offset applied by the parser
    day_hi = list(range(100, 115))
    frame[126:141] = bytes(day_low)
    frame[141:156] = bytes(day_hi)

    # Outside/Extra humidities block: offset 276, size 80. index0=outside.
    hum_day_low = list(range(30, 38))
    hum_day_hi = list(range(40, 48))
    frame[276:284] = bytes(hum_day_low)
    frame[284:292] = bytes(hum_day_hi)

    # Soil moisture block: offset 356, size 40. Order: DayHi/TimeDayHi/DayLow/...
    soil_moist_day_hi = list(range(10, 14))
    soil_moist_day_low = list(range(20, 24))
    frame[356:360] = bytes(soil_moist_day_hi)
    frame[368:372] = bytes(soil_moist_day_low)

    # Leaf wetness block: offset 396, size 40. Same structure as soil moisture.
    leaf_wet_day_hi = list(range(1, 5))
    leaf_wet_day_low = list(range(5, 9))
    frame[396:400] = bytes(leaf_wet_day_hi)
    frame[408:412] = bytes(leaf_wet_day_low)

    return bytes(with_crc(bytes(frame)))


def test_hilows_extended_decode_extra_soil_leaf_temps() -> None:
    raw = build_manual_hilows_frame()
    parsed = HighLowParserRevB(raw, datetime.now())

    # index 0 of the 15-entry sub-array is Extra Temp sensor 2 (day_low[0]=90 -> 90-90=0).
    assert parsed["ExtraTemp02DayLow"] == 0
    assert parsed["ExtraTemp08DayLow"] == 6  # index 6 -> day_low[6] = 96 -> 96-90=6
    # index 7 is Soil Temp sensor 1.
    assert parsed["SoilTemp01DayLow"] == 7  # day_low[7]=97 -> 97-90=7
    assert parsed["SoilTemp04DayLow"] == 10  # day_low[10]=100 -> 10
    # index 11 is Leaf Temp sensor 1.
    assert parsed["LeafTemp01DayLow"] == 11  # day_low[11]=101 -> 11
    assert parsed["LeafTemp04DayLow"] == 14  # day_low[14]=104 -> 14

    assert parsed["ExtraTemp02DayHi"] == 10  # day_hi[0]=100 -> 100-90=10


def test_hilows_extended_decode_outside_and_extra_humidity() -> None:
    raw = build_manual_hilows_frame()
    parsed = HighLowParserRevB(raw, datetime.now())

    assert parsed["OutHumDayLow"] == 30  # index 0
    assert parsed["ExtraHum02DayLow"] == 31  # index 1
    assert parsed["ExtraHum08DayLow"] == 37  # index 7
    assert parsed["OutHumDayHi"] == 40
    assert parsed["ExtraHum02DayHi"] == 41


def test_hilows_extended_decode_soil_moisture_and_leaf_wetness() -> None:
    raw = build_manual_hilows_frame()
    parsed = HighLowParserRevB(raw, datetime.now())

    assert parsed["SoilMoist01DayHi"] == 10
    assert parsed["SoilMoist04DayHi"] == 13
    assert parsed["SoilMoist01DayLow"] == 20
    assert parsed["LeafWet01DayHi"] == 1
    assert parsed["LeafWet04DayHi"] == 4
    assert parsed["LeafWet01DayLow"] == 5


def test_get_hilows_reads_438_bytes_and_returns_extended_parser() -> None:
    raw = build_manual_hilows_frame()
    link = FakeLink(["\n\r", "\x06", raw])
    parsed = make_client(link).get_hilows()
    assert "SoilMoist01DayHi" in parsed
    assert link.writes == ["\n", "HILOWS\n"]
