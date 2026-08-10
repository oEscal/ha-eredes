"""Constants for the E-REDES integration."""

from typing import Final

DOMAIN: Final = "eredes"

# Configuration keys
CONF_CPE: Final = "cpe"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_HISTORY_SYNC_FREQUENCY: Final = "history_sync_frequency"
CONF_HISTORY_SYNC_TIME: Final = "history_sync_time"
CONF_HISTORY_SYNC_INTERVAL_DAYS: Final = "history_sync_interval_days"

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

# API request types
REQUEST_TYPE_15MIN: Final = "3"  # 15-minute interval readings

# Sensor keys
SENSOR_ENERGY: Final = "energy"
SENSOR_POWER: Final = "power"
