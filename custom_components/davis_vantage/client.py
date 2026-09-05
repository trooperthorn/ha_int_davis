"""All client function"""
import asyncio
import logging
import re
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dt_time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from serialx import BaseSerial

import contextlib

from .const import (
    CONNECTION_CLOSED,
    CONNECTION_CONNECTED,
    CONNECTION_CONNECTING,
    CONNECTION_DEGRADED,
    CONNECTION_DISCONNECTED,
    CONNECTION_RECONNECTING,
    CONNECTION_STOPPING,
    DEFAULT_BAUD_RATE,
    DEFAULT_SHUTDOWN_TIMEOUT,
    RAIN_COLLECTOR_IMPERIAL,
    RAIN_COLLECTOR_METRIC,
    RAIN_COLLECTOR_METRIC_0_1,
)
from .protocol import (
    DataParser,
    DavisBadAckError,
    DavisProtocolClient,
    HighLowParserRevB,
    LoopData2Parser,
    LoopDataParserRevB,
)
from .utils import (
    calc_dew_point,
    calc_feels_like,
    calc_heat_index,
    calc_wind_chill,
    convert_kmh_to_bft,
    convert_to_iso_datetime,
    convert_to_kmh,
    get_baro_trend,
    get_solar_rad,
    get_uv,
    get_wind_rose,
)

_LOGGER = logging.getLogger(__name__)


class DavisSerialXLink:
    """Minimal link contract backed by serialx.serial_for_url()."""

    def __init__(
        self,
        endpoint: str,
        baudrate: int,
        *,
        timeout: float = 10,
    ) -> None:
        self.endpoint = endpoint
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial: BaseSerial | None = None

    @property
    def url(self) -> str:
        """Return a diagnostic URL without opening the interface."""
        return f"serial:{self.endpoint}:{self.baudrate}:8N1"

    def open(self) -> None:
        """Open a path or supported serial URL."""
        if self._serial is not None and self._serial.is_open:
            return
        import serialx

        self._serial = serialx.serial_for_url(
            self.endpoint,
            baudrate=self.baudrate,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )
        # serialx returns a configured but closed transport.
        self._serial.open()
        self._serial.reset_output_buffer()

    def close(self) -> None:
        """Close the serial interface."""
        if self._serial is not None:
            if self._serial.is_open:
                self._serial.close()
            self._serial = None

    def settimeout(self, timeout: float) -> None:
        """Set the read timeout."""
        self.timeout = timeout
        if self._serial is not None:
            self._serial.timeout = timeout

    def write(self, data: str | bytes) -> None:
        """Write command or binary data."""
        self.open()
        assert self._serial is not None
        payload = data.encode("utf-8") if isinstance(data, str) else data
        self._serial.write(payload)

    def read(
        self, size: int | None = None, timeout: float | None = None, binary: bool = False
    ) -> str | bytes:
        """Read data using the link contract shared with DavisProtocolClient."""
        self.open()
        assert self._serial is not None
        previous_timeout = self._serial.timeout
        effective_timeout = timeout or self.timeout
        try:
            data = self._read_exactly(size, effective_timeout) if size else self._serial.read(4048)
        finally:
            self._serial.timeout = (
                previous_timeout if previous_timeout is not None else self.timeout
            )
        if binary:
            return data
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data

    def _read_exactly(self, size: int, timeout: float) -> bytes:
        """Accumulate reads until size bytes arrive or the timeout elapses.

        serialx's Windows backend subclasses io.RawIOBase, whose read(size)
        returns whatever one underlying ReadFile call produced rather than
        guaranteeing size bytes, so a single call can still return short.
        Confirmed against real hardware (see docs/decisions.md): the per-call
        timeout must be set to the full remaining budget, not a short fixed
        poll interval. A short per-call timeout (e.g. 0.05s) reliably
        truncated large reads like the 438-byte HILOWS response well before
        the console finished sending, even though pyserial's own read(size)
        on the same port captured the identical response whole on the first
        call; the console was never the problem, an over-eager short timeout
        was. Recomputing the timeout from the remaining budget on every loop
        iteration keeps this resilient to a genuinely short response too.
        """
        assert self._serial is not None
        data = bytearray()
        deadline = time.monotonic() + timeout
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._serial.timeout = remaining
            chunk = self._serial.read(size - len(data))
            if chunk:
                data.extend(chunk)
        return bytes(data)


