"""All client function"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import struct
import re
from datetime import datetime, time, date, timedelta
from typing import TYPE_CHECKING, Any
import logging

from zoneinfo import ZoneInfo
from pyvantagepro import VantagePro2
from pyvantagepro.device import BadAckException
from pyvantagepro.link import link_from_url
from pyvantagepro.parser import HighLowParserRevB, LoopDataParserRevB, DataParser

if TYPE_CHECKING:
    from serialx import BaseSerial

from .utils import (
    calc_dew_point,
    calc_feels_like,
    calc_wind_chill,
    calc_heat_index,
    convert_kmh_to_bft,
    convert_to_iso_datetime,
    convert_to_kmh,
    get_baro_trend,
    get_solar_rad,
    get_uv,
    get_wind_rose,
)
from .const import (
    CONNECTION_CLOSED,
    CONNECTION_CONNECTED,
    CONNECTION_CONNECTING,
    CONNECTION_DEGRADED,
    CONNECTION_DISCONNECTED,
    CONNECTION_RECONNECTING,
    CONNECTION_STOPPING,
    DEFAULT_SHUTDOWN_TIMEOUT,
    RAIN_COLLECTOR_IMPERIAL,
    RAIN_COLLECTOR_METRIC,
    RAIN_COLLECTOR_METRIC_0_1,
    PROTOCOL_NETWORK,
    DEFAULT_BAUD_RATE,
)
from .protocol import DavisProtocolClient, validate_loop_frame

_LOGGER = logging.getLogger(__name__)


class DavisSerialXLink:
    """PyVantagePro-compatible link backed by serialx.serial_for_url()."""

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
        """Read data using the legacy PyVantagePro link contract."""
        self.open()
        assert self._serial is not None
        previous_timeout = self._serial.timeout
        self._serial.timeout = timeout or self.timeout
        try:
            data = self._serial.read(size or 4048)
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


class LoopData2Parser(LoopDataParserRevB):
    """Parse a LOOP2 packet, sent by the "LPS 2 1" command.

    PyVantagePro only implements the LOOP1 packet format, so this mirrors
    LoopDataParserRevB's approach (byte layout + unit scaling) for LOOP2,
    per the Davis Vantage Serial Communication Reference Manual Rev 2.6.1
    (section IX.2). Subclasses LoopDataParserRevB purely to reuse its
    unpack_storm_date()/unpack_time() helpers; its LOOP1 __init__ is
    intentionally not called.
    """

    LOOP2_FORMAT = (
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
        ("Unused6", "2s"),
        ("GraphPointers", "10s"),
        ("Unused7", "12s"),
        ("EOL", "2s"),
        ("CRC", "H"),
    )

    def __init__(self, data: bytes) -> None:
        # Deliberately skip LoopDataParserRevB.__init__ (LOOP1 format) and
        # go straight to the base DataParser with our own LOOP2 format.
        DataParser.__init__(self, data, self.LOOP2_FORMAT, order="<")

        self["Barometer"] = self["Barometer"] / 1000
        self["TempIn"] = self["TempIn"] / 10
        self["TempOut"] = self["TempOut"] / 10
        self["WindSpeed10Min"] = self["WindSpeed10Min"] / 10
        self["WindSpeed2Min"] = self["WindSpeed2Min"] / 10
        self["WindGust10Min"] = self["WindGust10Min"] / 10
        for key in ("DewPoint", "HeatIndex", "WindChill", "THSWIndex"):
            self[key] = None if self[key] == 255 else self[key]
        self["RainRate"] = self["RainRate"] / 100
        self["UV"] = self["UV"] / 10
        self["RainStorm"] = self["RainStorm"] / 100
        self["StormStartDate"] = self.unpack_storm_date(self["StormStartDate"])
        self["RainDay"] = self["RainDay"] / 100
        self["RainLast15Min"] = self["RainLast15Min"] / 100
        self["RainLastHour"] = self["RainLastHour"] / 100
        self["ETDay"] = self["ETDay"] / 1000
        self["RainLast24Hr"] = self["RainLast24Hr"] / 100

        # LOOP2 carries no alarm bits, battery status, forecast icon, or
        # sunrise/sunset (those are LOOP1-only). Fill them in as None so
        # downstream code that expects the keys doesn't KeyError.
        self["SunRise"] = None
        self["SunSet"] = None


class DavisVantageClient:
    """Davis Vantage Client class"""

    _vantagepro2: VantagePro2 | None = None
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
        # One entry owns one single-worker executor. Cancelling an HA waiter
        # never cancels or releases ownership of the blocking Davis operation.
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
        """WeatherLink TCP must periodically release port 22222 for cloud uploads."""
        return not self._persistent_connection or self._protocol == PROTOCOL_NETWORK

    def _close_transport_sync(self) -> None:
        """Close without constructing a lazy transport."""
        if self._vantagepro2 is not None:
            self._vantagepro2.link.close()

    def _execute_owned(self, func, args: tuple[Any, ...]) -> Any:
        """Execute one operation on the dedicated worker."""
        if self._needs_reconnect:
            self._connection_state = CONNECTION_RECONNECTING
            try:
                self._close_transport_sync()
            except (OSError, TimeoutError):
                pass
            self._vantagepro2 = None
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
            try:
                self._close_transport_sync()
            except (OSError, TimeoutError):
                pass
            self._vantagepro2 = None
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
        """Bridge to the underlying PyVantagePro link object."""
        if not self._vantagepro2:
            self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())

        return self._vantagepro2.link

    def get_vantagepro2fromurl(self, url: str) -> VantagePro2:
        if self._protocol == PROTOCOL_NETWORK:
            network_link = link_from_url(url)
            network_link.settimeout(10)
            vp = DavisProtocolClient(network_link)
        else:
            serial_link = DavisSerialXLink(self._link, self._baud_rate)
            serial_link.settimeout(10)
            vp = DavisProtocolClient(serial_link)
        if self._should_close_after_transaction():
            vp.link.close()
        return vp

    async def async_get_vantagepro2fromurl(self, url: str):
        _LOGGER.debug("async_get_vantagepro2fromurl with url=%s", url)
        try:
            return await self._async_run_io(self.get_vantagepro2fromurl, url)
        except Exception as err:
            _LOGGER.error("Error on opening device from url: %s: %s", url, err)
            return None

    async def connect_to_station(self) -> None:
        """Connect to the Davis station and perform a real wake exchange."""
        self._connection_state = CONNECTION_CONNECTING

        def _connect() -> None:
            if self._vantagepro2 is None:
                self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
            self._vantagepro2.link.open()
            self._vantagepro2.wake_up()
            if self._should_close_after_transaction():
                self._vantagepro2.link.close()

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

        if not self._vantagepro2:
            self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())

        try:
            self._vantagepro2.link.open()
            
            if self._use_loop2:
                _LOGGER.debug("Start get_current_data (LOOP 2 requested)")
                data = self._get_loop2_data()
            else:
                _LOGGER.debug("Start get_current_data (LOOP 1)")
                data = self._vantagepro2.get_current_data()
                
            _LOGGER.debug("End get_current_data:")
        except Exception as e:
            if self._should_close_after_transaction():
                self._vantagepro2.link.close()
            raise e

        try:
            _LOGGER.debug("Start get_hilows")
            hilows = self._vantagepro2.get_hilows()
            _LOGGER.debug("End get_hilows")
        except Exception as e:
            _LOGGER.error("Couldn't get hilows: %s", e)
            
        # LOOP2 already carries a rolling 10-min wind gust + average natively
        # (see add_loop2_wind_info), so the archive fetch below - otherwise
        # needed purely to derive gust/average wind from DMPAFT - would just
        # be a redundant round trip. Skip it in LOOP2 mode.
        if not self._use_loop2:
            try:
                end_datetime = datetime.now()
                start_datetime = end_datetime - timedelta(
                    minutes=self._vantagepro2.archive_period * 2
                )
                _LOGGER.debug("Start get_archives")
                archives = self._vantagepro2.get_archives(start_datetime, end_datetime)
                _LOGGER.debug("End get_archives")
            except Exception as e:
                _LOGGER.debug("Skipping archive sync (non-fatal serial/encoding hiccup): %s", e)
                archives = None

        # The rain collector type rarely changes and reading it costs an extra
        # wake-up + serial round trip. Fetch it once and reuse the cached value
        # on subsequent polls to keep per-poll latency down.
        if not self._rain_collector:
            try:
                _LOGGER.debug("Start get_rain_collector")
                self._rain_collector = self.get_rain_collector()
                _LOGGER.debug("End get_rain_collector")
            except Exception as e:
                _LOGGER.error("Couldn't get rain_collector: %s", e)
        if self._should_close_after_transaction():
            self._vantagepro2.link.close()

        self._last_readout_duration = (datetime.now() - start_readout).total_seconds()

        return data, archives, hilows

    def _get_loop2_data(self) -> "LoopData2Parser":
        """Request and parse a single LOOP2 packet.

        PyVantagePro has no LOOP2 support, so this sends "LPS 2 1" directly
        (mirroring VantagePro2.get_current_data()'s own wake_up/send/read
        pattern) and parses the response with LoopData2Parser.
        """
        assert self._vantagepro2 is not None  # only called from get_current_data(), after it connects
        self._vantagepro2.wake_up()
        self._vantagepro2.send("LPS 2 1", self._vantagepro2.ACK)
        raw_data = self._vantagepro2.link.read(99, binary=True)
        if not isinstance(raw_data, bytes):
            raw_data = raw_data.encode("latin-1")
        validate_loop_frame(raw_data, 1)
        return LoopData2Parser(raw_data)

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
        # LOOP1 and LOOP2 packets have different byte layouts (see
        # LoopData2Parser) - always parsing with the LOOP1 format here would
        # silently misinterpret LOOP2 bytes and could corrupt the
        # incorrect-value masking done downstream in remove_all_incorrect_data.
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

        # EXPOSE RAW BYTES FOR HARDWARE DIAGNOSTICS
        raw_data["_raw_bytes"] = data.raw_bytes

        return raw_data

    def __get_full_raw_data_hilows(self, data):
        raw_data = DataParser(data.raw_bytes, HighLowParserRevB.HILOWS_FORMAT)  
        return raw_data

    # --- DEVICE ACTION METHODS ---
    async def _async_send_console_command(
        self, command: str, expected: bytes, *, response_size: int = 256
    ) -> str:
        """Send one command and enforce its documented response prefix."""
        def send_cmd():
            if not self._vantagepro2:
                self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
            try:
                self._vantagepro2.link.open()
                self._vantagepro2.wake_up()
                self._vantagepro2.link.write(command.encode("ascii"))
                response = self._vantagepro2.link.read(
                    response_size, binary=True
                )
                if isinstance(response, str):
                    response = response.encode("latin-1")
                if response.startswith(b"\x21"):
                    raise BadAckException()
                if not response.startswith(expected):
                    raise BadAckException()
                return response.decode("ascii", errors="ignore")
            finally:
                if self._should_close_after_transaction():
                    self._vantagepro2.link.close()

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
        """Set the barometer/elevation offset (BAR= command, manual sec. VIII.5).

        `bar_inhg`: a known-good local barometer reading (20.000-32.500 inHg)
        to fine-tune the console's own adjusted-pressure calculation, or 0 to
        clear any existing offset. `elevation_ft`: station elevation
        (-2000 to 15000 ft) - the primary correction, always required.
        """
        bar_value = 0 if bar_inhg == 0 else round(bar_inhg * 1000)
        cmd = f"BAR={bar_value} {elevation_ft}\n"
        return await self._async_send_console_command(cmd, b"\n\rOK\n\r")

    def get_eeprom(self, address_hex: str, size: int) -> bytes:
        """Read `size` bytes from EEPROM starting at `address_hex` (manual sec. XIII)."""
        if not self._vantagepro2:
            self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
        self._vantagepro2.link.open()
        try:
            self._vantagepro2.wake_up()
            return self._vantagepro2.read_from_eeprom(address_hex, size)
        finally:
            if self._should_close_after_transaction():
                self._vantagepro2.link.close()

    async def async_get_eeprom(self, address_hex: str, size: int) -> str:
        """Read EEPROM bytes and return them as a hex string."""
        data = await self._async_run_io(self.get_eeprom, address_hex, size)
        return data.hex()

    def set_eeprom(self, address_hex: str, data: bytes) -> None:
        """Write `data` to EEPROM starting at `address_hex` (manual sec. XIII).

        Advanced/expert use only: several EEPROM locations are factory
        calibration values that should never be written (see the manual's
        EEPROM address table), and writing the wrong bytes to the wrong
        address can corrupt console settings. There is no hardware
        protection against this - the console will accept whatever is sent.
        """
        if not self._vantagepro2:
            self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
        self._vantagepro2.link.open()
        try:
            self._vantagepro2.wake_up()
            self._vantagepro2.write_to_eeprom(address_hex, len(data), data)
        finally:
            if self._should_close_after_transaction():
                self._vantagepro2.link.close()

    async def async_set_eeprom(self, address_hex: str, data_hex: str) -> None:
        """Write a hex string of bytes to EEPROM starting at `address_hex`."""
        data = bytes.fromhex(data_hex)
        await self._async_run_io(self.set_eeprom, address_hex, data)
    # -----------------------------

    def add_additional_info(self, data: dict[str, Any]) -> None:
        assert self._vantagepro2 is not None  # only called from async_get_current_data(), after get_current_data() connects
        # LOOP2 packets already carry console-computed DewPoint, HeatIndex,
        # and WindChill (LoopData2Parser) - prefer those over the client-side
        # formulas below rather than clobbering real values with estimates.
        # LOOP1 has none of these, so they're always None going in and the
        # formulas fill them in as before.
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
                    # THSW (Temp-Humidity-Sun-Wind) is Davis's own "feels
                    # like" figure and is strictly better than the generic
                    # calc_feels_like() estimate when LOOP2 provides it.
                    data["FeelsLike"] = data.get("THSWIndex")
                    if data["FeelsLike"] is None:
                        data["FeelsLike"] = calc_feels_like(
                            data["TempOut"], data["HumOut"], data["WindSpeed"]
                        )
                    
        # --- ROBUST WIND DIRECTION & ROSE SYNC FIX ---
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
        # ---------------------------------------------
        
        # --- HARDWARE DIAGNOSTICS & ALARMS ---
        # PyVantagePro's LOOP1 parser already decodes these into named fields
        # (see LoopDataParserRevB.__init__ in the pyvantagepro fork) - no need
        # to re-derive them from raw byte offsets. LOOP2 packets don't carry
        # battery status, alarm bits, or a forecast icon at all (see the
        # Rev 2.6.1 manual's LOOP2 packet layout), so these are simply absent
        # (None) from `data` in that mode, which downstream bool()/get() calls
        # already handle.
        data["TransmitterBatteryStatus"] = data.get("BatteryStatus")
        data["ConsoleBatteryVoltage"] = data.get("BatteryVolts")
        data["FlashFloodAlarm"] = bool(data.get("AlarmRain15min"))
        data["StormRainAlarm"] = bool(data.get("AlarmRainStormTotal"))
        data["THSWAlarm"] = bool(data.get("AlarmOutHighTHSW"))
        # ------------------------------------------------

        if data.get("RainRate") is not None:
            data["IsRaining"] = data["RainRate"] > 0
            
        data["ArchiveInterval"] = self._vantagepro2.archive_period
        data["Latitude"] = self.latitude
        data["Longitude"] = self.longitude
        data["Elevation"] = self.elevation

    # ... [Keep all your other existing methods exactly as they are: convert_values, correct_rain_values, etc.]
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
        rain_collector_factor: dict[str, float] = {
            RAIN_COLLECTOR_IMPERIAL: 1.0,
            RAIN_COLLECTOR_METRIC: 2 / 2.54,
            RAIN_COLLECTOR_METRIC_0_1: 1 / 2.54,
        }
        factor = rain_collector_factor.get(data["RainCollector"], 1.0)
        for key in ["RainDay", "RainMonth", "RainYear", "RainRate", "RainStorm", "RainRateDay"]:
            if key in data and data[key] is not None:
                data[key] *= factor

    def remove_all_incorrect_data(self, raw_data, data):
        loop_format = (
            LoopData2Parser.LOOP2_FORMAT if self._use_loop2 else LoopDataParserRevB.LOOP_FORMAT
        )
        data_info = {key: value for key, value in loop_format}
        self.remove_incorrect_data(raw_data, data_info, data)

    def remove_all_incorrect_hilows(self, raw_data, data):
        data_info = {key: value for key, value in HighLowParserRevB.HILOWS_FORMAT}
        self.remove_incorrect_data(raw_data, data_info, data)

    def remove_incorrect_data(self, raw_data, data_info: dict[str, str], data: dict[str, Any]):
        for key in data.keys(): 
            info_key = re.sub(r"\d+$", "", key) 
            data_type = data_info.get(info_key, "")
            raw_value = raw_data.get(info_key, 0) 
            if self.is_incorrect_value(raw_value, data_type): 
                data[key] = None 

    def is_incorrect_value(self, raw_value: int, data_type: str) -> bool:
        if (
            ((data_type in ["B", "7s"]) and (raw_value == 255))
            or ((data_type == "H") and (raw_value in [32767, 65535]))
            or ((data_type == "h") and (raw_value in [32767, -32768]))
        ):
            return True
        else:
            return False

    def add_archive_info(self, archives, data: dict[str, Any]):
        if not archives:
            return
        latest_archive = archives[-1]
        data["WindGust"] = latest_archive["WindHi"]
        data["WindSpeedAvg"] = latest_archive["WindAvg"]
        if data["WindSpeedAvg"] > 0:
            if latest_archive["WindAvgDir"] < 255:
                data["WindAvgDir"] = latest_archive["WindAvgDir"] * 22.5
                data["WindAvgDirRose"] = get_wind_rose(data["WindAvgDir"])
        if data["WindSpeedAvg"] is not None:
            data["WindSpeedBft"] = convert_kmh_to_bft(
                convert_to_kmh(data["WindSpeedAvg"])
            )

    def add_loop2_wind_info(self, data: dict[str, Any]):
        """Populate gust/average wind fields directly from a LOOP2 packet.

        LOOP2 already carries a rolling 10-minute wind gust and average
        (see LoopData2Parser), which is what add_archive_info() otherwise
        derives from a DMPAFT archive fetch - so this replaces that round
        trip in LOOP2 mode instead of duplicating it.
        """
        wind_gust = data.get("WindGust10Min")
        wind_avg = data.get("WindSpeed10Min")
        wind_gust_dir = data.get("WindGustDir10Min")

        if wind_gust is not None:
            data["WindGust"] = wind_gust
        if wind_avg is not None:
            data["WindSpeedAvg"] = wind_avg
            data["WindSpeedBft"] = convert_kmh_to_bft(convert_to_kmh(wind_avg))
        # LOOP2 has no true 10-minute *average* direction field, only the
        # direction of the 10-minute gust - expose it under its own name
        # rather than mislabeling it as "WindAvgDir".
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

    def get_link(self) -> str:
        if self._protocol == PROTOCOL_NETWORK:
            return f"tcp:{self._link}"
        return f"serial:{self._link}:{self._baud_rate}:8N1"

    def get_raw_data(self):
        return self._last_raw_data

    def get_raw_hilows(self):
        return self._last_raw_hilows

    def strtotime(self, time_str: str | None) -> time | None:
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
        """Invalidate a functools.cached_property on the underlying VantagePro2.

        Safe to call even if the property was never accessed yet (nothing
        cached) or no connection has been made yet (_vantagepro2 is None) -
        the caller only wants "make sure it's not stale", not an error.
        """
        if self._vantagepro2 is not None:
            self._vantagepro2.__dict__.pop(property_name, None)

    def get_rain_collector(self) -> str:
        rain_collector_map = {
            0x00: RAIN_COLLECTOR_IMPERIAL,
            0x10: RAIN_COLLECTOR_METRIC,
            0x20: RAIN_COLLECTOR_METRIC_0_1,
        }
        if not self._vantagepro2:
            self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
        try:
            self._vantagepro2.link.open()
            self._vantagepro2.wake_up()
            rain_collector = self._vantagepro2.get_rain_collector()
            return rain_collector_map.get(rain_collector, "")
        finally:
            if self._should_close_after_transaction():
                self._vantagepro2.link.close()

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
        if not self._vantagepro2:
            self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
        try:
            self._vantagepro2.link.open()
            self._vantagepro2.set_rain_collector(
                rain_collector_map.get(rain_collector, 0x00)
            )
            # Invalidate the cache so the next poll re-reads the new setting.
            self._rain_collector = ""
        finally:
            if self._should_close_after_transaction():
                self._vantagepro2.link.close()

    async def async_set_rain_collector(self, rain_collector: str):
        await self._async_run_io(self.set_rain_collector, rain_collector)

    def get_latitude_longitude_elevation(self) -> tuple[float, float, int]:
        latitude = longitude = None
        if not self._vantagepro2:
            self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
        try:
            self._vantagepro2.link.open()
            data = self._vantagepro2.read_from_eeprom("0B", 6)
            latitude, longitude, elevation = struct.unpack(b"hhh", data)
            latitude /= 10
            longitude /= 10
            return latitude, longitude, elevation
        finally:
            if self._should_close_after_transaction():
                self._vantagepro2.link.close()

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
        if not self._vantagepro2:
            self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
        try:
            self._vantagepro2.link.open()
            data = self._vantagepro2.gettime()
        except Exception as e:
            raise e
        finally:
            if self._should_close_after_transaction():
                self._vantagepro2.link.close()
        return data

    async def async_get_davis_time(self) -> datetime | None:
        data = None
        try:
            data = await self._async_run_io(self.get_davis_time)
        except Exception as e:
            _LOGGER.error("Couldn't get davis time: %s", e)
        return data

    def set_davis_time(self, dtime: datetime) -> None:
        if not self._vantagepro2:
            self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
        try:
            self._vantagepro2.link.open()
            self._vantagepro2.settime(dtime)
        except Exception as e:
            raise e
        finally:
            if self._should_close_after_transaction():
                self._vantagepro2.link.close()

    async def async_set_davis_time(self) -> None:
        try:
            await self._async_run_io(self.set_davis_time, datetime.now())
        except Exception as e:
            _LOGGER.error("Couldn't set davis time: %s", e)

    def get_info(self) -> dict[str, Any] | None:
        if not self._vantagepro2:
            self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
        try:
            self._vantagepro2.link.open()
            firmware_version = self._vantagepro2.firmware_version
            firmware_date = self._vantagepro2.firmware_date
            diagnostics = self._vantagepro2.diagnostics
        except Exception as e:
            raise e
        finally:
            if self._should_close_after_transaction():
                self._vantagepro2.link.close()
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
        if not self._vantagepro2:
            self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
        try:
            self._vantagepro2.link.open()
            firmware_version = self._vantagepro2.firmware_version
            archive_period = self._vantagepro2.archive_period
        except Exception as e:
            raise e
        finally:
            if self._should_close_after_transaction():
                self._vantagepro2.link.close()
        return {"version": firmware_version, "archive_period": archive_period}

    async def async_get_static_info(self) -> dict[str, Any] | None:
        info = None
        try:
            info = await self._async_run_io(self.get_static_info)
        except Exception as e:
            _LOGGER.error("Couldn't get static info: %s", e)
        return info

    def set_yearly_rain(self, rain_clicks: int) -> None:
        if not self._vantagepro2:
            self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
        try:
            self._vantagepro2.link.open()
            self._vantagepro2.set_yearly_rain(rain_clicks)
        except Exception as e:
            raise e
        finally:
            if self._should_close_after_transaction():
                self._vantagepro2.link.close()

    async def async_set_yearly_rain(self, rain_clicks: int) -> None:
        try:
            await self._async_run_io(self.set_yearly_rain, rain_clicks)
        except Exception as e:
            _LOGGER.error("Couldn't set yearly rain: %s", e)

    def set_archive_period(self, archive_period: int) -> None:
        if not self._vantagepro2:
            self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
        try:
            self._vantagepro2.link.open()
            self._vantagepro2.set_archive_period(archive_period)
        except Exception as e:
            raise e
        finally:
            if self._should_close_after_transaction():
                self._vantagepro2.link.close()

    async def async_set_archive_period(self, archive_period: int) -> None:
        await self._async_run_io(self.set_archive_period, archive_period)

    async def async_begin_shutdown(self) -> None:
        """Reject new work before platforms and services begin unloading."""
        async with self._submit_lock:
            self._stopping = True
            self._connection_state = CONNECTION_STOPPING

    async def async_cancel_shutdown(self) -> None:
        """Resume normal operation after a begun shutdown was aborted.

        Used when async_begin_shutdown() ran but the platform unload it was
        guarding then failed, so async_close() never followed - without this,
        _stopping would stay True forever and every future poll would raise
        "Davis transport is stopping" even though HA still considers the
        entry loaded.
        """
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

        self._vantagepro2 = None
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
