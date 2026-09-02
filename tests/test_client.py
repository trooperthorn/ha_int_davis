"""Tests for davis_vantage.client - pure data-transform logic.

These exercise DavisVantageClient's data massaging (rain-unit correction,
dash-value masking, archive/LOOP2 wind-info population) and LoopData2Parser
directly, without touching any serial I/O.
"""
import asyncio
import struct
import threading
import time
from unittest.mock import MagicMock

import pytest

from custom_components.davis_vantage.client import DavisVantageClient, LoopData2Parser
from custom_components.davis_vantage.const import (
    CONNECTION_CONNECTED,
    CONNECTION_DEGRADED,
    PROTOCOL_NETWORK,
    PROTOCOL_SERIAL,
    RAIN_COLLECTOR_IMPERIAL,
    RAIN_COLLECTOR_METRIC,
    RAIN_COLLECTOR_METRIC_0_1,
)


class _FakeVantagePro2:
    """Stands in for the real device connection in tests that only exercise
    data-transform logic and never touch serial I/O."""

    archive_period = 5

    def __init__(self) -> None:
        self.link = MagicMock()


def make_client(**kwargs) -> DavisVantageClient:
    defaults = dict(
        hass=None,
        protocol=PROTOCOL_SERIAL,
        link="/dev/ttyUSB0",
        persistent_connection=False,
    )
    defaults.update(kwargs)
    client = DavisVantageClient(**defaults)
    client._vantagepro2 = _FakeVantagePro2()
    return client


def pack_loop2(**overrides) -> bytes:
    """Build a syntactically valid 99-byte LOOP2 packet with given field overrides."""
    fmt = LoopData2Parser.LOOP2_FORMAT
    values = []
    for name, code in fmt:
        if name in overrides:
            values.append(overrides[name])
        elif code.endswith("s"):
            values.append(b"\x00" * int(code[:-1]))
        else:
            values.append(0)
    struct_fmt = "=" + "".join(code for _, code in fmt)
    return struct.pack(struct_fmt, *values)


class TestGetLink:
    def test_network_protocol_uses_tcp(self):
        client = make_client(protocol=PROTOCOL_NETWORK, link="192.168.1.50:22222")
        assert client.get_link() == "tcp:192.168.1.50:22222"

    def test_serial_protocol_uses_configured_baud(self):
        client = make_client(protocol=PROTOCOL_SERIAL, link="/dev/ttyUSB0", baud_rate=9600)
        assert client.get_link() == "serial:/dev/ttyUSB0:9600:8N1"

    def test_serial_protocol_defaults_to_19200(self):
        client = make_client(protocol=PROTOCOL_SERIAL, link="/dev/ttyUSB0")
        assert client.get_link() == "serial:/dev/ttyUSB0:19200:8N1"

    def test_falsy_baud_rate_falls_back_to_default(self):
        # baud_rate=0/None must not produce "serial:...:0:8N1"
        client = make_client(protocol=PROTOCOL_SERIAL, link="/dev/ttyUSB0", baud_rate=0)
        assert client.get_link() == "serial:/dev/ttyUSB0:19200:8N1"

    def test_weatherlink_tcp_is_released_even_when_persistent_is_requested(self):
        client = make_client(
            protocol=PROTOCOL_NETWORK,
            link="192.168.1.50:22222",
            persistent_connection=True,
        )
        assert client._should_close_after_transaction() is True