class DavisVantageClient:
    """Davis Vantage Client class"""

    _protocol_client: DavisProtocolClient | None = None
    _latitude: float = 0.0
    _longitude: float = 0.0
    _elevation: int = 0
    _firmware_version: str | None = None
    _last_readout_duration: float = 0

    def __init__(
        self,
        hass,
        protocol: str,
        link: str,
        persistent_connection: bool,
        use_loop2: bool = False,
        baud_rate: int = DEFAULT_BAUD_RATE,
    ) -> None:
        self._hass = hass
        self._protocol = protocol
        self._link = link
        self._rain_collector = ""
        self._last_data = {}  # type: ignore
        self._last_raw_data = {}  # type: ignore
        self._last_raw_hilows = {}  # type: ignore
        self._persistent_connection = persistent_connection
        self._use_loop2 = use_loop2
        self._baud_rate = baud_rate or DEFAULT_BAUD_RATE
        # Single-worker executor per entry; cancelling an HA waiter never releases the device.
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="davis-vantage"
        )
        self._submit_lock = asyncio.Lock()
        self._pending_io: set[asyncio.Future[Any]] = set()
        self._stopping = False
        self._executor_shutdown = False
        self._needs_reconnect = False
        self._connection_state = CONNECTION_DISCONNECTED
        self._last_success: str | None = None
        self._last_failure: str | None = None
        self._last_failure_time: str | None = None
        self._failure_streak = 0
        self._reconnect_count = 0
        self._last_close_result: str | None = None

    def _should_close_after_transaction(self) -> bool:
        """Serial-only: release the port unless a persistent connection was requested."""
        return not self._persistent_connection

    def _close_transport_sync(self) -> None:
        """Close without constructing a lazy transport."""
        if self._protocol_client is not None:
            self._protocol_client.link.close()

    def _execute_owned(self, func, args: tuple[Any, ...]) -> Any:
        """Execute one operation on the dedicated worker."""
        if self._needs_reconnect:
            self._connection_state = CONNECTION_RECONNECTING
            with contextlib.suppress(OSError, TimeoutError):
                self._close_transport_sync()
            self._protocol_client = None
            self._needs_reconnect = False
            self._reconnect_count += 1
        try:
            result = func(*args)
        except Exception as err:
            self._connection_state = CONNECTION_DEGRADED
            self._last_failure = str(err)
            self._last_failure_time = datetime.now().isoformat()
            self._failure_streak += 1
            self._needs_reconnect = True
            with contextlib.suppress(OSError, TimeoutError):
                self._close_transport_sync()
            self._protocol_client = None
            raise
        self._connection_state = CONNECTION_CONNECTED
        self._last_success = datetime.now().isoformat()
        self._failure_streak = 0
        return result

    async def _async_run_io(self, func, *args: Any) -> Any:
        """Queue one device operation and shield its physical ownership."""
        async with self._submit_lock:
            if self._stopping or self._executor_shutdown:
                raise RuntimeError("Davis transport is stopping")
            concurrent_future = self._executor.submit(self._execute_owned, func, args)
            future = asyncio.wrap_future(concurrent_future)
            self._pending_io.add(future)
            future.add_done_callback(self._pending_io.discard)
        return await asyncio.shield(future)

    @property
    def connection_diagnostics(self) -> dict[str, Any]:
        """Return non-sensitive transport lifecycle diagnostics."""
        return {
            "state": self._connection_state,
            "last_success": self._last_success,
            "last_failure": self._last_failure,
            "last_failure_time": self._last_failure_time,
            "failure_streak": self._failure_streak,
            "reconnect_count": self._reconnect_count,
            "in_flight_or_queued": len(self._pending_io),
            "last_close_result": self._last_close_result,
        }

    @property
    def latitude(self) -> float:
        return self._latitude

    @property
    def longitude(self) -> float:
        return self._longitude

    @property
    def elevation(self) -> int:
        return self._elevation

    @property
    def firmware_version(self) -> str | None:
        return self._firmware_version

    @property
    def link(self):
        """Bridge to the underlying protocol client's link object."""
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())

        return self._protocol_client.link

    def get_protocol_client_from_url(self, url: str) -> DavisProtocolClient:
        del url  # serial-only: the link is built from configured endpoint/baud directly
        serial_link = DavisSerialXLink(self._link, self._baud_rate)
        serial_link.settimeout(10)
        client = DavisProtocolClient(serial_link)
        if self._should_close_after_transaction():
            client.link.close()
        return client

    async def async_get_vantagepro2fromurl(self, url: str):
        _LOGGER.debug("async_get_vantagepro2fromurl with url=%s", url)
        try:
            return await self._async_run_io(self.get_protocol_client_from_url, url)
        except Exception as err:
            _LOGGER.error("Error on opening device from url: %s: %s", url, err)
            return None

    async def connect_to_station(self) -> None:
        """Connect to the Davis station and perform a real wake exchange."""
        self._connection_state = CONNECTION_CONNECTING

        def _connect() -> None:
            if self._protocol_client is None:
                self._protocol_client = self.get_protocol_client_from_url(self.get_link())
            self._protocol_client.link.open()
            self._protocol_client.wake_up()
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

        await self._async_run_io(_connect)

    async def get_station_info(self):
        static_info = await self.async_get_static_info()
        self._firmware_version = (
            static_info.get("version", None) if static_info is not None else None
        )
        latitude, longitude, elevation = (
            await self.async_get_latitude_longitude_elevation()
        )
        if latitude:
            self._latitude = latitude
        if longitude:
            self._longitude = longitude
        if elevation:
            self._elevation = elevation

    def get_current_data(self):
        """Get current data from weather station."""
        data = None
        archives = None
        hilows = None

        start_readout = datetime.now()

        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())

        try:
            self._protocol_client.link.open()

            if self._use_loop2:
                _LOGGER.debug("Start get_current_data (LOOP 2 requested)")
                data = self._protocol_client.get_current_data_loop2()
            else:
                _LOGGER.debug("Start get_current_data (LOOP 1)")
                data = self._protocol_client.get_current_data()

            _LOGGER.debug("End get_current_data:")
        except Exception as e:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()
            raise e

        try:
            _LOGGER.debug("Start get_hilows")
            hilows = self._protocol_client.get_hilows()
            _LOGGER.debug("End get_hilows")
        except Exception as e:
            _LOGGER.error("Couldn't get hilows: %s", e)

        # LOOP2 already carries 10-min gust and average; the archive fetch is LOOP1-only.
        if not self._use_loop2:
            try:
                end_datetime = datetime.now()
                start_datetime = end_datetime - timedelta(
                    minutes=self._protocol_client.archive_period * 2
                )
                _LOGGER.debug("Start get_archives")
                archives = self._protocol_client.get_archives(start_datetime, end_datetime)
                _LOGGER.debug("End get_archives")
            except Exception as e:
                _LOGGER.debug("Skipping archive sync (non-fatal serial/encoding hiccup): %s", e)
                archives = None

        # Cached: reading the collector type costs an extra wake-up round trip.
        if not self._rain_collector:
            try:
                _LOGGER.debug("Start get_rain_collector")
                self._rain_collector = self.get_rain_collector()
                _LOGGER.debug("End get_rain_collector")
            except Exception as e:
                _LOGGER.error("Couldn't get rain_collector: %s", e)
        if self._should_close_after_transaction():
            self._protocol_client.link.close()

        self._last_readout_duration = (datetime.now() - start_readout).total_seconds()

        return data, archives, hilows

    async def async_get_current_data(self):
        """Get current date from weather station async."""
        data = self._last_data
        try:
            new_data, archives, hilows = await self._async_run_io(
                self.get_current_data
            )
            if new_data:
                new_raw_data = self.__get_full_raw_data(new_data)
                self._last_raw_data = new_raw_data
                self.remove_all_incorrect_data(new_raw_data, new_data)
                self.add_additional_info(new_data)
                self.convert_values(new_data)

                if archives:
                    self.add_archive_info(archives, new_data)
                elif self._use_loop2:
                    self.add_loop2_wind_info(new_data)
                if hilows:
                    new_raw_hilows = self.__get_full_raw_data_hilows(hilows)
                    self._last_raw_hilows = new_raw_hilows
                    self.remove_all_incorrect_hilows(new_raw_hilows, hilows)
                    self.add_hilows_info(hilows, new_data)

                data = new_data
                data["Datetime"] = self.get_iso_now()
                data["LastError"] = ""
                data["LastSuccessTime"] = self.get_iso_now()
            else:
                data["LastError"] = "Couldn't acquire data, no data received"
            data["LastReadoutDuration"] = self._last_readout_duration

        except Exception as e:
            _LOGGER.error("Couldn't acquire data from %s: %s", self.get_link(), e)
            data["LastError"] = f"Couldn't acquire data on {self.get_link()}: {e}"

        if data["LastError"]:
            data["LastErrorTime"] = self.get_iso_now()

        self._last_data = data
        return data

    def __get_full_raw_data(self, data):
        # LOOP1 and LOOP2 layouts differ; the wrong format corrupts the masking downstream.
        if self._use_loop2:
            raw_data = DataParser(data.raw_bytes, LoopData2Parser.LOOP2_FORMAT)
            raw_data["_raw_bytes"] = data.raw_bytes
            return raw_data

        raw_data = DataParser(data.raw_bytes, LoopDataParserRevB.LOOP_FORMAT)
        raw_data["HumExtra"] = struct.unpack(b"7B", raw_data["HumExtra"])
        raw_data["ExtraTemps"] = struct.unpack(b"7B", raw_data["ExtraTemps"])
        raw_data["SoilMoist"] = struct.unpack(b"4B", raw_data["SoilMoist"])
        raw_data["SoilTemps"] = struct.unpack(b"4B", raw_data["SoilTemps"])
        raw_data["LeafWetness"] = struct.unpack(b"4B", raw_data["LeafWetness"])
        raw_data["LeafTemps"] = struct.unpack(b"4B", raw_data["LeafTemps"])
        raw_data.tuple_to_dict("ExtraTemps")
        raw_data.tuple_to_dict("LeafTemps")
        raw_data.tuple_to_dict("SoilTemps")
        raw_data.tuple_to_dict("HumExtra")
        raw_data.tuple_to_dict("LeafWetness")
        raw_data.tuple_to_dict("SoilMoist")

        raw_data["_raw_bytes"] = data.raw_bytes

        return raw_data

    def __get_full_raw_data_hilows(self, data):
        raw_data = DataParser(data.raw_bytes, HighLowParserRevB.HILOWS_FORMAT)
        return raw_data

    async def _async_send_console_command(
        self, command: str, expected: bytes, *, response_size: int = 256
    ) -> str:
        """Send one command and enforce its documented response prefix."""
        def send_cmd():
            if not self._protocol_client:
                self._protocol_client = self.get_protocol_client_from_url(self.get_link())
            try:
                self._protocol_client.link.open()
                self._protocol_client.wake_up()
                self._protocol_client.link.write(command.encode("ascii"))
                response = self._protocol_client.link.read(
                    response_size, binary=True
                )
                if isinstance(response, str):
                    response = response.encode("latin-1")
                if response.startswith(b"\x21"):
                    raise DavisBadAckError("Console rejected the command (NAK)")
                if not response.startswith(expected):
                    raise DavisBadAckError(
                        f"Expected response starting with {expected!r}, got {response!r}"
                    )
                return response.decode("ascii", errors="ignore")
            finally:
                if self._should_close_after_transaction():
                    self._protocol_client.link.close()

        return await self._async_run_io(send_cmd)

    async def async_set_console_lamps(self, state: bool):
        """Turn the physical console backlight on (1) or off (0)."""
        cmd = f"LAMPS {'1' if state else '0'}\n"
        await self._async_send_console_command(cmd, b"\n\rOK\n\r")

    async def async_clear_alarms(self):
        """Clear all active alarm bits."""
        await self._async_send_console_command("CLRBITS\n", b"\x06", response_size=1)

    async def async_get_rxcheck(self):
        """Retrieve detailed connectivity diagnostics."""
        return await self._async_send_console_command("RXCHECK\n", b"\n\rOK\n\r")

    async def async_get_nver(self) -> str:
        """Get the firmware version string (Vantage Pro2/Vue only)."""
        return await self._async_send_console_command("NVER\n", b"\n\rOK\n\r")

    async def async_get_bardata(self) -> str:
        """Get the current barometer calibration parameters as text."""
        return await self._async_send_console_command("BARDATA\n", b"\n\rOK\n\r")

    async def async_set_barometer_calibration(
        self, elevation_ft: int, bar_inhg: float = 0.0
    ) -> str:
        """Set the barometer/elevation offset (BAR= command, manual sec. VIII.5)."""
        bar_value = 0 if bar_inhg == 0 else round(bar_inhg * 1000)
        cmd = f"BAR={bar_value} {elevation_ft}\n"
        return await self._async_send_console_command(cmd, b"\n\rOK\n\r")

    async def async_get_test(self) -> bool:
        """Connection sanity check (TEST)."""
        return await self._async_run_io(self.get_test)

    def get_test(self) -> bool:
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            return self._protocol_client.test()
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    async def async_get_station_type(self) -> int:
        """Return the WRD station-type byte (16=VP/Pro2, 17=Vue)."""
        return await self._async_run_io(self.get_station_type)

    def get_station_type(self) -> int:
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            return self._protocol_client.get_station_type()
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    async def async_rxtest(self) -> None:
        """Return the console to the main screen and clear the RXCHECK CRC-error count."""
        await self._async_run_io(self.rxtest)

    def rxtest(self) -> None:
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            self._protocol_client.rxtest()
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    async def async_get_receivers(self) -> int:
        """Return the RECEIVERS bitmap of station IDs the console can hear."""
        return await self._async_run_io(self.get_receivers)

    def get_receivers(self) -> int:
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            return self._protocol_client.get_receivers()
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    async def async_get_calibrated_values(self) -> str:
        """Read the 43-byte CALED block of calibrated sensor values, as hex."""
        data = await self._async_run_io(self.get_calibrated_values)
        return data.hex()

    def get_calibrated_values(self) -> bytes:
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            return self._protocol_client.get_calibrated_values()
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    async def async_set_calibrated_values(self, data_hex: str) -> None:
        """Push a 43-byte block of uncalibrated raw sensor values (CALFIX)."""
        data = bytes.fromhex(data_hex)
        await self._async_run_io(self.set_calibrated_values, data)

    def set_calibrated_values(self, data: bytes) -> None:
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            self._protocol_client.set_calibrated_values(data)
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    async def async_set_yearly_et(self, et_hundredths: int) -> None:
        """Set yearly ET in 100ths of an inch (PUTET)."""
        try:
            await self._async_run_io(self.set_yearly_et, et_hundredths)
        except Exception as e:
            _LOGGER.error("Couldn't set yearly ET: %s", e)

    def set_yearly_et(self, et_hundredths: int) -> None:
        """Set yearly ET so it reads back as et_hundredths, not the console's raw PUTET value.

        Confirmed against real hardware on 2026-09-04 (see docs/decisions.md): the console
        subtracts the current day's not-yet-finalized ET from whatever PUTET is given
        before storing it, undocumented in the manual. Sending a PUTET value 14/100" low
        when the day's ET was 0.142" was reproduced twice; compensating by the day's current
        ET here makes the stored yearly total match what the caller actually asked for.
        """
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            day_et_hundredths = round(self._protocol_client.get_current_data()["ETDay"] * 100)
            self._protocol_client.set_yearly_et(et_hundredths + day_et_hundredths)
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    def get_eeprom(self, address_hex: str, size: int) -> bytes:
        """Read `size` bytes from EEPROM starting at `address_hex` (manual sec. XIII)."""
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        self._protocol_client.link.open()
        try:
            self._protocol_client.wake_up()
            return self._protocol_client.read_from_eeprom(address_hex, size)
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    async def async_get_eeprom(self, address_hex: str, size: int) -> str:
        """Read EEPROM bytes and return them as a hex string."""
        data = await self._async_run_io(self.get_eeprom, address_hex, size)
        return data.hex()

    def set_eeprom(self, address_hex: str, data: bytes) -> None:
        """Write `data` to EEPROM starting at `address_hex` (manual sec. XIII).

        The console has no write protection; validate_eeprom_write is the only guard.
        """
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        self._protocol_client.link.open()
        try:
            self._protocol_client.wake_up()
            self._protocol_client.write_to_eeprom(address_hex, len(data), data)
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    async def async_set_eeprom(self, address_hex: str, data_hex: str) -> None:
        """Write a hex string of bytes to EEPROM starting at `address_hex`."""
        data = bytes.fromhex(data_hex)
        await self._async_run_io(self.set_eeprom, address_hex, data)

    def add_additional_info(self, data: dict[str, Any]) -> None:
        assert self._protocol_client is not None  # only called from async_get_current_data(), after get_current_data() connects
        # LOOP2 supplies DewPoint, HeatIndex and WindChill; only compute them when None (LOOP1).
        if data.get("TempOut") is not None:
            if data.get("HumOut") is not None:
                if data.get("HeatIndex") is None:
                    data["HeatIndex"] = calc_heat_index(data["TempOut"], data["HumOut"])
                if data.get("DewPoint") is None:
                    data["DewPoint"] = calc_dew_point(data["TempOut"], data["HumOut"])
            if data.get("WindSpeed") is not None:
                if data.get("WindChill") is None:
                    data["WindChill"] = calc_wind_chill(data["TempOut"], data["WindSpeed"])
                if data.get("HumOut") is not None:
                    # Prefer the console's THSW over the client-side estimate when LOOP2 provides it.
                    data["FeelsLike"] = data.get("THSWIndex")
                    if data["FeelsLike"] is None:
                        data["FeelsLike"] = calc_feels_like(
                            data["TempOut"], data["HumOut"], data["WindSpeed"]
                        )

        wind_dir = data.get("WindDir")
        wind_speed = data.get("WindSpeed", 0)

        wind_map = {
            "N": 0, "NNE": 23, "NE": 45, "ENE": 68, "E": 90, "ESE": 113,
            "SE": 135, "SSE": 158, "S": 180, "SSW": 203, "SW": 225,
            "WSW": 248, "W": 270, "WNW": 293, "NW": 315, "NNW": 338
        }

        if wind_dir is not None:
            if isinstance(wind_dir, str):
                data["WindDirRose"] = wind_dir.strip().lower()
                data["WindDir"] = wind_map.get(wind_dir.upper(), 0)
            else:
                try:
                    deg = float(wind_dir)
                    data["WindDir"] = deg
                    rose = get_wind_rose(deg)
                    data["WindDirRose"] = rose.lower() if isinstance(rose, str) else "n"
                except (ValueError, TypeError):
                    data["WindDir"] = 0
                    data["WindDirRose"] = "n"
        else:
            if wind_speed == 0:
                data["WindDir"] = 0
                data["WindDirRose"] = "n"

        # Battery status, alarm bits and forecast icon are LOOP1-only; None in LOOP2 mode.
        data["TransmitterBatteryStatus"] = data.get("BatteryStatus")
        data["ConsoleBatteryVoltage"] = data.get("BatteryVolts")
        data["FlashFloodAlarm"] = bool(data.get("AlarmRain15min"))
        data["StormRainAlarm"] = bool(data.get("AlarmRainStormTotal"))
        data["THSWAlarm"] = bool(data.get("AlarmOutHighTHSW"))

        if data.get("RainRate") is not None:
            data["IsRaining"] = data["RainRate"] > 0

        data["ArchiveInterval"] = self._protocol_client.archive_period
        data["Latitude"] = self.latitude
        data["Longitude"] = self.longitude
        data["Elevation"] = self.elevation

    def convert_values(self, data: dict[str, Any]) -> None:
        del data["Datetime"]
        if data["BarTrend"] is not None:
            data["BarTrend"] = get_baro_trend(data["BarTrend"])
        if data["UV"] is not None:
            data["UV"] = get_uv(data["UV"])
        if data["SolarRad"] is not None:
            data["SolarRad"] = get_solar_rad(data["SolarRad"])
        data["RainCollector"] = self._rain_collector
        data["StormStartDate"] = self.strtodate(data["StormStartDate"])
        data["SunRise"] = self.strtotime(data["SunRise"])
        data["SunSet"] = self.strtotime(data["SunSet"])
        self.correct_rain_values(data)

    def correct_rain_values(self, data: dict[str, Any]):
        """Convert raw rain-click counts to inches, by configured collector type.

        Parsers in protocol.py store rain fields (rate/day/month/year/storm/
        15-min/hour/24-hr) as raw click counts, not pre-scaled inches. This is
        the single point where a click count becomes inches, replacing the
        previous two-layer scale (parser assumed a 0.01" collector, then this
        method applied a second correction factor on top of that).
        """
        rain_collector_inches_per_click: dict[str, float] = {
            RAIN_COLLECTOR_IMPERIAL: 0.01,
            RAIN_COLLECTOR_METRIC: 0.2 / 25.4,
            RAIN_COLLECTOR_METRIC_0_1: 0.1 / 25.4,
        }
        factor = rain_collector_inches_per_click.get(data["RainCollector"], 0.01)
        for key in [
            "RainDay", "RainMonth", "RainYear", "RainRate", "RainStorm",
            "RainRateDay", "RainLast15Min", "RainLastHour", "RainLast24Hr",
        ]:
            if key in data and data[key] is not None:
                data[key] *= factor

    def remove_all_incorrect_data(self, raw_data, data):
        loop_format = (
            LoopData2Parser.LOOP2_FORMAT if self._use_loop2 else LoopDataParserRevB.LOOP_FORMAT
        )
        data_info = dict(loop_format)
        self.remove_incorrect_data(raw_data, data_info, data)

    def remove_all_incorrect_hilows(self, raw_data, data):
        data_info = dict(HighLowParserRevB.HILOWS_FORMAT)
        self.remove_incorrect_data(raw_data, data_info, data)

    def remove_incorrect_data(self, raw_data, data_info: dict[str, str], data: dict[str, Any]):
        for key in data:
            info_key = re.sub(r"\d+$", "", key)
            data_type = data_info.get(info_key, "")
            raw_value = raw_data.get(info_key, 0)
            if self.is_incorrect_value(raw_value, data_type):
                data[key] = None

    def is_incorrect_value(self, raw_value: int, data_type: str) -> bool:
        return bool((data_type in ["B", "7s"] and raw_value == 255) or (data_type == "H" and raw_value in [32767, 65535]) or (data_type == "h" and raw_value in [32767, -32768]))

    def add_archive_info(self, archives, data: dict[str, Any]):
        if not archives:
            return
        latest_archive = archives[-1]
        data["WindGust"] = latest_archive["WindHi"]
        data["WindSpeedAvg"] = latest_archive["WindAvg"]
        if data["WindSpeedAvg"] > 0 and latest_archive["WindAvgDir"] < 255:
            data["WindAvgDir"] = latest_archive["WindAvgDir"] * 22.5
            data["WindAvgDirRose"] = get_wind_rose(data["WindAvgDir"])
        if data["WindSpeedAvg"] is not None:
            data["WindSpeedBft"] = convert_kmh_to_bft(
                convert_to_kmh(data["WindSpeedAvg"])
            )

    def add_loop2_wind_info(self, data: dict[str, Any]):
        """Populate gust/average wind fields directly from a LOOP2 packet."""
        wind_gust = data.get("WindGust10Min")
        wind_avg = data.get("WindSpeed10Min")
        wind_gust_dir = data.get("WindGustDir10Min")

        if wind_gust is not None:
            data["WindGust"] = wind_gust
        if wind_avg is not None:
            data["WindSpeedAvg"] = wind_avg
            data["WindSpeedBft"] = convert_kmh_to_bft(convert_to_kmh(wind_avg))
        # LOOP2 has no 10-minute average direction, only the gust direction; not WindAvgDir.
        if wind_gust_dir not in (None, 0, 32767):
            data["WindGustDir"] = wind_gust_dir
            rose = get_wind_rose(wind_gust_dir)
            data["WindGustDirRose"] = rose.lower() if isinstance(rose, str) else "n"

    def add_hilows_info(self, hilows, data: dict[str, Any]):
        if not hilows:
            return
        data["TempOutHiDay"] = hilows["TempHiDay"]
        data["TempOutHiTime"] = self.strtotime(hilows["TempHiTime"])
        data["TempOutLowDay"] = hilows["TempLoDay"]
        data["TempOutLowTime"] = self.strtotime(hilows["TempLoTime"])

        data["DewPointHiDay"] = hilows["DewHiDay"]
        data["DewPointHiTime"] = self.strtotime(hilows["DewHiTime"])
        data["DewPointLowDay"] = hilows["DewLoDay"]
        data["DewPointLowTime"] = self.strtotime(hilows["DewLoTime"])

        data["RainRateDay"] = hilows["RainHiDay"]
        data["RainRateTime"] = self.strtotime(hilows["RainHiTime"])

        data["BarometerHiDay"] = hilows["BaroHiDay"]
        data["BarometerHiTime"] = self.strtotime(hilows["BaroHiTime"])
        data["BarometerLowDay"] = hilows["BaroLoDay"]
        data["BarometerLoTime"] = self.strtotime(hilows["BaroLoTime"])

        data["SolarRadDay"] = hilows["SolarHiDay"]
        data["SolarRadTime"] = self.strtotime(hilows["SolarHiTime"])

        data["UVDay"] = hilows["UVHiDay"]
        data["UVTime"] = self.strtotime(hilows["UVHiTime"])

        data["WindGustDay"] = hilows["WindHiDay"]
        data["WindGustTime"] = self.strtotime(hilows["WindHiTime"])

        # Extra temp (7) / soil temp (4) / leaf temp (4): day high/low only.
        # Month/year hi/lo values are decoded onto the hilows dict itself and
        # available via get_raw_data()/diagnostics for advanced use; see
        # docs/backlog.md for the scoping note on why sensors are day-only.
        for sensor in range(2, 9):
            index = f"{sensor:02d}"
            data[f"ExtraTemp{index}Hi"] = hilows.get(f"ExtraTemp{index}DayHi")
            data[f"ExtraTemp{index}Low"] = hilows.get(f"ExtraTemp{index}DayLow")
        for sensor in range(1, 5):
            index = f"{sensor:02d}"
            data[f"SoilTemp{index}Hi"] = hilows.get(f"SoilTemp{index}DayHi")
            data[f"SoilTemp{index}Low"] = hilows.get(f"SoilTemp{index}DayLow")
            data[f"LeafTemp{index}Hi"] = hilows.get(f"LeafTemp{index}DayHi")
            data[f"LeafTemp{index}Low"] = hilows.get(f"LeafTemp{index}DayLow")
            data[f"SoilMoist{index}Hi"] = hilows.get(f"SoilMoist{index}DayHi")
            data[f"SoilMoist{index}Low"] = hilows.get(f"SoilMoist{index}DayLow")
            data[f"LeafWet{index}Hi"] = hilows.get(f"LeafWet{index}DayHi")
            data[f"LeafWet{index}Low"] = hilows.get(f"LeafWet{index}DayLow")
        for sensor in range(2, 9):
            index = f"{sensor:02d}"
            data[f"ExtraHum{index}Hi"] = hilows.get(f"ExtraHum{index}DayHi")
            data[f"ExtraHum{index}Low"] = hilows.get(f"ExtraHum{index}DayLow")

    def get_link(self) -> str:
        return f"serial:{self._link}:{self._baud_rate}:8N1"

    def get_raw_data(self):
        return self._last_raw_data

    def get_raw_hilows(self):
        return self._last_raw_hilows

    def strtotime(self, time_str: str | None) -> dt_time | None:
        if time_str is None:
            return None
        else:
            return datetime.strptime(time_str, "%H:%M").time()

    def strtodate(self, date_str: str | None) -> date | None:
        if date_str is None:
            return None
        else:
            return datetime.strptime(date_str, "%Y-%m-%d").date()

    def clear_cached_property(self, property_name: str) -> None:
        """Invalidate a functools.cached_property on the underlying protocol client."""
        if self._protocol_client is not None:
            self._protocol_client.__dict__.pop(property_name, None)

    def get_rain_collector(self) -> str:
        rain_collector_map = {
            0x00: RAIN_COLLECTOR_IMPERIAL,
            0x10: RAIN_COLLECTOR_METRIC,
            0x20: RAIN_COLLECTOR_METRIC_0_1,
        }
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            self._protocol_client.wake_up()
            rain_collector = self._protocol_client.get_rain_collector()
            return rain_collector_map.get(rain_collector, "")
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    async def async_get_rain_collector(self) -> str:
        info = ""
        try:
            info = await self._async_run_io(self.get_rain_collector)
        except Exception as e:
            _LOGGER.error("Couldn't get rain collector: %s", e)
        return info

    def set_rain_collector(self, rain_collector: str):
        rain_collector_map = {
            RAIN_COLLECTOR_IMPERIAL: 0x00,
            RAIN_COLLECTOR_METRIC: 0x10,
            RAIN_COLLECTOR_METRIC_0_1: 0x20,
        }
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            self._protocol_client.set_rain_collector(
                rain_collector_map.get(rain_collector, 0x00)
            )
            # Drop the cached collector type so the next poll re-reads it.
            self._rain_collector = ""
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    async def async_set_rain_collector(self, rain_collector: str):
        await self._async_run_io(self.set_rain_collector, rain_collector)

    def get_latitude_longitude_elevation(self) -> tuple[float, float, int]:
        latitude = longitude = None
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            data = self._protocol_client.read_from_eeprom("0B", 6)
            latitude, longitude, elevation = struct.unpack(b"hhh", data)
            latitude /= 10
            longitude /= 10
            return latitude, longitude, elevation
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    async def async_get_latitude_longitude_elevation(self):
        latitude = longitude = elevation = None
        try:
            latitude, longitude, elevation = await self._async_run_io(
                self.get_latitude_longitude_elevation
            )
        except Exception as e:
            _LOGGER.error("Couldn't get latitude longitude: %s", e)
        return latitude, longitude, elevation

    def get_davis_time(self) -> datetime | None:
        data = None
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            data = self._protocol_client.gettime()
        except Exception as e:
            raise e
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()
        return data

    async def async_get_davis_time(self) -> datetime | None:
        data = None
        try:
            data = await self._async_run_io(self.get_davis_time)
        except Exception as e:
            _LOGGER.error("Couldn't get davis time: %s", e)
        return data

    def set_davis_time(self, dtime: datetime) -> None:
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            self._protocol_client.settime(dtime)
        except Exception as e:
            raise e
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    async def async_set_davis_time(self) -> None:
        try:
            await self._async_run_io(self.set_davis_time, datetime.now())
        except Exception as e:
            _LOGGER.error("Couldn't set davis time: %s", e)

    def get_info(self) -> dict[str, Any] | None:
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            firmware_version = self._protocol_client.firmware_version
            firmware_date = self._protocol_client.firmware_date
            diagnostics = self._protocol_client.diagnostics
        except Exception as e:
            raise e
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()
        return {
            "version": firmware_version,
            "date": firmware_date,
            "diagnostics": diagnostics,
        }

    async def async_get_info(self) -> dict[str, Any] | None:
        info = None
        try:
            info = await self._async_run_io(self.get_info)
        except Exception as e:
            _LOGGER.error("Couldn't get firmware info: %s", e)
        return info

    def get_static_info(self) -> dict[str, Any] | None:
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            firmware_version = self._protocol_client.firmware_version
            archive_period = self._protocol_client.archive_period
        except Exception as e:
            raise e
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()
        return {"version": firmware_version, "archive_period": archive_period}

    async def async_get_static_info(self) -> dict[str, Any] | None:
        info = None
        try:
            info = await self._async_run_io(self.get_static_info)
        except Exception as e:
            _LOGGER.error("Couldn't get static info: %s", e)
        return info

    def set_yearly_rain(self, rain_clicks: int) -> None:
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            self._protocol_client.set_yearly_rain(rain_clicks)
        except Exception as e:
            raise e
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    async def async_set_yearly_rain(self, rain_clicks: int) -> None:
        try:
            await self._async_run_io(self.set_yearly_rain, rain_clicks)
        except Exception as e:
            _LOGGER.error("Couldn't set yearly rain: %s", e)

    def set_archive_period(self, archive_period: int) -> None:
        if not self._protocol_client:
            self._protocol_client = self.get_protocol_client_from_url(self.get_link())
        try:
            self._protocol_client.link.open()
            self._protocol_client.set_archive_period(archive_period)
        except Exception as e:
            raise e
        finally:
            if self._should_close_after_transaction():
                self._protocol_client.link.close()

    async def async_set_archive_period(self, archive_period: int) -> None:
        await self._async_run_io(self.set_archive_period, archive_period)

    async def async_begin_shutdown(self) -> None:
        """Reject new work before platforms and services begin unloading."""
        async with self._submit_lock:
            self._stopping = True
            self._connection_state = CONNECTION_STOPPING

    async def async_cancel_shutdown(self) -> None:
        """Resume normal operation after a begun shutdown was aborted."""
        async with self._submit_lock:
            if self._executor_shutdown:
                return
            self._stopping = False
            self._connection_state = (
                CONNECTION_CONNECTED if self._last_success else CONNECTION_DISCONNECTED
            )

    async def async_close(self) -> bool:
        """Stop new work, drain physical ownership, then close the transport."""
        async with self._submit_lock:
            if self._executor_shutdown:
                return self._last_close_result == "closed"
            self._stopping = True
            self._connection_state = CONNECTION_STOPPING
            pending = tuple(self._pending_io)

        try:
            async with asyncio.timeout(DEFAULT_SHUTDOWN_TIMEOUT):
                if pending:
                    await asyncio.gather(
                        *(asyncio.shield(future) for future in pending),
                        return_exceptions=True,
                    )
                close_future = asyncio.wrap_future(
                    self._executor.submit(self._close_transport_sync)
                )
                await asyncio.shield(close_future)
        except TimeoutError:
            self._last_close_result = "timeout_waiting_for_in_flight_io"
            _LOGGER.error("Timed out waiting for Davis transport shutdown")
            return False
        except Exception as err:
            self._last_close_result = f"close_failed: {err}"
            _LOGGER.error("Error closing Davis station connection: %s", err)
            self._executor.shutdown(wait=True, cancel_futures=False)
            self._executor_shutdown = True
            self._connection_state = CONNECTION_DEGRADED
            return False

        self._protocol_client = None
        self._executor.shutdown(wait=False, cancel_futures=False)
        self._executor_shutdown = True
        self._connection_state = CONNECTION_CLOSED
        self._last_close_result = "closed"
        return True

    def get_iso_now(self) -> datetime:
        now = convert_to_iso_datetime(
            datetime.now(), ZoneInfo(self._hass.config.time_zone)
        )
        return now
