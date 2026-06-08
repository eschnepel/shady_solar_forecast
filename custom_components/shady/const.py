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