class TestCorrectRainValues:
    @pytest.mark.parametrize(
        "collector,factor",
        [
            (RAIN_COLLECTOR_IMPERIAL, 1.0),
            (RAIN_COLLECTOR_METRIC, 2 / 2.54),
            (RAIN_COLLECTOR_METRIC_0_1, 1 / 2.54),
        ],
    )
    def test_applies_collector_factor(self, collector, factor):
        client = make_client()
        data = {"RainCollector": collector, "RainDay": 1.0, "RainRate": 2.0}
        client.correct_rain_values(data)
        assert data["RainDay"] == pytest.approx(factor)
        assert data["RainRate"] == pytest.approx(2.0 * factor)

    def test_missing_keys_are_skipped_not_errored(self):
        client = make_client()
        data = {"RainCollector": RAIN_COLLECTOR_IMPERIAL, "RainDay": None}
        client.correct_rain_values(data)  # must not raise
        assert data["RainDay"] is None

    def test_unknown_collector_defaults_to_imperial_factor(self):
        client = make_client()
        data = {"RainCollector": "", "RainDay": 5.0}
        client.correct_rain_values(data)
        assert data["RainDay"] == 5.0


class TestIsIncorrectValue:
    @pytest.mark.parametrize(
        "raw_value,data_type,expected",
        [
            (255, "B", True),
            (254, "B", False),
            (255, "7s", True),
            (32767, "H", True),
            (65535, "H", True),
            (100, "H", False),
            (32767, "h", True),
            (-32768, "h", True),
            (0, "h", False),
            (255, "unknown_type", False),
        ],
    )
    def test_dash_value_detection(self, raw_value, data_type, expected):
        client = make_client()
        assert client.is_incorrect_value(raw_value, data_type) is expected


class TestAddArchiveInfo:
    def test_populates_gust_and_average_from_latest_archive(self):
        client = make_client()
        data: dict = {}
        archives = [{"WindHi": 22, "WindAvg": 8, "WindAvgDir": 4}]
        client.add_archive_info(archives, data)
        assert data["WindGust"] == 22
        assert data["WindSpeedAvg"] == 8
        assert data["WindAvgDir"] == 4 * 22.5
        assert "WindAvgDirRose" in data

    def test_empty_archives_leaves_data_untouched(self):
        client = make_client()
        data: dict = {}
        client.add_archive_info([], data)
        assert data == {}

    def test_dashed_direction_is_not_set(self):
        client = make_client()
        data: dict = {}
        archives = [{"WindHi": 5, "WindAvg": 3, "WindAvgDir": 255}]
        client.add_archive_info(archives, data)
        assert "WindAvgDir" not in data


class TestAddLoop2WindInfo:
    def test_populates_gust_average_and_gust_direction(self):
        client = make_client(use_loop2=True)
        data = {
            "WindGust10Min": 18.5,
            "WindSpeed10Min": 6.2,
            "WindGustDir10Min": 270,
        }
        client.add_loop2_wind_info(data)
        assert data["WindGust"] == 18.5
        assert data["WindSpeedAvg"] == 6.2
        assert data["WindGustDir"] == 270
        assert data["WindGustDirRose"] == "w"
        assert "WindSpeedBft" in data

    def test_never_fabricates_a_true_average_direction(self):
        # LOOP2 has no real 10-min *average* direction field - only the
        # gust direction. add_loop2_wind_info must not invent WindAvgDir.
        client = make_client(use_loop2=True)
        data = {"WindGust10Min": 10, "WindSpeed10Min": 5, "WindGustDir10Min": 90}
        client.add_loop2_wind_info(data)
        assert "WindAvgDir" not in data
        assert "WindAvgDirRose" not in data

    def test_calm_or_dashed_gust_direction_is_skipped(self):
        client = make_client(use_loop2=True)
        for dashed in (None, 0, 32767):
            data = {"WindGust10Min": 5, "WindSpeed10Min": 2, "WindGustDir10Min": dashed}
            client.add_loop2_wind_info(data)
            assert "WindGustDir" not in data


