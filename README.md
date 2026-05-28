# Shady – Solar Forecast Sensor for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue.svg)](https://www.home-assistant.io/)

**Shady** exposes the solar production forecast from Home Assistant's built-in Energy Dashboard as proper sensor entities — corrected for real-world shading and string-specific losses using a per-hour linear regression model trained on your own historical recorder data.

---

## How It Works

```
Energy Dashboard forecast (Wh/slot)
        │
        ▼
┌─────────────────────────────────────────────┐
│  Per-string hourly regression correction    │
│                                             │
│  For each PV string and each hour-of-day H: │
│    slope(H), intercept(H)  ←  WLS fit over  │
│    last N days of recorder history          │
│                                             │
│  Corrected(H) = max(0, slope(H) × raw + intercept(H))  │
└─────────────────────────────────────────────┘
        │
        ▼
Summed corrected forecast → HA sensors
```

Key properties of the correction model:

- **Per-hour models** — a separate regression is fitted for each hour of the day (0–23). A chimney that shades string 2 only between 09:00–11:00 is modelled precisely.
- **Neighbour smoothing** — training samples from H±1 are added at 50 % weight so hours with few data points borrow strength from adjacent hours.
- **Z-score normalisation** — the forecast reference sensor (typically in W) and the raw forecast (Wh) can have different units and magnitudes; normalisation makes the regression unit-agnostic.
- **Up to 4 independent PV strings** — each string gets its own set of hourly models, allowing different shading profiles per string.
- **Persistence** — the last successful forecast is saved to HA storage and restored on restart so sensors are never undefined after a reboot.
- **Event-driven** — updates immediately whenever the Energy Manager receives new forecast data; a 1-hour fallback poll is used as safety net.

---

## Requirements

- Home Assistant **2024.1** or later
- **Energy Dashboard** configured with at least one solar source
- A solar **forecast provider** linked to that source in the Energy Dashboard:
  - [Forecast.Solar](https://www.home-assistant.io/integrations/forecast_solar/)
  - [Solcast PV Solar](https://github.com/BJReplay/ha-solcast-solar)
  - Any other integration that implements `async_get_solar_forecast`
- **Recorder** enabled (default in HA) with history for the configured sensors

---

## Installation

### Via HACS (recommended)

1. Open HACS → **Integrations** → ⋮ → **Custom repositories**
2. Add `https://github.com/eschnepel/shady_solar_forecast` with category **Integration**
3. Search for **Shady** and install
4. Restart Home Assistant

### Manual

1. Copy the `shady/` folder into `<config>/custom_components/shady/`
2. Restart Home Assistant

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration → Shady**
2. Fill in the configuration form:

| Field | Required | Description |
|---|---|---|
| **Forecast reference sensor** | ✓ | Sensor tracked by the Recorder that correlates with the raw forecast (e.g. `sensor.power_production_now`) |
| **PV String 1** | ✓ | Actual production sensor for string 1 (tracked by Recorder) |
| **PV String 2–4** | – | Additional string sensors (leave empty if not applicable) |
| **History days** | – | Days of recorder history used for regression training (default: 28) |

> **Tip:** The forecast reference sensor and PV string sensors must have recorder statistics. Verify in **Developer Tools → Statistics**.

---

## Sensors

All sensors are grouped under the **Shady** device.

### Aggregate

| Entity | Unit | Description |
|---|---|---|
| `sensor.shady_solar_forecast_hourly` | Wh | Corrected forecast for the **current hour** |
| `sensor.shady_solar_forecast_today` | Wh | Total corrected forecast for **today** |
| `sensor.shady_solar_forecast_remaining` | Wh | Corrected forecast for the **rest of today** |

The `solar_forecast_hourly` sensor carries a `forecast` attribute with the full corrected forecast dict:

```yaml
forecast:
  "2025-05-23T06:00:00+02:00": 12.5
  "2025-05-23T07:00:00+02:00": 87.3
  "2025-05-23T08:00:00+02:00": 201.0
  ...
```

> Note: slots whose hour-of-day has no fitted regression model (e.g. night hours with zero variance) are excluded from the corrected forecast.

### Per-String

For each configured PV string one additional sensor is created:

| Entity (example) | Unit | Description |
|---|---|---|
| `sensor.shady_solar_forecast_hourly_solakon_one_string_1_leistung` | Wh | Corrected forecast for the current hour, string 1 only |

Each per-string sensor also carries a `forecast` attribute (same structure as above, string-specific) and a `pv_sensor` attribute with the source entity ID.

---

## Use in Automations

### Start appliance when solar forecast is good

```yaml
automation:
  - alias: "Start dishwasher at solar peak"
    trigger:
      - platform: time
        at: "09:00:00"
    condition:
      - condition: numeric_state
        entity_id: sensor.shady_solar_forecast_remaining
        above: 3000   # > 3 kWh remaining today
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.dishwasher
```

### Template sensor for a specific hour

```yaml
template:
  - sensor:
      - name: "Solar Forecast 14:00"
        unit_of_measurement: "Wh"
        state: >
          {% set fc = state_attr('sensor.shady_solar_forecast_hourly', 'forecast') %}
          {% if fc %}
            {% set slot = fc | dict2items
               | selectattr('key', 'search', 'T14:') | list | first %}
            {{ slot.value if slot else 0 }}
          {% else %}0{% endif %}
```

---

## Diagnostics & Troubleshooting

Enable **DEBUG** logging for detailed regression output:

```yaml
logger:
  logs:
    custom_components.shady: debug
```

On each update you will see:

```
INFO  Hourly models for sensor.string_1: 14 hour-buckets fitted  fc_rows=419  pv_rows=401
DEBUG   hour 06: slope=0.2341  intercept=1.2300
DEBUG   hour 07: slope=0.3102  intercept=0.8800
...
INFO  → Combined midday slot: raw=612.0 Wh  corrected=287.4 Wh
```

| Symptom | Likely cause |
|---|---|
| Sensors show `unavailable` | No solar forecast provider configured in Energy Dashboard |
| `forecast` attribute is `{}` | Forecast provider has no data yet; wait up to 1 h |
| `No hourly models for sensor.x` | Sensor has no recorder statistics — check **Developer Tools → Statistics** |
| Corrected = Raw | All hour-buckets degenerate (zero variance); needs more history |
| Corrected values seem wrong | Check `fc_sensor` — it should correlate with the raw forecast, not be the PV production itself |

---

## License

MIT
