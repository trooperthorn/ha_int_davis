"""Constants for the Davis Vantage integration."""

NAME = "Davis Vantage"
DOMAIN = "davis_vantage"
MANUFACTURER = "Davis"

DEFAULT_SYNC_INTERVAL = 30  # seconds
DEFAULT_NAME = NAME

RAIN_COLLECTOR_IMPERIAL = '0.01"'
RAIN_COLLECTOR_METRIC = '0.2 mm'
RAIN_COLLECTOR_METRIC_0_1 = '0.1 mm'

PROTOCOL_NETWORK = 'Network'
PROTOCOL_SERIAL = 'Serial'

SERVICE_SET_DAVIS_TIME = 'set_davis_time'
SERVICE_GET_DAVIS_TIME = 'get_davis_time'
SERVICE_GET_RAW_DATA = 'get_raw_data'
SERVICE_GET_INFO = 'get_info'
SERVICE_SET_YEARLY_RAIN = 'set_yearly_rain'
SERVICE_SET_ARCHIVE_PERIOD = 'set_archive_period'
SERVICE_SET_RAIN_COLLECTOR = 'set_rain_collector'
SERVICE_SET_BAROMETER_CALIBRATION = 'set_barometer_calibration'
SERVICE_GET_EEPROM = 'get_eeprom'
SERVICE_SET_EEPROM = 'set_eeprom'
SERVICE_SET_CONSOLE_LAMPS = 'set_console_lamps'
SERVICE_CLEAR_ALARMS = 'clear_alarms'

MODEL_VANTAGE_PRO2 = 'Vantage Pro2'
MODEL_VANTAGE_PRO2PLUS = 'Vantage Pro2 Plus'
MODEL_VANTAGE_VUE = 'Vantage Vue'

CONFIG_STATION_MODEL = "station_model"
CONFIG_INTERVAL = "interval"
CONFIG_MINIMAL_INTERVAL = 5
CONFIG_PROTOCOL = "protocol"
CONFIG_LINK = "link"
CONFIG_PERSISTENT_CONNECTION = "persistent_connection"
CONF_USE_LOOP2 = "use_loop2"

CONFIG_BAUD_RATE = "baud_rate"
DEFAULT_BAUD_RATE = 19200
# Valid console baud rates, fastest first, per the Davis serial protocol.
SUPPORTED_BAUD_RATES = [19200, 14400, 9600, 4800, 2400, 1200]
