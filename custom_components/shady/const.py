"""Constants for Shady."""

DOMAIN = "shady"
DEFAULT_NAME = "Shady"

# Config entry keys
CONF_FC_SENSOR    = "fc_sensor"       # forecast reference sensor (recorder)
CONF_PV_SENSOR_1  = "pv_sensor_1"     # String 1
CONF_PV_SENSOR_2  = "pv_sensor_2"     # String 2 (optional)
CONF_PV_SENSOR_3  = "pv_sensor_3"     # String 3 (optional)
CONF_PV_SENSOR_4  = "pv_sensor_4"     # String 4 (optional)
CONF_HISTORY_DAYS = "history_days"
CONF_ALGORITHM    = "algorithm"

DEFAULT_FC_SENSOR   = "sensor.power_production_now"
DEFAULT_HISTORY_DAYS = 28
DEFAULT_ALGORITHM    = "linear"

# Algorithm choices
ALGORITHM_FACTOR     = "factor"      # per-5-min-bucket mean ratio: avg(pv) / avg(fc)
ALGORITHM_LINEAR     = "linear"      # per-5-min-bucket WLS: pv ~ slope*fc + intercept
ALGORITHM_QUADRATIC  = "quadratic"   # per-5-min-bucket WLS: pv ~ a*fc² + b*fc + c

ALGORITHM_OPTIONS = [ALGORITHM_FACTOR, ALGORITHM_LINEAR, ALGORITHM_QUADRATIC]

# All string sensor keys in order
PV_SENSOR_KEYS = [CONF_PV_SENSOR_1, CONF_PV_SENSOR_2, CONF_PV_SENSOR_3, CONF_PV_SENSOR_4]