class TestAddAdditionalInfoPrefersNativeLoop2Values:
    def test_loop1_style_data_gets_calculated_values(self):
        client = make_client(use_loop2=False)
        data = {"TempOut": 75.0, "HumOut": 50.0, "WindSpeed": 5.0}
        client.add_additional_info(data)
        assert data["HeatIndex"] is not None
        assert data["DewPoint"] is not None
        assert data["WindChill"] is not None
        assert data["FeelsLike"] is not None

    def test_native_loop2_values_are_not_overwritten(self):
        client = make_client(use_loop2=True)
        data = {
            "TempOut": 75.0,
            "HumOut": 50.0,
            "WindSpeed": 5.0,
            "HeatIndex": 111.1,
            "DewPoint": 222.2,
            "WindChill": 333.3,
            "THSWIndex": 444.4,
        }
        client.add_additional_info(data)
        assert data["HeatIndex"] == 111.1
        assert data["DewPoint"] == 222.2
        assert data["WindChill"] == 333.3
        # THSW is preferred as FeelsLike when the console provides it.
        assert data["FeelsLike"] == 444.4

    def test_feels_like_falls_back_when_no_thsw(self):
        client = make_client(use_loop2=True)
        data = {"TempOut": 75.0, "HumOut": 50.0, "WindSpeed": 5.0, "THSWIndex": None}
        client.add_additional_info(data)
        assert data["FeelsLike"] is not None


class TestLoopData2Parser:
    def test_total_packet_size_is_99_bytes(self):
        codes = [code for _, code in LoopData2Parser.LOOP2_FORMAT]
        assert struct.calcsize("=" + "".join(codes)) == 99

    def test_scales_temperature_and_barometer(self):
        raw = pack_loop2(Barometer=29910, TempIn=725, TempOut=825, HumIn=45, HumOut=60)
        parsed = LoopData2Parser(raw)
        assert parsed["Barometer"] == pytest.approx(29.91)
        assert parsed["TempIn"] == pytest.approx(72.5)
        assert parsed["TempOut"] == pytest.approx(82.5)
        assert parsed["HumIn"] == 45
        assert parsed["HumOut"] == 60

    def test_scales_wind_fields_to_tenths_of_mph(self):
        raw = pack_loop2(
            WindSpeed=12,
            WindSpeed10Min=95,
            WindSpeed2Min=110,
            WindGust10Min=205,
            WindGustDir10Min=180,
        )
        parsed = LoopData2Parser(raw)
        assert parsed["WindSpeed"] == 12
        assert parsed["WindSpeed10Min"] == pytest.approx(9.5)
        assert parsed["WindSpeed2Min"] == pytest.approx(11.0)
        assert parsed["WindGust10Min"] == pytest.approx(20.5)
        assert parsed["WindGustDir10Min"] == 180

    @pytest.mark.parametrize(
        "field", ["DewPoint", "HeatIndex", "WindChill", "THSWIndex"]
    )
    def test_dash_sentinel_255_becomes_none(self, field):
        raw = pack_loop2(**{field: 255})
        parsed = LoopData2Parser(raw)
        assert parsed[field] is None

    def test_real_value_survives_for_apparent_temperature_fields(self):
        raw = pack_loop2(DewPoint=68, HeatIndex=91, WindChill=45, THSWIndex=95)
        parsed = LoopData2Parser(raw)
        assert parsed["DewPoint"] == 68
        assert parsed["HeatIndex"] == 91
        assert parsed["WindChill"] == 45
        assert parsed["THSWIndex"] == 95

    def test_sunrise_sunset_absent_in_loop2_are_none_not_missing(self):
        raw = pack_loop2()
        parsed = LoopData2Parser(raw)
        # Must not KeyError downstream in convert_values()'s direct
        # data["SunRise"]/data["SunSet"] access.
        assert parsed["SunRise"] is None
        assert parsed["SunSet"] is None

    def test_storm_start_date_field_is_named_like_loop1(self):
        # correct_rain_values() looks for "RainStorm" (LOOP1's name), not
        # "StormRain" - keep LOOP2 consistent so unit correction still runs.
        raw = pack_loop2(RainStorm=150)
        parsed = LoopData2Parser(raw)
        assert parsed["RainStorm"] == pytest.approx(1.5)

    def test_rain_and_et_scaling(self):
        raw = pack_loop2(RainRate=256, RainDay=500, RainLast15Min=10, ETDay=25)
        parsed = LoopData2Parser(raw)
        assert parsed["RainRate"] == pytest.approx(2.56)
        assert parsed["RainDay"] == pytest.approx(5.0)
        assert parsed["RainLast15Min"] == pytest.approx(0.1)
        assert parsed["ETDay"] == pytest.approx(0.025)


