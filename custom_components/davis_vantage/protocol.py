"""Davis Vantage serial wire protocol, implemented directly against serialx.

Implements the Davis Vantage Pro/Pro2/Vue Serial Communication Reference
Manual, Rev. 2.6.1 (see `~/repos/vendor-docs-reference/docs/davis-vantage.md`).
`DavisProtocolClient` owns the wake-up/send/ACK handshake and every documented
command; the `*Parser` classes turn raw binary frames into named-field dicts.
See docs/decisions.md and docs/protocol.md for the verified/unverified
callouts on individual fields and commands.
"""

from __future__ import annotations

import functools
import struct
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

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


class DavisProtocolError(Exception):
    """Base class for Davis wire-protocol errors."""


class DavisNoDeviceError(DavisProtocolError):
    """The console did not answer the wake-up exchange after all retries."""


class DavisBadAckError(DavisProtocolError):
    """The console's acknowledgement did not match what was expected."""


class DavisBadCRCError(DavisProtocolError):
    """A binary block failed its CRC-16 check."""


class DavisBadDataError(DavisProtocolError):
    """A binary block was malformed (wrong size, bad signature, etc.)."""


class retry:
    """Retries a function until it returns a truthy value or raises.

    `delay` sets the initial delay in seconds between attempts.
    """

    def __init__(self, tries: int = 3, delay: float = 1) -> None:
        self.tries = tries
        self.delay = delay

    def __call__(self, f: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped_f(*args: Any, **kwargs: Any) -> Any:
            for i in range(self.tries):
                try:
                    ret = f(*args, **kwargs)
                    if ret:
                        return ret
                    if i == self.tries - 1:
                        return ret
                except Exception:
                    if i == self.tries - 1:
                        raise
                if self.delay > 0:
                    time.sleep(self.delay)
            return None

        wrapped_f.__doc__ = f.__doc__
        wrapped_f.__name__ = f.__name__
        wrapped_f.__module__ = f.__module__
        return wrapped_f


class VantageProCRC:
    """CRC-CCITT (poly 0x1021), table-driven, MSB-first.

    A block (data + its trailing 2-byte CRC, MSB first) passes the check
    when accumulating all of it through the table yields zero.
    """

    CRC_TABLE: tuple[int, ...] = (
        0x0, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5, 0x60C6, 0x70E7,
        0x8108, 0x9129, 0xA14A, 0xB16B, 0xC18C, 0xD1AD, 0xE1CE, 0xF1EF,
        0x1231, 0x0210, 0x3273, 0x2252, 0x52B5, 0x4294, 0x72F7, 0x62D6,
        0x9339, 0x8318, 0xB37B, 0xA35A, 0xD3BD, 0xC39C, 0xF3FF, 0xE3DE,
        0x2462, 0x3443, 0x0420, 0x1401, 0x64E6, 0x74C7, 0x44A4, 0x5485,
        0xA56A, 0xB54B, 0x8528, 0x9509, 0xE5EE, 0xF5CF, 0xC5AC, 0xD58D,
        0x3653, 0x2672, 0x1611, 0x0630, 0x76D7, 0x66F6, 0x5695, 0x46B4,
        0xB75B, 0xA77A, 0x9719, 0x8738, 0xF7DF, 0xE7FE, 0xD79D, 0xC7BC,
        0x48C4, 0x58E5, 0x6886, 0x78A7, 0x0840, 0x1861, 0x2802, 0x3823,
        0xC9CC, 0xD9ED, 0xE98E, 0xF9AF, 0x8948, 0x9969, 0xA90A, 0xB92B,
        0x5AF5, 0x4AD4, 0x7AB7, 0x6A96, 0x1A71, 0x0A50, 0x3A33, 0x2A12,
        0xDBFD, 0xCBDC, 0xFBBF, 0xEB9E, 0x9B79, 0x8B58, 0xBB3B, 0xAB1A,
        0x6CA6, 0x7C87, 0x4CE4, 0x5CC5, 0x2C22, 0x3C03, 0x0C60, 0x1C41,
        0xEDAE, 0xFD8F, 0xCDEC, 0xDDCD, 0xAD2A, 0xBD0B, 0x8D68, 0x9D49,
        0x7E97, 0x6EB6, 0x5ED5, 0x4EF4, 0x3E13, 0x2E32, 0x1E51, 0x0E70,
        0xFF9F, 0xEFBE, 0xDFDD, 0xCFFC, 0xBF1B, 0xAF3A, 0x9F59, 0x8F78,
        0x9188, 0x81A9, 0xB1CA, 0xA1EB, 0xD10C, 0xC12D, 0xF14E, 0xE16F,
        0x1080, 0x00A1, 0x30C2, 0x20E3, 0x5004, 0x4025, 0x7046, 0x6067,
        0x83B9, 0x9398, 0xA3FB, 0xB3DA, 0xC33D, 0xD31C, 0xE37F, 0xF35E,
        0x02B1, 0x1290, 0x22F3, 0x32D2, 0x4235, 0x5214, 0x6277, 0x7256,
        0xB5EA, 0xA5CB, 0x95A8, 0x8589, 0xF56E, 0xE54F, 0xD52C, 0xC50D,
        0x34E2, 0x24C3, 0x14A0, 0x0481, 0x7466, 0x6447, 0x5424, 0x4405,
        0xA7DB, 0xB7FA, 0x8799, 0x97B8, 0xE75F, 0xF77E, 0xC71D, 0xD73C,
        0x26D3, 0x36F2, 0x0691, 0x16B0, 0x6657, 0x7676, 0x4615, 0x5634,
        0xD94C, 0xC96D, 0xF90E, 0xE92F, 0x99C8, 0x89E9, 0xB98A, 0xA9AB,
        0x5844, 0x4865, 0x7806, 0x6827, 0x18C0, 0x08E1, 0x3882, 0x28A3,
        0xCB7D, 0xDB5C, 0xEB3F, 0xFB1E, 0x8BF9, 0x9BD8, 0xABBB, 0xBB9A,
        0x4A75, 0x5A54, 0x6A37, 0x7A16, 0x0AF1, 0x1AD0, 0x2AB3, 0x3A92,
        0xFD2E, 0xED0F, 0xDD6C, 0xCD4D, 0xBDAA, 0xAD8B, 0x9DE8, 0x8DC9,
        0x7C26, 0x6C07, 0x5C64, 0x4C45, 0x3CA2, 0x2C83, 0x1CE0, 0x0CC1,
        0xEF1F, 0xFF3E, 0xCF5D, 0xDF7C, 0xAF9B, 0xBFBA, 0x8FD9, 0x9FF8,
        0x6E17, 0x7E36, 0x4E55, 0x5E74, 0x2E93, 0x3EB2, 0x0ED1, 0x1EF0,
    )

    def __init__(self, data: bytes) -> None:
        self.data = data

    @functools.cached_property
    def checksum(self) -> int:
        """Return the running CRC over `self.data`."""
        crc = 0
        for byte in bytearray(self.data):
            crc = self.CRC_TABLE[(crc >> 8) ^ byte] ^ ((crc & 0xFF) << 8)
        return crc

    @functools.cached_property
    def data_with_checksum(self) -> bytes:
        """Return `self.data` with its big-endian CRC appended."""
        return self.data + struct.pack(">H", self.checksum)

    def check(self) -> bool:
        """Return True when `self.data` (including a trailing CRC) is valid."""
        return len(self.data) != 0 and self.checksum == 0


class DataParser(dict):
    """Unpack a binary layout described as `(name, struct_code)` tuples."""

    def __init__(
        self, data: bytes, data_format: tuple[tuple[str, str], ...], order: str = "<"
    ) -> None:
        super().__init__()
        self.fields, format_codes = zip(*data_format, strict=False)
        self.struct = struct.Struct(order + "".join(format_codes))
        self.raw_bytes = data
        values = self.struct.unpack_from(data, 0)
        self["Datetime"] = None
        self.update(zip(self.fields, values, strict=False))

    def tuple_to_dict(self, key: str) -> None:
        """Expand a tuple field into `key01`, `key02`, ... (1-based)."""
        for index, value in enumerate(self[key]):
            self[f"{key}{index + 1:02d}"] = value
        del self[key]


def _bit(value: int, number: int) -> int:
    """Return a Davis bit by its least-significant-bit-first number."""
    return int(bool(value & (1 << number)))


def apply_loop1_alarm_bits(data: Any, raw: bytes) -> None:
    """Decode LOOP1 alarm bytes (offsets 70-85) using Davis LSB-first bit numbering."""
    mappings: tuple[tuple[int, tuple[str, ...]], ...] = (
        (70, ("AlarmInFallBarTrend", "AlarmInRisBarTrend", "AlarmInLowTemp",
              "AlarmInHighTemp", "AlarmInLowHum", "AlarmInHighHum", "AlarmInTime")),
        (71, ("AlarmRainHighRate", "AlarmRain15min", "AlarmRain24hour",
              "AlarmRainStormTotal", "AlarmRainETDaily")),
        (72, ("AlarmOutLowTemp", "AlarmOutHighTemp", "AlarmOutWindSpeed",
              "AlarmOut10minAvgSpeed", "AlarmOutLowDewpoint", "AlarmOutHighDewPoint",
              "AlarmOutHighHeat", "AlarmOutLowWindChill")),
        (73, ("AlarmOutHighTHSW", "AlarmOutHighSolarRad", "AlarmOutHighUV",
              "AlarmOutUVDose", "AlarmOutUVDoseEnabled")),
    )
    for offset, names in mappings:
        for number, name in enumerate(names):
            data[name] = _bit(raw[offset], number)

    data["AlarmOutLowHum"] = _bit(raw[74], 2)
    data["AlarmOutHighHum"] = _bit(raw[74], 3)

    for sensor in range(1, 8):
        value = raw[74 + sensor]
        for number, suffix in enumerate(("LowTemp", "HighTemp", "LowHum", "HighHum")):
            data[f"AlarmEx{sensor:02d}{suffix}"] = _bit(value, number)

    soil_leaf_suffixes = (
        "LowLeafWet", "HighLeafWet", "LowSoilMois", "HighSoilMois",
        "LowLeafTemp", "HighLeafTemp", "LowSoilTemp", "HighSoilTemp",
    )
    for sensor in range(1, 5):
        value = raw[81 + sensor]
        for number, suffix in enumerate(soil_leaf_suffixes):
            data[f"Alarm{sensor:02d}{suffix}"] = _bit(value, number)


class LoopDataParserRevB(DataParser):
    """Parse a LOOP (Rev B) packet. Offsets match the manual's LOOP table."""

    LOOP_FORMAT: tuple[tuple[str, str], ...] = (
        ("LOO", "3s"),
        ("BarTrend", "B"),
        ("PacketType", "B"),
        ("NextRec", "H"),
        ("Barometer", "H"),
        ("TempIn", "h"),
        ("HumIn", "B"),
        ("TempOut", "h"),
        ("WindSpeed", "B"),
        ("WindSpeed10Min", "B"),
        ("WindDir", "H"),
        ("ExtraTemps", "7s"),
        ("SoilTemps", "4s"),
        ("LeafTemps", "4s"),
        ("HumOut", "B"),
        ("HumExtra", "7s"),
        ("RainRate", "H"),
        ("UV", "B"),
        ("SolarRad", "H"),
        ("RainStorm", "H"),
        ("StormStartDate", "H"),
        ("RainDay", "H"),
        ("RainMonth", "H"),
        ("RainYear", "H"),
        ("ETDay", "H"),
        ("ETMonth", "H"),
        ("ETYear", "H"),
        ("SoilMoist", "4s"),
        ("LeafWetness", "4s"),
        ("AlarmIn", "B"),
        ("AlarmRain", "B"),
        ("AlarmOut", "2s"),
        ("AlarmExTempHum", "8s"),
        ("AlarmSoilLeaf", "4s"),
        ("BatteryStatus", "B"),
        ("BatteryVolts", "H"),
        ("ForecastIcon", "B"),
        ("ForecastRuleNo", "B"),
        ("SunRise", "H"),
        ("SunSet", "H"),
        ("EOL", "2s"),
        ("CRC", "H"),
    )

    def __init__(self, data: bytes, dtime: datetime) -> None:
        super().__init__(data, self.LOOP_FORMAT)
        self["Datetime"] = dtime
        self["Barometer"] = self["Barometer"] / 1000
        self["TempIn"] = unpack_temp(self["TempIn"])
        self["TempOut"] = unpack_temp(self["TempOut"])
        self["HumIn"] = unpack_humidity(self["HumIn"])
        self["HumOut"] = unpack_humidity(self["HumOut"])
        self["WindSpeed"] = unpack_wind_speed(self["WindSpeed"])
        self["WindSpeed10Min"] = unpack_wind_speed(self["WindSpeed10Min"])
        self["WindDir"] = unpack_wind_dir(self["WindDir"])
        self["SolarRad"] = unpack_solar_rad(self["SolarRad"])
        self["RainRate"] = unpack_rain_rate(self["RainRate"])
        # Raw rain click counts; client.py.correct_rain_values() applies the
        # single collector-dependent inches-per-click scale.
        self["UV"] = unpack_uv(self["UV"])
        self["StormStartDate"] = unpack_storm_date(self["StormStartDate"])
        self["ETDay"] = self["ETDay"] / 1000
        self["ETMonth"] = self["ETMonth"] / 100
        self["ETYear"] = self["ETYear"] / 100
        self["BatteryVolts"] = self["BatteryVolts"] * 300 / 512 / 100
        self["SunRise"] = unpack_time(self["SunRise"])
        self["SunSet"] = unpack_time(self["SunSet"])

        self["HumExtra"] = struct.unpack("7B", self["HumExtra"])
        self["ExtraTemps"] = tuple(t - 90 for t in struct.unpack("7B", self["ExtraTemps"]))
        self["SoilMoist"] = struct.unpack("4B", self["SoilMoist"])
        self["SoilTemps"] = tuple(t - 90 for t in struct.unpack("4B", self["SoilTemps"]))
        self["LeafWetness"] = struct.unpack("4B", self["LeafWetness"])
        self["LeafTemps"] = tuple(t - 90 for t in struct.unpack("4B", self["LeafTemps"]))

        apply_loop1_alarm_bits(self, self.raw_bytes)
        for key in ("AlarmIn", "AlarmRain", "AlarmOut", "AlarmExTempHum", "AlarmSoilLeaf"):
            del self[key]

        del self["LOO"]
        del self["NextRec"]
        del self["PacketType"]
        del self["EOL"]
        del self["CRC"]

        self.tuple_to_dict("ExtraTemps")
        self.tuple_to_dict("LeafTemps")
        self.tuple_to_dict("SoilTemps")
        self.tuple_to_dict("HumExtra")
        self.tuple_to_dict("LeafWetness")
        self.tuple_to_dict("SoilMoist")


def unpack_storm_date(packed: int) -> str | None:
    """Unpack a storm-start-date field: bits 15-12 month, 11-7 day, 6-0 year-2000.

    The all-ones sentinel (0xFFFF, confirmed on real hardware with no ISS data,
    2026-09-04) decodes to month=15, day=31 - both nonzero, so a zero-only
    check does not catch it. Validate the ranges directly instead.
    """
    month = (packed >> 12) & 0x0F
    day = (packed >> 7) & 0x1F
    year = (packed & 0x7F) + 2000
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def unpack_uv(raw: int) -> float | None:
    """Unpack a 1-byte UV Index field, raw byte 255 means no UV sensor connected."""
    return None if raw == 255 else raw / 10


def unpack_temp(raw: int) -> float | None:
    """Unpack a signed 16-bit tenths-of-degree temperature field, dash sentinel 32767."""
    return None if raw == 32767 else raw / 10


def unpack_humidity(raw: int) -> int | None:
    """Unpack an unsigned 1-byte percent-humidity field, dash sentinel 255."""
    return None if raw == 255 else raw


def unpack_wind_speed(raw: int) -> int | None:
    """Unpack an unsigned 1-byte mph wind-speed field.

    The manual states a dashed reading is "forced to 0", but a real console
    with no ISS data at all (confirmed 2026-09-04) reported the raw 255
    sentinel instead; report unavailable rather than guess which is true.
    """
    return None if raw == 255 else raw


def unpack_wind_dir(raw: int) -> int | None:
    """Unpack an unsigned 16-bit wind-direction field.

    0 is the manual's own documented "no data" value (north is 360, not 0);
    32767 is an additional sentinel confirmed on real hardware with no ISS
    data (2026-09-04), not documented in the manual.
    """
    return None if raw in (0, 32767) else raw


def unpack_solar_rad(raw: int) -> int | None:
    """Unpack an unsigned 16-bit solar radiation field (W/m^2), dash sentinel 32767."""
    return None if raw == 32767 else raw


def unpack_rain_rate(raw: int) -> int | None:
    """Unpack a raw rain-rate click-count field, dash sentinel 65535 (0xFFFF)."""
    return None if raw == 65535 else raw


def unpack_time(packed: int) -> str | None:
    """Unpack a packed `hour*100+minute` field into `HH:MM`."""
    if packed in (0xFFFF, 65535):
        return None
    hour, minute = divmod(packed, 100)
    return f"{hour:02d}:{minute:02d}"


LOOP2_FORMAT: tuple[tuple[str, str], ...] = (
    ("LOO", "3s"),
    ("BarTrend", "B"),
    ("PacketType", "B"),
    ("Unused0", "2s"),
    ("Barometer", "H"),
    ("TempIn", "h"),
    ("HumIn", "B"),
    ("TempOut", "h"),
    ("WindSpeed", "B"),
    ("Unused1", "1s"),
    ("WindDir", "H"),
    ("WindSpeed10Min", "H"),
    ("WindSpeed2Min", "H"),
    ("WindGust10Min", "H"),
    ("WindGustDir10Min", "H"),
    ("Unused2", "2s"),
    ("Unused3", "2s"),
    ("DewPoint", "h"),
    ("Unused4", "1s"),
    ("HumOut", "B"),
    ("Unused5", "1s"),
    ("HeatIndex", "h"),
    ("WindChill", "h"),
    ("THSWIndex", "h"),
    ("RainRate", "H"),
    ("UV", "B"),
    ("SolarRad", "H"),
    ("RainStorm", "H"),
    ("StormStartDate", "H"),
    ("RainDay", "H"),
    ("RainLast15Min", "H"),
    ("RainLastHour", "H"),
    ("ETDay", "H"),
    ("RainLast24Hr", "H"),
    ("BarReductionMethod", "B"),
    ("UserBarOffset", "h"),
    ("BarCalNumber", "h"),
    ("BarSensorRaw", "H"),
    ("BarAbsolute", "H"),
    ("AltimeterSetting", "H"),
    ("GraphPointers", "10s"),
    ("Unused6", "14s"),
    ("EOL", "2s"),
    ("CRC", "H"),
)

# Cumulative-offset milestones, verified 2026-09-04 against a live Vantage
# Pro2 (firmware 3.88, station type 16) LOOP2 capture on COM3 by comparing
# decoded DewPoint/HumOut/HeatIndex/WindChill/RainRate against the same
# moment's known-correct LOOP1 reading (see docs/decisions.md). This
# assertion fails loudly at import time if a future edit drifts these.
_LOOP2_OFFSET_MILESTONES: dict[str, int] = {
    "LOO": 0,
    "WindDir": 16,
    "DewPoint": 30,
    "RainRate": 41,
    "BarReductionMethod": 60,
    "EOL": 95,
}


def _assert_loop2_offsets() -> None:
    """Fail at import time if LOOP2_FORMAT's field offsets drift from the manual."""
    offset = 0
    for name, code in LOOP2_FORMAT:
        if name in _LOOP2_OFFSET_MILESTONES:
            expected = _LOOP2_OFFSET_MILESTONES[name]
            if offset != expected:
                raise AssertionError(
                    f"LOOP2_FORMAT offset regression: {name!r} is at byte "
                    f"{offset}, expected {expected} per the Davis manual"
                )
        offset += struct.calcsize(code)
    total = struct.calcsize("<" + "".join(code for _, code in LOOP2_FORMAT))
    if total != 99:
        raise AssertionError(f"LOOP2_FORMAT must total 99 bytes, got {total}")


_assert_loop2_offsets()


class LoopData2Parser(DataParser):
    """Parse a LOOP2 packet (the `LPS 2 1` command)."""

    LOOP2_FORMAT = LOOP2_FORMAT

    def __init__(self, data: bytes) -> None:
        super().__init__(data, self.LOOP2_FORMAT)

        self["Barometer"] = self["Barometer"] / 1000
        self["TempIn"] = unpack_temp(self["TempIn"])
        self["TempOut"] = unpack_temp(self["TempOut"])
        self["HumIn"] = unpack_humidity(self["HumIn"])
        self["HumOut"] = unpack_humidity(self["HumOut"])
        self["WindSpeed"] = unpack_wind_speed(self["WindSpeed"])
        self["WindDir"] = unpack_wind_dir(self["WindDir"])
        self["SolarRad"] = unpack_solar_rad(self["SolarRad"])
        self["RainRate"] = unpack_rain_rate(self["RainRate"])
        self["WindSpeed10Min"] = self["WindSpeed10Min"] / 10
        self["WindSpeed2Min"] = self["WindSpeed2Min"] / 10
        self["WindGust10Min"] = self["WindGust10Min"] / 10
        for key in ("DewPoint", "HeatIndex", "WindChill", "THSWIndex"):
            self[key] = None if self[key] == 255 else self[key]
        self["UV"] = unpack_uv(self["UV"])
        self["StormStartDate"] = unpack_storm_date(self["StormStartDate"])
        self["ETDay"] = self["ETDay"] / 1000
        # Rain* fields (other than RainRate above) are left as raw click
        # counts; see module docstring.

        # LOOP1-only keys are filled with None so downstream lookups do not KeyError.
        self["SunRise"] = None
        self["SunSet"] = None


class HighLowParserRevB(DataParser):
    """Parse the HILOWS response (436 data bytes + 2-byte CRC)."""

    HILOWS_FORMAT: tuple[tuple[str, str], ...] = (
        ("BaroLoDay", "H"), ("BaroHiDay", "H"),
        ("BaroLoMonth", "H"), ("BaroHiMonth", "H"),
        ("BaroLoYear", "H"), ("BaroHiYear", "H"),
        ("BaroLoTime", "H"), ("BaroHiTime", "H"),
        ("WindHiDay", "B"), ("WindHiTime", "H"),
        ("WindHiMonth", "B"), ("WindHiYear", "B"),
        ("InTempHiDay", "H"), ("InTempLoDay", "H"),
        ("InTempHiTime", "H"), ("InTempLoTime", "H"),
        ("InTempLoMonth", "H"), ("InTempHiMonth", "H"),
        ("InTempLoYear", "H"), ("InTempHiYear", "H"),
        ("InHumHiDay", "B"), ("InHumLoDay", "B"),
        ("InHumHiTime", "H"), ("InHumLoTime", "H"),
        ("InHumHiMonth", "B"), ("InHumLoMonth", "B"),
        ("InHumHiYear", "B"), ("InHumLoYear", "B"),
        ("TempLoDay", "h"), ("TempHiDay", "h"),
        ("TempLoTime", "H"), ("TempHiTime", "H"),
        ("TempHiMonth", "h"), ("TempLoMonth", "h"),
        ("TempHiYear", "h"), ("TempLoYear", "h"),
        ("DewLoDay", "h"), ("DewHiDay", "h"),
        ("DewLoTime", "H"), ("DewHiTime", "H"),
        ("DewHiMonth", "h"), ("DewLoMonth", "h"),
        ("DewHiYear", "h"), ("DewLoYear", "h"),
        ("ChillLoDay", "h"), ("ChillLoTime", "H"),
        ("ChillLoMonth", "h"), ("ChillLoYear", "h"),
        ("HeatHiDay", "h"), ("HeatHiTime", "H"),
        ("HeatHiMonth", "h"), ("HeatHiYear", "h"),
        ("THSWHiDay", "h"), ("THSWHiTime", "H"),
        ("THSWHiMonth", "h"), ("THSWHiYear", "h"),
        ("SolarHiDay", "H"), ("SolarHiTime", "H"),
        ("SolarHiMonth", "H"), ("SolarHiYear", "H"),
        ("UVHiDay", "B"), ("UVHiTime", "H"),
        ("UVHiMonth", "B"), ("UVHiYear", "B"),
        ("RainHiDay", "H"), ("RainHiTime", "H"),
        ("RainHiHour", "H"), ("RainHiMonth", "H"), ("RainHiYear", "H"),
        # Extra/Soil/Leaf temps: 15 sub-entries (0-6 extra 2-8, 7-10 soil 1-4,
        # 11-14 leaf 1-4). Bytes: DayLow(15)/DayHi(15)/TimeDayLow(30)/
        # TimeDayHi(30)/MonthHi(15)/MonthLow(15)/YearHi(15)/YearLow(15).
        # Stored here as raw byte strings (not struct repeat counts, which
        # would desync DataParser's name<->value zip) and expanded into
        # per-sensor keys by _decode_extra_soil_leaf_temps() below.
        ("XTempDayLow", "15s"), ("XTempDayHi", "15s"),
        ("XTempTimeDayLow", "30s"), ("XTempTimeDayHi", "30s"),
        ("XTempMonthHi", "15s"), ("XTempMonthLow", "15s"),
        ("XTempYearHi", "15s"), ("XTempYearLow", "15s"),
        # Outside/Extra humidities: 8 sub-entries (0 outside, 1-7 extra 2-8).
        ("XHumDayLow", "8s"), ("XHumDayHi", "8s"),
        ("XHumTimeDayLow", "16s"), ("XHumTimeDayHi", "16s"),
        ("XHumMonthHi", "8s"), ("XHumMonthLow", "8s"),
        ("XHumYearHi", "8s"), ("XHumYearLow", "8s"),
        # Soil moisture: 4 sub-entries. Order per manual: DayHi/TimeDayHi/
        # DayLow/TimeDayLow/MonthLow/MonthHi/YearLow/YearHi.
        ("SoilMoistDayHi", "4s"), ("SoilMoistTimeDayHi", "8s"),
        ("SoilMoistDayLow", "4s"), ("SoilMoistTimeDayLow", "8s"),
        ("SoilMoistMonthLow", "4s"), ("SoilMoistMonthHi", "4s"),
        ("SoilMoistYearLow", "4s"), ("SoilMoistYearHi", "4s"),
        # Leaf wetness: same 4-entry structure as soil moisture.
        ("LeafWetDayHi", "4s"), ("LeafWetTimeDayHi", "8s"),
        ("LeafWetDayLow", "4s"), ("LeafWetTimeDayLow", "8s"),
        ("LeafWetMonthLow", "4s"), ("LeafWetMonthHi", "4s"),
        ("LeafWetYearLow", "4s"), ("LeafWetYearHi", "4s"),
        ("CRC", "H"),
    )

    # Index ranges within the 15-entry extra/soil/leaf temp sub-arrays.
    _EXTRA_TEMP_INDICES = range(0, 7)   # Extra Temps 2-8
    _SOIL_TEMP_INDICES = range(7, 11)   # Soil Temps 1-4
    _LEAF_TEMP_INDICES = range(11, 15)  # Leaf Temps 1-4

    def __init__(self, data: bytes, dtime: datetime) -> None:
        super().__init__(data, self.HILOWS_FORMAT)
        self["Datetime"] = dtime

        for key in (
            "BaroLoDay", "BaroHiDay", "BaroLoMonth", "BaroHiMonth",
            "BaroLoYear", "BaroHiYear",
        ):
            self[key] = self[key] / 1000
        self["BaroLoTime"] = unpack_time(self["BaroLoTime"])
        self["BaroHiTime"] = unpack_time(self["BaroHiTime"])
        self["WindHiTime"] = unpack_time(self["WindHiTime"])

        for key in (
            "InTempHiDay", "InTempLoDay", "InTempLoMonth", "InTempHiMonth",
            "InTempLoYear", "InTempHiYear",
            "TempLoDay", "TempHiDay", "TempHiMonth", "TempLoMonth",
            "TempHiYear", "TempLoYear",
        ):
            self[key] = self[key] / 10
        self["TempLoTime"] = unpack_time(self["TempLoTime"])
        self["TempHiTime"] = unpack_time(self["TempHiTime"])
        self["DewLoTime"] = unpack_time(self["DewLoTime"])
        self["DewHiTime"] = unpack_time(self["DewHiTime"])
        self["SolarHiTime"] = unpack_time(self["SolarHiTime"])

        self["UVHiDay"] = self["UVHiDay"] / 10
        self["UVHiTime"] = unpack_time(self["UVHiTime"])
        self["UVHiMonth"] = self["UVHiMonth"] / 10
        self["UVHiYear"] = self["UVHiYear"] / 10

        # Rain hi/lo fields are raw click counts; see module docstring.
        self["RainHiTime"] = unpack_time(self["RainHiTime"])

        self._decode_extra_soil_leaf_temps()
        self._decode_humidities()
        self._decode_soil_moisture_and_leaf_wetness()

    def _decode_extra_soil_leaf_temps(self) -> None:
        """Split the 15-entry extra/soil/leaf temperature sub-arrays."""
        temp_fields = (
            "XTempDayLow", "XTempDayHi", "XTempMonthHi", "XTempMonthLow",
            "XTempYearHi", "XTempYearLow",
        )
        time_fields = ("XTempTimeDayLow", "XTempTimeDayHi")

        for field in temp_fields:
            raw = struct.unpack("<15B", self.pop(field))
            for group, indices, prefix in (
                ("Extra", self._EXTRA_TEMP_INDICES, "ExtraTemp"),
                ("Soil", self._SOIL_TEMP_INDICES, "SoilTemp"),
                ("Leaf", self._LEAF_TEMP_INDICES, "LeafTemp"),
            ):
                for position, source_index in enumerate(indices):
                    sensor_no = position + (2 if group == "Extra" else 1)
                    self[f"{prefix}{sensor_no:02d}{field[5:]}"] = raw[source_index] - 90

        for field in time_fields:
            raw = struct.unpack("<15H", self.pop(field))
            for group, indices, prefix in (
                ("Extra", self._EXTRA_TEMP_INDICES, "ExtraTemp"),
                ("Soil", self._SOIL_TEMP_INDICES, "SoilTemp"),
                ("Leaf", self._LEAF_TEMP_INDICES, "LeafTemp"),
            ):
                for position, source_index in enumerate(indices):
                    sensor_no = position + (2 if group == "Extra" else 1)
                    self[f"{prefix}{sensor_no:02d}Time{field[9:]}"] = unpack_time(raw[source_index])

    def _decode_humidities(self) -> None:
        """Split the 8-entry outside/extra humidity sub-arrays."""
        hum_fields = (
            "XHumDayLow", "XHumDayHi", "XHumMonthHi", "XHumMonthLow",
            "XHumYearHi", "XHumYearLow",
        )
        time_fields = ("XHumTimeDayLow", "XHumTimeDayHi")

        for field in hum_fields:
            raw = struct.unpack("<8B", self.pop(field))
            suffix = field[4:]
            self[f"OutHum{suffix}"] = raw[0]
            for index in range(1, 8):
                self[f"ExtraHum{index + 1:02d}{suffix}"] = raw[index]

        for field in time_fields:
            raw = struct.unpack("<8H", self.pop(field))
            suffix = field[8:]
            self[f"OutHumTime{suffix}"] = unpack_time(raw[0])
            for index in range(1, 8):
                self[f"ExtraHum{index + 1:02d}Time{suffix}"] = unpack_time(raw[index])

    def _decode_soil_moisture_and_leaf_wetness(self) -> None:
        """Split the 4-entry soil moisture / leaf wetness sub-arrays."""
        for prefix, byte_fields, time_fields in (
            (
                "SoilMoist",
                ("SoilMoistDayHi", "SoilMoistDayLow", "SoilMoistMonthLow",
                 "SoilMoistMonthHi", "SoilMoistYearLow", "SoilMoistYearHi"),
                ("SoilMoistTimeDayHi", "SoilMoistTimeDayLow"),
            ),
            (
                "LeafWet",
                ("LeafWetDayHi", "LeafWetDayLow", "LeafWetMonthLow",
                 "LeafWetMonthHi", "LeafWetYearLow", "LeafWetYearHi"),
                ("LeafWetTimeDayHi", "LeafWetTimeDayLow"),
            ),
        ):
            for field in byte_fields:
                raw = struct.unpack("<4B", self.pop(field))
                suffix = field[len(prefix):]
                for index, value in enumerate(raw):
                    self[f"{prefix}{index + 1:02d}{suffix}"] = value
            for field in time_fields:
                raw = struct.unpack("<4H", self.pop(field))
                suffix = field[len(prefix) + 4:]  # strip prefix + "Time"
                for index, value in enumerate(raw):
                    self[f"{prefix}{index + 1:02d}Time{suffix}"] = unpack_time(value)


class ArchiveDataParserRevB(DataParser):
    """Parse one 52-byte Rev B archive record. Rev A is not supported."""

    ARCHIVE_FORMAT: tuple[tuple[str, str], ...] = (
        ("DateStamp", "H"),
        ("TimeStamp", "H"),
        ("TempOut", "h"),
        ("TempOutHi", "h"),
        ("TempOutLow", "h"),
        ("Rainfall", "H"),
        ("HighRainRate", "H"),
        ("Barometer", "H"),
        ("SolarRad", "H"),
        ("WindSamps", "H"),
        ("TempIn", "h"),
        ("HumIn", "B"),
        ("HumOut", "B"),
        ("WindAvg", "B"),
        ("WindHi", "B"),
        ("WindHiDir", "B"),
        ("WindAvgDir", "B"),
        ("UV", "B"),
        ("ETHour", "B"),
        ("SolarRadHi", "H"),
        ("UVHi", "B"),
        ("ForecastRuleNo", "B"),
        ("LeafTemps", "2s"),
        ("LeafWetness", "2s"),
        ("SoilTemps", "4s"),
        ("RecType", "B"),
        ("ExtraHum", "2s"),
        ("ExtraTemps", "3s"),
        ("SoilMoist", "4s"),
    )

    def __init__(self, data: bytes) -> None:
        super().__init__(data, self.ARCHIVE_FORMAT)
        self["Datetime"] = unpack_dmp_date_time(self["DateStamp"], self["TimeStamp"])
        del self["DateStamp"]
        del self["TimeStamp"]

        self["TempOut"] = self["TempOut"] / 10
        self["TempOutHi"] = self["TempOutHi"] / 10
        self["TempOutLow"] = self["TempOutLow"] / 10
        self["Barometer"] = self["Barometer"] / 1000
        self["TempIn"] = self["TempIn"] / 10
        self["UV"] = unpack_uv(self["UV"])
        # UVHi's dash value is 0 per the manual, indistinguishable from a real
        # zero reading at night; left unscaled-but-ambiguous like the manual itself.
        self["UVHi"] = self["UVHi"] / 10
        self["ETHour"] = self["ETHour"] / 1000
        # Rainfall/HighRainRate stay raw click counts; see class docstring.

        self["SoilTemps"] = tuple(t - 90 for t in struct.unpack("4B", self["SoilTemps"]))
        self["ExtraHum"] = struct.unpack("2B", self["ExtraHum"])
        self["SoilMoist"] = struct.unpack("4B", self["SoilMoist"])
        self["LeafTemps"] = tuple(t - 90 for t in struct.unpack("2B", self["LeafTemps"]))
        self["LeafWetness"] = struct.unpack("2B", self["LeafWetness"])
        self["ExtraTemps"] = tuple(t - 90 for t in struct.unpack("3B", self["ExtraTemps"]))

        self.tuple_to_dict("SoilTemps")
        self.tuple_to_dict("LeafTemps")
        self.tuple_to_dict("ExtraTemps")
        self.tuple_to_dict("SoilMoist")
        self.tuple_to_dict("LeafWetness")
        self.tuple_to_dict("ExtraHum")


def pack_dmp_date_time(d: datetime) -> bytes:
    """Pack `d` into the 4-byte DMPAFT date/time-stamp block plus CRC."""
    vpdate = d.day + d.month * 32 + (d.year - 2000) * 512
    vptime = 100 * d.hour + d.minute
    data = struct.pack("<HH", vpdate, vptime)
    return VantageProCRC(data).data_with_checksum


def unpack_dmp_date_time(date: int, time_value: int) -> datetime | None:
    """Unpack an archive record's packed date/time fields."""
    if date in (0xFFFF, 0) and time_value in (0xFFFF, 0):
        return None
    day = date & 0x1F
    month = (date >> 5) & 0x0F
    year = ((date >> 9) & 0x7F) + 2000
    if month == 0 or day == 0:
        return None
    hour, minute = divmod(time_value, 100)
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def pack_datetime(dtime: datetime) -> bytes:
    """Pack `dtime` into the 6-byte SETTIME block plus CRC."""
    data = struct.pack(
        ">BBBBBB",
        dtime.second, dtime.minute, dtime.hour,
        dtime.day, dtime.month, dtime.year - 1900,
    )
    return VantageProCRC(data).data_with_checksum


def unpack_datetime(data: bytes) -> datetime:
    """Unpack the 6-byte GETTIME block (CRC already validated by the caller)."""
    second, minute, hour, day, month, year = struct.unpack(">BBBBBB", data[:6])
    return datetime(year + 1900, month, day, hour, minute, second)


def _bytes(value: str | bytes) -> bytes:
    """Normalize the link's text-or-bytes return type."""
    return value.encode("latin-1") if isinstance(value, str) else value


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
        raise DavisBadDataError(
            f"Expected {expected_size}-byte {label}, got {len(data)} bytes"
        )
    if not VantageProCRC(data).check():
        raise DavisBadCRCError(f"Invalid CRC in {label}")


def validate_loop_frame(data: bytes, packet_type: int) -> None:
    """Validate the common LOOP/LOOP2 envelope and CRC."""
    validate_crc_frame(data, 99, f"LOOP{packet_type + 1} packet")
    if data[:3] != b"LOO" or data[4] != packet_type or data[95:97] != b"\n\r":
        raise DavisBadDataError("Invalid LOOP packet signature, type, or terminator")


class DavisProtocolClient:
    """Communicates with a Davis console: wake-up, commands, binary parsing."""

    WAKE_STR = "\n"
    WAKE_ACK = "\n\r"
    ACK = "\x06"
    NACK = "\x21"
    DONE = "DONE\n\r"
    CANCEL = "\x18"
    ESC = "\x1b"
    OK = "\n\rOK\n\r"

    def __init__(self, link: Any) -> None:
        if sys.byteorder != "little":
            raise RuntimeError("Davis binary parsing requires a little-endian host")
        self.link = link
        self.link.open()
        self._check_revision()

    def _read_binary(self, size: int, *, timeout: float | None = None) -> bytes:
        return _bytes(self.link.read(size, timeout=timeout, binary=True))

    @retry(tries=3, delay=1)
    def wake_up(self) -> bool:
        """Wake up the console; retries up to 3 times per the manual."""
        self.link.write(self.WAKE_STR)
        ack = self.link.read(len(self.WAKE_ACK))
        if ack == self.WAKE_ACK:
            return True
        # A stray byte can shift the buffer by one; consume it and retry.
        self.link.read(1)
        raise DavisNoDeviceError("Console did not acknowledge the wake-up byte")

    @retry(tries=3, delay=0.5)
    def send(self, data: str | bytes, wait_ack: str | bytes | None = None,
              timeout: float | None = None) -> bool:
        """Send a command or raw bytes, optionally requiring an exact ACK."""
        if isinstance(data, bytes | bytearray):
            self.link.write(data)
        else:
            self.link.write(f"{data}\n")
        if wait_ack is None:
            return True
        ack = self.link.read(len(wait_ack), timeout=timeout)
        if ack == wait_ack:
            return True
        raise DavisBadAckError(f"Expected {wait_ack!r}, got {ack!r}")

    def gettime(self) -> datetime:
        """Read console time only when its CRC is valid (GETTIME)."""
        self.wake_up()
        self.send("GETTIME", self.ACK)
        raw = self._read_binary(8)
        validate_crc_frame(raw, 8, "GETTIME response")
        return unpack_datetime(raw)

    def settime(self, dtime: datetime) -> None:
        """Set the console clock (SETTIME)."""
        self.wake_up()
        self.send("SETTIME", self.ACK)
        self.send(pack_datetime(dtime), self.ACK)

    def get_current_data(self) -> LoopDataParserRevB:
        """Read one validated LOOP1 packet."""
        self.wake_up()
        self.send("LOOP 1", self.ACK)
        raw = self._read_binary(99)
        validate_loop_frame(raw, 0)
        if not self.RevB:
            raise NotImplementedError("Rev A LOOP packets are not supported")
        return LoopDataParserRevB(raw, datetime.now())

    def get_current_data_loop2(self) -> LoopData2Parser:
        """Request and parse a single LOOP2 packet (`LPS 2 1`)."""
        self.wake_up()
        self.send("LPS 2 1", self.ACK)
        raw = self._read_binary(99)
        validate_loop_frame(raw, 1)
        return LoopData2Parser(raw)

    def get_hilows(self) -> HighLowParserRevB:
        """Read one validated HILOWS response."""
        self.wake_up()
        self.send("HILOWS", self.ACK)
        raw = self._read_binary(438)
        validate_crc_frame(raw, 438, "HILOWS packet")
        if not self.RevB:
            raise NotImplementedError("Rev A HILOWS packets are not supported")
        return HighLowParserRevB(raw, datetime.now())

    def read_from_eeprom(self, hex_address: str, size: int) -> bytes:
        """Read EEPROM using hexadecimal address and count parameters (EEBRD)."""
        validate_eeprom_range(hex_address, size)
        self.wake_up()
        self.send(f"EEBRD {hex_address.upper()} {size:02X}", self.ACK)
        raw = self._read_binary(size + 2)
        validate_crc_frame(raw, size + 2, "EEPROM response")
        return raw[:-2]

    def write_to_eeprom(self, hex_address: str, size: int, data: bytes) -> None:
        """Write EEPROM with one LF terminator and a validated safe range (EEBWR)."""
        if len(data) != size:
            raise ValueError("EEPROM byte count does not match the payload length")
        validate_eeprom_write(hex_address, size)
        self.wake_up()
        self.send(f"EEBWR {hex_address.upper()} {size:02X}", self.ACK)
        self.send(VantageProCRC(data).data_with_checksum, self.ACK)

    def set_archive_period(self, archive_period: int) -> None:
        """Set the archive period and require the documented ACK response (SETPER)."""
        period = int(archive_period)
        if period not in VALID_ARCHIVE_PERIODS:
            raise ValueError(f"Unsupported Davis archive period: {period}")
        self.wake_up()
        self.send(f"SETPER {period}", self.ACK)

    def set_yearly_rain(self, rain_clicks: int) -> None:
        """Set yearly rainfall in rain clicks (PUTRAIN)."""
        self.wake_up()
        self.send(f"PUTRAIN {rain_clicks}", self.ACK)

    def set_yearly_et(self, et_hundredths: int) -> None:
        """Set yearly ET in 100ths of an inch (PUTET)."""
        self.wake_up()
        self.send(f"PUTET {et_hundredths}", self.ACK)

    def newsetup(self) -> None:
        """Re-initialize the console after EEPROM configuration changes (NEWSETUP)."""
        self.wake_up()
        self.send("NEWSETUP", self.ACK)

    def get_rain_collector(self) -> int:
        """Return the configured rain-collector type (0x00/0x10/0x20)."""
        setup_bits = struct.unpack("B", self.read_from_eeprom("2B", 1))[0]
        return setup_bits & 0x30

    def set_rain_collector(self, collector_type: int) -> None:
        """Set rain collector bits through the documented read-modify-write."""
        if collector_type not in (0x00, 0x10, 0x20):
            raise ValueError("Unsupported Davis rain collector type")
        self.wake_up()
        setup_bits = struct.unpack("<B", self.read_from_eeprom("2B", 1))[0]
        updated_bits = (setup_bits & 0xCF) | collector_type
        self.write_to_eeprom("2B", 1, struct.pack("<B", updated_bits))
        self.newsetup()

    def test(self) -> bool:
        """Connection sanity check: TEST echoes "TEST" back."""
        self.wake_up()
        self.link.write("TEST\n")
        response = self.link.read(6)
        return _bytes(response).strip(b"\r\n") == b"TEST"

    def get_station_type(self) -> int:
        """Return the WRD station-type byte (16=VP/Pro2, 17=Vue)."""
        self.wake_up()
        self.link.write("WRD\x12\x4D\n")
        ack = self.link.read(len(self.ACK))
        if ack != self.ACK:
            raise DavisBadAckError(f"Expected ACK, got {ack!r}")
        return self._read_binary(1)[0]

    def rxtest(self) -> None:
        """Return the console to the main conditions screen and clear CRC-error count.

        Confirmed against real hardware on 2026-09-04: the console replies with the
        standard "\\n\\rOK\\n\\r" text response, not a raw ACK byte; the manual's RXTEST
        row does not state which one it uses.
        """
        self.wake_up()
        self.send("RXTEST", self.OK)

    def get_receivers(self) -> int:
        """Return the RECEIVERS bitmap of station IDs the console can hear."""
        self.wake_up()
        self.send("RECEIVERS", self.OK)
        return self._read_binary(1)[0]

    def get_calibrated_values(self) -> bytes:
        """Read the 43-byte CALED block of calibrated sensor values."""
        self.wake_up()
        self.send("CALED", self.ACK)
        raw = self._read_binary(45)
        validate_crc_frame(raw, 45, "CALED response")
        return raw[:-2]

    def set_calibrated_values(self, data: bytes) -> None:
        """Push a 43-byte block of uncalibrated raw sensor values (CALFIX)."""
        if len(data) != 43:
            raise ValueError("CALFIX payload must be exactly 43 bytes")
        self.wake_up()
        self.send("CALFIX", self.ACK)
        self.send(VantageProCRC(data).data_with_checksum, self.ACK)

    @functools.cached_property
    def archive_period(self) -> int:
        """Number of minutes in the archive period (ARCHIVE_PERIOD EEPROM field)."""
        return struct.unpack("B", self.read_from_eeprom("2D", 1))[0]

    @functools.cached_property
    def firmware_date(self):
        """Return the firmware date code (VER)."""
        self.wake_up()
        self.send("VER", self.OK)
        data = _bytes(self.link.read(13))
        return datetime.strptime(data.strip(b"\n\r").decode("ascii"), "%b %d %Y").date()

    @functools.cached_property
    def firmware_version(self) -> str:
        """Return the firmware version as a string (NVER)."""
        self.wake_up()
        self.send("NVER", self.OK)
        data = _bytes(self.link.read(6))
        return data.strip(b"\n\r").decode("ascii")

    @functools.cached_property
    def diagnostics(self) -> dict[str, int]:
        """Return the console diagnostics report (RXCHECK)."""
        self.wake_up()
        self.send("RXCHECK", self.OK)
        raw = _bytes(self.link.read())
        values = [int(part) for part in raw.strip(b"\n\r").decode("ascii").split(" ")]
        return {
            "total_received": values[0], "total_missed": values[1],
            "resyn": values[2], "max_received": values[3], "crc_errors": values[4],
        }

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
        raise DavisBadCRCError("DMPAFT page failed validation after 3 attempts")

    def get_archives(
        self, start_date: datetime | None = None, stop_date: datetime | None = None
    ) -> list[ArchiveDataParserRevB]:
        """Return archive records between `start_date` and `stop_date`."""
        seen: set[datetime] = set()
        records: list[ArchiveDataParserRevB] = []
        for record in self._get_archives_generator(start_date, stop_date):
            when = record["Datetime"]
            if when not in seen:
                seen.add(when)
                records.append(record)
        records.sort(key=lambda item: item["Datetime"])
        return records

    def _get_archives_generator(
        self, start_date: datetime | None = None, stop_date: datetime | None = None
    ):
        """Download DMPAFT pages without acknowledging beyond the final page."""
        self.wake_up()
        start = start_date or datetime(2001, 1, 1, 1, 1, 1)
        stop = stop_date or datetime.now()
        period = self.archive_period
        start -= timedelta(minutes=start.minute % period)

        self.send("DMPAFT", self.ACK)
        self.link.write(pack_dmp_date_time(start))
        if _bytes(self.link.read(1, timeout=2, binary=True)) != b"\x06":
            raise DavisBadAckError("Console rejected the DMPAFT date/time handshake")

        header = self._read_binary(6)
        if len(header) != 6 or not VantageProCRC(header).check():
            self.link.write(self.CANCEL)
            raise DavisBadCRCError("Invalid DMPAFT header")
        pages, first_record, _crc = struct.unpack("<HHH", header)
        if first_record > 4:
            self.link.write(self.CANCEL)
            raise DavisBadDataError("Invalid first-record offset in DMPAFT header")
        if pages == 0:
            self.link.write(self.ESC)
            return

        self.link.write(self.ACK)
        for page_number in range(pages):
            try:
                raw_page = self._read_archive_page(page_number)
            except (DavisBadCRCError, DavisBadDataError):
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
                    yield record

            if stop_download:
                self.link.write(self.ESC)
                break
            if page_number < pages - 1:
                self.link.write(self.ACK)

    def _check_revision(self) -> None:
        """Check firmware date and set the Rev A/Rev B data-format flags."""
        cutoff = datetime(2002, 4, 24).date()
        self.RevA = self.RevB = True
        if self.firmware_date < cutoff:
            self.RevB = False
        else:
            self.RevA = False
