"""All client function"""
import asyncio
import struct
import re
from datetime import datetime, time, date, timedelta
from typing import Any
import logging

from functools import cached_property
from zoneinfo import ZoneInfo
from pyvantagepro import VantagePro2
from pyvantagepro.parser import HighLowParserRevB, LoopDataParserRevB, DataParser
from pyvantagepro.utils import ListDict
from homeassistant.core import HomeAssistant

from .utils import (
    calc_dew_point,
    calc_feels_like,
    calc_wind_chill,
    contains_correct_raw_data,
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
    RAIN_COLLECTOR_IMPERIAL,
    RAIN_COLLECTOR_METRIC,
    RAIN_COLLECTOR_METRIC_0_1,
    PROTOCOL_NETWORK,
    DEFAULT_BAUD_RATE,
)

_LOGGER = logging.getLogger(__name__)

class DavisVantageClient:
    """Davis Vantage Client class"""

    _vantagepro2 = None  # type: ignore
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
        self._use_loop2 = use_loop2  # CRITICAL FIX: Uncommented
        self._baud_rate = baud_rate or DEFAULT_BAUD_RATE

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
        """Bridge to the underlying PyVantagePro link object for LOOP2 monkey patches."""
        if not self._vantagepro2:
            self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
            self._vantagepro2.link.wakeup = self._vantagepro2.wake_up

        return self._vantagepro2.link

    @property
    def parser(self):
        """Bridge to parser for Loop 2 monkey patches."""
        return LoopDataParserRevB

    def get_vantagepro2fromurl(self, url: str):
        try:
            vp = VantagePro2.from_url(url)
            if not self._persistent_connection:
                vp.link.close()
            return vp
        except Exception as e:
            raise e

    async def async_get_vantagepro2fromurl(self, url: str):
        _LOGGER.debug("async_get_vantagepro2fromurl with url=%s", url)
        vp = None
        try:
            loop = asyncio.get_event_loop()
            vp = await loop.run_in_executor(None, self.get_vantagepro2fromurl, url)
        except Exception as e:
            _LOGGER.error("Error on opening device from url: %s: %s", url, e)
        return vp

    async def connect_to_station(self) -> None:
        """Connect to the Davis station and verify the serial link is active."""
        if not self._vantagepro2:
            self._vantagepro2 = await self.async_get_vantagepro2fromurl(self.get_link())
        
        if not self._vantagepro2:
            raise ConnectionError(f"Failed to create VantagePro2 object for {self._link}")

        await self._hass.async_add_executor_job(self._vantagepro2.link.open)

        if not hasattr(self._vantagepro2, "link") or self._vantagepro2.link is None:
            raise ConnectionError(
                f"Serial port opened at {self._link}, but Davis console failed to ACK wake-up signal."
            )

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
                data = self._vantagepro2.get_current_data() 
            else:
                _LOGGER.debug("Start get_current_data (LOOP 1)")
                data = self._vantagepro2.get_current_data()
                
            _LOGGER.debug("End get_current_data:")
        except Exception as e:
            if not self._persistent_connection:
                self._vantagepro2.link.close()
            raise e

        try:
            _LOGGER.debug("Start get_hilows")
            hilows = self._vantagepro2.get_hilows()
            _LOGGER.debug("End get_hilows")
        except Exception as e:
            _LOGGER.error("Couldn't get hilows: %s", e)
            
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
        if not self._persistent_connection:
            self._vantagepro2.link.close()

        self._last_readout_duration = (datetime.now() - start_readout).total_seconds()

        return data, archives, hilows

    async def async_get_current_data(self):
        """Get current date from weather station async."""
        data = self._last_data
        try:
            loop = asyncio.get_event_loop()
            new_data, archives, hilows = await loop.run_in_executor(
                None, self.get_current_data
            )
            if new_data:
                new_raw_data = self.__get_full_raw_data(new_data)
                self._last_raw_data = new_raw_data
                self.remove_all_incorrect_data(new_raw_data, new_data)
                self.add_additional_info(new_data)
                self.convert_values(new_data)
                
                if archives:
                    self.add_archive_info(archives, new_data)
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
    async def _async_send_console_command(self, command: str) -> str:
        """Helper to send a direct serial command to the console."""
        def send_cmd():
            if not self._vantagepro2:
                self._vantagepro2 = self.get_vantagepro2fromurl(self.get_link())
            self._vantagepro2.link.open()
            self._vantagepro2.link.wakeup()
            self._vantagepro2.link.write(command.encode('ascii'))
            
            # Read response
            response = self._vantagepro2.link.read(256)
            if not self._persistent_connection:
                self._vantagepro2.link.close()
            return response.decode('ascii', errors='ignore')

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, send_cmd)

    async def async_set_console_lamps(self, state: bool):
        """Turn the physical console backlight on (1) or off (0)."""
        cmd = f"LAMPS {'1' if state else '0'}\n"
        await self._async_send_console_command(cmd)

    async def async_clear_alarms(self):
        """Clear all active alarm bits."""
        await self._async_send_console_command("CLRBITS\n")

    async def async_get_rxcheck(self):
        """Retrieve detailed connectivity diagnostics."""
        return await self._async_send_console_command("RXCHECK\n")
    # -----------------------------

    def add_additional_info(self, data: dict[str, Any]) -> None:
        if data.get("TempOut") is not None:
            if data.get("HumOut") is not None:
                data["HeatIndex"] = calc_heat_index(data["TempOut"], data["HumOut"])
                data["DewPoint"] = calc_dew_point(data["TempOut"], data["HumOut"])
            if data.get("WindSpeed") is not None:
                data["WindChill"] = calc_wind_chill(data["TempOut"], data["WindSpeed"])
                if data.get("HumOut") is not None:
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
        
        # --- HARDWARE DIAGNOSTICS & ALARMS EXTRACTION ---
        # These byte offsets are only valid for LOOP1 packets. The LOOP2 packet
        # layout is different and undocumented in our reference manual, so skip
        # this extraction when LOOP2 is active to avoid reporting wrong data.
        if (
            not self._use_loop2
            and "_raw_bytes" in self._last_raw_data
            and len(self._last_raw_data["_raw_bytes"]) >= 99
        ):
            raw = self._last_raw_data["_raw_bytes"]
            
            # Byte 86: Transmitter Battery Status
            data["TransmitterBatteryStatus"] = raw[86]
            
            # Bytes 87-88: Console Battery Voltage = ((Data * 300)/512)/100.0
            raw_voltage = struct.unpack('<H', raw[87:89])[0]
            data["ConsoleBatteryVoltage"] = ((raw_voltage * 300) / 512) / 100.0
            
            # Byte 71: Rain Alarms
            rain_alarms = raw[71]
            data["FlashFloodAlarm"] = bool(rain_alarms & 0x02) # Bit 1
            data["StormRainAlarm"] = bool(rain_alarms & 0x08)  # Bit 3
            
            # Byte 73: Outside Alarms 2
            out_alarms_2 = raw[73]
            data["THSWAlarm"] = bool(out_alarms_2 & 0x01) # Bit 0
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
        data_info = {key: value for key, value in LoopDataParserRevB.LOOP_FORMAT}
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

    def clear_cached_property(self, property_name: str):
        del self._vantagepro2.__dict__[property_name]

    def get_rain_collector(self) -> str:
        rain_collector_map = {
            0x00: RAIN_COLLECTOR_IMPERIAL,
            0x10: RAIN_COLLECTOR_METRIC,
            0x20: RAIN_COLLECTOR_METRIC_0_1,
        }
        self._vantagepro2.wake_up()
        rain_collector = self._vantagepro2.get_rain_collector() 
        return rain_collector_map.get(rain_collector, "") 

    async def async_get_rain_collector(self) -> str:
        info = ""
        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, self.get_rain_collector)
        except Exception as e:
            _LOGGER.error("Couldn't get rain collector: %s", e)
        return info

    def set_rain_collector(self, rain_collector: str):
        rain_collector_map = {
            RAIN_COLLECTOR_IMPERIAL: 0x00,
            RAIN_COLLECTOR_METRIC: 0x10,
            RAIN_COLLECTOR_METRIC_0_1: 0x20,
        }
        self._vantagepro2.set_rain_collector(
            rain_collector_map.get(rain_collector, 0x00)
        )
        # Invalidate the cache so the next poll re-reads the new setting.
        self._rain_collector = ""

    async def async_set_rain_collector(self, rain_collector: str):
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.set_rain_collector, rain_collector)
        except Exception as e:
            _LOGGER.error("Couldn't set rain collector: %s", e)

    def get_latitude_longitude_elevation(self) -> tuple[float, float, int]:
        latitude = longitude = None
        data = self._vantagepro2.read_from_eeprom("0B", 6) 
        latitude, longitude, elevation = struct.unpack(b"hhh", data) 
        latitude /= 10
        longitude /= 10
        return latitude, longitude, elevation

    async def async_get_latitude_longitude_elevation(self):
        latitude = longitude = elevation = None
        try:
            loop = asyncio.get_event_loop()
            latitude, longitude, elevation = await loop.run_in_executor(
                None, self.get_latitude_longitude_elevation
            )
        except Exception as e:
            _LOGGER.error("Couldn't get latitude longitude: %s", e)
        return latitude, longitude, elevation

    def get_davis_time(self) -> datetime | None:
        data = None
        try:
            self._vantagepro2.link.open()
            data = self._vantagepro2.gettime()
        except Exception as e:
            raise e
        finally:
            if not self._persistent_connection:
                self._vantagepro2.link.close()
        return data

    async def async_get_davis_time(self) -> datetime | None:
        data = None
        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, self.get_davis_time)
        except Exception as e:
            _LOGGER.error("Couldn't get davis time: %s", e)
        return data

    def set_davis_time(self, dtime: datetime) -> None:
        try:
            self._vantagepro2.link.open()
            self._vantagepro2.settime(dtime)
        except Exception as e:
            raise e
        finally:
            if not self._persistent_connection:
                self._vantagepro2.link.close()

    async def async_set_davis_time(self) -> None:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.set_davis_time, datetime.now())
        except Exception as e:
            _LOGGER.error("Couldn't set davis time: %s", e)

    def get_info(self) -> dict[str, Any] | None:
        try:
            self._vantagepro2.link.open()
            firmware_version = self._vantagepro2.firmware_version 
            firmware_date = self._vantagepro2.firmware_date 
            diagnostics = self._vantagepro2.diagnostics 
        except Exception as e:
            raise e
        finally:
            if not self._persistent_connection:
                self._vantagepro2.link.close()
        return {
            "version": firmware_version,
            "date": firmware_date,
            "diagnostics": diagnostics,
        }

    async def async_get_info(self) -> dict[str, Any] | None:
        info = None
        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, self.get_info)
        except Exception as e:
            _LOGGER.error("Couldn't get firmware info: %s", e)
        return info

    def get_static_info(self) -> dict[str, Any] | None:
        try:
            self._vantagepro2.link.open()
            firmware_version = self._vantagepro2.firmware_version 
            archive_period = self._vantagepro2.archive_period 
        except Exception as e:
            raise e
        finally:
            if not self._persistent_connection:
                self._vantagepro2.link.close()
        return {"version": firmware_version, "archive_period": archive_period}

    async def async_get_static_info(self) -> dict[str, Any] | None:
        info = None
        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, self.get_static_info)
        except Exception as e:
            _LOGGER.error("Couldn't get static info: %s", e)
        return info

    def set_yearly_rain(self, rain_clicks: int) -> None:
        try:
            self._vantagepro2.link.open()
            self._vantagepro2.set_yearly_rain(rain_clicks)
        except Exception as e:
            raise e
        finally:
            if not self._persistent_connection:
                self._vantagepro2.link.close()

    async def async_set_yearly_rain(self, rain_clicks: int) -> None:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.set_yearly_rain, rain_clicks)
        except Exception as e:
            _LOGGER.error("Couldn't set yearly rain: %s", e)

    def set_archive_period(self, archive_period: int) -> None:
        try:
            self._vantagepro2.link.open()
            self._vantagepro2.set_archive_period(archive_period)
        except Exception as e:
            raise e
        finally:
            if not self._persistent_connection:
                self._vantagepro2.link.close()

    async def async_set_archive_period(self, archive_period: int) -> None:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.set_archive_period, archive_period)
        except Exception as e:
            _LOGGER.error("Couldn't set archive period: %s", e)

    def get_iso_now(self) -> datetime:
        now = convert_to_iso_datetime(
            datetime.now(), ZoneInfo(self._hass.config.time_zone)
        )
        return now