"""Constants for the E-REDES integration."""

from typing import Final

DOMAIN: Final = "eredes"

# Configuration keys
CONF_CPE: Final = "cpe"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_HISTORY_SYNC_FREQUENCY: Final = "history_sync_frequency"
CONF_HISTORY_SYNC_TIME: Final = "history_sync_time"
CONF_HISTORY_SYNC_INTERVAL_DAYS: Final = "history_sync_interval_days"
CONF_PROVISIONAL_REFRESH_INTERVAL_MINUTES: Final = (
    "provisional_refresh_interval_minutes"
)

# Legacy config keys migrated to CONF_ACCESS_TOKEN (see async_migrate_entry)
LEGACY_TOKEN_KEYS: Final = ("session_cookie", "aat_token")

# API endpoints
BASE_URL: Final = "https://balcaodigital.e-redes.pt"
API_URL: Final = f"{BASE_URL}/ms/reading/data-usage/edm/get"

# Timing
DEFAULT_SCAN_INTERVAL: Final = 3600  # 1 hour in seconds
HISTORY_SYNC_FREQUENCY_HOURLY: Final = "hourly"
HISTORY_SYNC_FREQUENCY_DAYS: Final = "days"
DEFAULT_HISTORY_SYNC_FREQUENCY: Final = HISTORY_SYNC_FREQUENCY_DAYS
DEFAULT_HISTORY_SYNC_TIME: Final = "05:00:00"
DEFAULT_HISTORY_SYNC_INTERVAL_DAYS: Final = 1
MIN_HISTORY_SYNC_INTERVAL_DAYS: Final = 1
MAX_HISTORY_SYNC_INTERVAL_DAYS: Final = 30
DEFAULT_PROVISIONAL_REFRESH_INTERVAL_MINUTES: Final = 15
MIN_PROVISIONAL_REFRESH_INTERVAL_MINUTES: Final = 1
MAX_PROVISIONAL_REFRESH_INTERVAL_MINUTES: Final = 1440

# API request types
REQUEST_TYPE_15MIN: Final = "3"  # 15-minute interval readings

# Sensor keys
SENSOR_ENERGY: Final = "energy"
SENSOR_POWER: Final = "power"
SENSOR_LAST_REAL_DATA_DAY: Final = "last_real_data_day"
SENSOR_LAST_MATCHING_15MIN_DATA_DAY: Final = "last_matching_15min_data_day"
