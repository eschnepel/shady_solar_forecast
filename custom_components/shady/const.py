"""Constants for Shady."""

DOMAIN = "shady"
DEFAULT_NAME = "Shady"

# Config entry keys
CONF_FC_SENSOR = "fc_sensor"  # forecast reference sensor (recorder)
CONF_PV_SENSORS = "pv_sensors"  # ordered list of PV sensor entity IDs
CONF_HISTORY_DAYS = "history_days"
CONF_ALGORITHM = "algorithm"

# Legacy keys (pv_sensor_1…4) – kept for migration only
LEGACY_PV_SENSOR_KEYS = [
    "pv_sensor_1",
    "pv_sensor_2",
    "pv_sensor_3",
    "pv_sensor_4",
]

DEFAULT_FC_SENSOR = "sensor.power_production_now"
DEFAULT_HISTORY_DAYS = 28
DEFAULT_ALGORITHM = "linear"

# Algorithm choices
ALGORITHM_FACTOR = "factor"  # per-5-min-bucket mean ratio: avg(pv) / avg(fc)
ALGORITHM_LINEAR = "linear"  # per-5-min-bucket WLS: pv ~ slope*fc + intercept
ALGORITHM_QUADRATIC = "quadratic"  # per-5-min-bucket WLS: pv ~ a*fc² + b*fc + c

ALGORITHM_OPTIONS = [ALGORITHM_FACTOR, ALGORITHM_LINEAR, ALGORITHM_QUADRATIC]

# System I/O sensor config keys (all optional, each a single entity_id or empty)
CONF_GRID_IMPORT = "grid_import"
CONF_GRID_EXPORT = "grid_export"
CONF_BATTERY_IMPORT = "battery_import"
CONF_BATTERY_EXPORT = "battery_export"

SYSTEM_SENSOR_KEYS: list[str] = [
    CONF_GRID_IMPORT,
    CONF_GRID_EXPORT,
    CONF_BATTERY_IMPORT,
    CONF_BATTERY_EXPORT,
]

# Filter gap-successor samples from recorder data
CONF_FILTER_RECORDER_GAPS = "filter_recorder_gaps"
DEFAULT_FILTER_RECORDER_GAPS = False

# Use effective (loss-corrected) PV strings instead of raw sensors in the model
CONF_USE_EFFECTIVE_SENSORS = "use_effective_sensors"
DEFAULT_USE_EFFECTIVE_SENSORS = False

# Storage key for effective string history cache
EFFECTIVE_STORAGE_KEY = "shady.effective_history"

# Cache version – increment this on every release that changes forecast
# calculation logic (algorithm, unit handling, effective-sensor pipeline,
# etc.).  Both the last-forecast store and the effective-history store use
# this version so that stale cached data is automatically discarded after
# an update.
CACHE_VERSION = 2

EFFECTIVE_STORAGE_VERSION = CACHE_VERSION