class TestAsyncClose:
    async def test_noop_when_never_connected(self):
        client = make_client()
        client._vantagepro2 = None
        # Must not attempt to open a connection just to close it.
        await client.async_close()

    async def test_closes_link_when_connected(self):
        client = make_client()
        client._vantagepro2.link = MagicMock()
        link = client._vantagepro2.link

        assert await client.async_close() is True

        link.close.assert_called_once()

    async def test_close_error_is_caught_not_raised(self):
        client = make_client()
        client._vantagepro2.link = MagicMock()
        client._vantagepro2.link.close.side_effect = OSError("port already gone")

        # A failed close is a controlled shutdown result and still stops the worker.
        assert await client.async_close() is False
        assert client._executor_shutdown is True


class TestIoLockSerializesConcurrentAccess:
    async def test_concurrent_calls_do_not_overlap_on_the_device(self):
        # Two blocking calls that each hold "the device" for a bit - without
        # the lock, run_in_executor's default thread pool would run them on
        # separate threads at the same time, which for a real serial port
        # means interleaved reads/writes on the wire.
        client = make_client()
        overlap_detected = False
        busy = False

        def slow_operation():
            nonlocal overlap_detected, busy
            if busy:
                overlap_detected = True
            busy = True
            time.sleep(0.05)
            busy = False

        client.get_static_info = slow_operation  # type: ignore[method-assign]
        client.get_info = slow_operation  # type: ignore[method-assign]

        await asyncio.gather(
            client.async_get_static_info(), client.async_get_info()
        )

        assert overlap_detected is False
        assert await client.async_close() is True


class TestTransportOwnershipDuringCancellation:
    async def test_cancelled_waiter_does_not_release_physical_worker(self):
        client = make_client()
        started = threading.Event()
        release = threading.Event()
        second_ran = False

        def delayed_read():
            started.set()
            release.wait(timeout=2)

        def second_operation():
            nonlocal second_ran
            second_ran = True

        first = asyncio.create_task(client._async_run_io(delayed_read))
        while not started.is_set():
            await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(client._async_run_io(second_operation))
        await asyncio.sleep(0.02)
        assert second_ran is False
        release.set()
        await second
        assert second_ran is True
        assert await client.async_close() is True

    async def test_unload_close_waits_for_delayed_read(self):
        client = make_client()
        started = threading.Event()
        release = threading.Event()

        def delayed_read():
            started.set()
            release.wait(timeout=2)

        read_task = asyncio.create_task(client._async_run_io(delayed_read))
        while not started.is_set():
            await asyncio.sleep(0)
        close_task = asyncio.create_task(client.async_close())
        await asyncio.sleep(0.02)
        assert close_task.done() is False
        release.set()
        await read_task
        assert await close_task is True

    async def test_failure_reconnects_before_the_next_transaction(self):
        client = make_client()
        client._vantagepro2.link = MagicMock()

        def fail():
            raise OSError("USB logger removed")

        with pytest.raises(OSError):
            await client._async_run_io(fail)
        assert client.connection_diagnostics["state"] == CONNECTION_DEGRADED

        assert await client._async_run_io(lambda: "recovered") == "recovered"
        diagnostics = client.connection_diagnostics
        assert diagnostics["state"] == CONNECTION_CONNECTED
        assert diagnostics["reconnect_count"] == 1
        assert diagnostics["failure_streak"] == 0
        assert await client.async_close() is True

