# Shady – Solar Forecast Sensor for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue.svg)](https://www.home-assistant.io/)

**Shady** exposes the solar production forecast from Home Assistant's built-in Energy Dashboard as proper sensor entities — corrected for real-world shading and string-specific losses using a per-hour model trained on your own historical recorder data.

---

## How It Works

```
Energy Dashboard forecast (Wh/slot)
        │
        ▼
┌──────────────────────────────────────────────────┐
│  Per-string hourly correction (chosen algorithm) │
│                                                  │
│  For each PV string and each hour-of-day H:      │
│    model(H)  ←  fitted over last N days          │
│    of recorder history                           │
│                                                  │
│  Corrected(H) = max(0, predict(model(H), raw))   │
└──────────────────────────────────────────────────┘
        │
        ▼
Summed corrected forecast → HA sensors
```

### Correction Algorithms

Three algorithms are available, all sharing the same per-hour structure:

| Algorithm | Model | Best for |
|---|---|---|
| **Factor** | `y = factor(H) × raw` | Simple setups, limited history |
| **Linear regression** | `y = slope(H) × raw + intercept(H)` | General use (default) |
| **Quadratic regression** | `y = a(H) × raw² + b(H) × raw + c(H)` | Non-linear shading effects |

All algorithms use:
- **Per-hour models** — a separate model is fitted for each hour of the day (0–23). A chimney that shades string 2 only between 09:00–11:00 is captured precisely.
- **Neighbour smoothing** — training samples from H±1 are added at 50 % weight so hours with few data points borrow strength from adjacent hours.
- **Z-score normalisation** — the forecast reference sensor (typically in W) and the raw forecast (Wh) can have different units; normalisation makes all algorithms unit-agnostic.
- **Graceful fallback** — quadratic falls back to linear if fewer than 3 training points are available for a bucket; any algorithm falls back to the raw forecast if no models can be fitted.

### Per-String Models

Up to 4 PV strings can be configured independently. Each string gets its own set of hourly models, allowing different shading profiles per string (e.g. string 1 faces south-west and is unshaded, string 2 faces east and is shaded by a chimney at 09:00–11:00). The corrected outputs are summed into the aggregate forecast.

### Persistence

The last successful forecast is saved to HA storage and restored on restart so sensors have values immediately after a reboot.

### Event-Driven Updates

Updates are triggered immediately whenever the Energy Manager receives new forecast data. A 1-hour fallback poll is used as safety net.

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

1. Copy the `custom_components/shady/` folder into `<config>/custom_components/shady/`
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
| **History days** | – | Days of recorder history used for model training (default: 28) |
| **Correction algorithm** | – | `factor`, `linear` (default), or `quadratic` |

All options can be changed later via **Settings → Devices & Services → Shady → Configure**.

> **Tip:** The forecast reference sensor and PV string sensors must have recorder statistics. Verify under **Developer Tools → Statistics**.

---

## Sensors

All sensors are grouped under the **Shady** device.

### Aggregate

| Entity | Unit | Description |
|---|---|---|
| `sensor.shady_solar_forecast_hourly` | Wh | Corrected forecast for the **current hour** |
| `sensor.shady_solar_forecast_today` | Wh | Total corrected forecast for **today** |
| `sensor.shady_solar_forecast_remaining` | Wh | Corrected forecast for the **rest of today** |
| `sensor.shady_solar_forecast_hourly_raw` | Wh | **Raw** (uncorrected) forecast for the current hour |

The `solar_forecast_hourly` and `solar_forecast_hourly_raw` sensors each carry a `forecast` attribute with their respective full forecast dict:

```yaml
forecast:
  "2025-05-23T06:00:00+02:00": 12.5
  "2025-05-23T07:00:00+02:00": 87.3
  "2025-05-23T08:00:00+02:00": 201.0
  ...
```

> Note: slots whose hour-of-day has no fitted model (e.g. night hours with zero variance) default to `0.0` in the corrected forecast.

### Per-String

For each configured PV string one additional sensor is created:

| Entity (example) | Unit | Description |
|---|---|---|
| `sensor.shady_solar_forecast_hourly_solakon_one_string_1_leistung` | Wh | Corrected forecast current hour, string 1 only |

Each per-string sensor carries a `forecast` attribute (string-specific `{ts: Wh}` dict) and a `pv_sensor` attribute with the source entity ID.

---

## Choosing an Algorithm

| Situation | Recommendation |
|---|---|
| Less than 2–3 weeks of history | **Factor** – needs fewest data points |
| Typical residential installation | **Linear** – good balance of accuracy and stability |
| Strong non-linear shading (e.g. partial roof obstruction that grows with sun angle) | **Quadratic** – models the curvature; requires more history for stable coefficients |
| Quadratic produces implausible spikes | Switch back to **Linear** |

All algorithms are constrained to `max(0, predicted)` so no negative production values are ever emitted.

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

Enable **DEBUG** logging for detailed model output:

```yaml
logger:
  logs:
    custom_components.shady: debug
```

On each update you will see per-string INFO lines plus DEBUG detail per hour:

```
INFO  Hourly models for sensor.string_1: algorithm=linear  14 hour-buckets  fc_rows=419  pv_rows=401  ...
DEBUG   hour 06: (0.2341, 1.23)
DEBUG   hour 07: (0.3102, 0.88)
...
INFO  → Midday slot: raw=612.0 Wh  corrected=287.4 Wh
```

| Symptom | Likely cause |
|---|---|
| Sensors show `unavailable` | No solar forecast provider configured in Energy Dashboard |
| `forecast` attribute is `{}` | Forecast provider has no data yet; wait up to 1 h |
| `No hourly models for sensor.x` | Sensor has no recorder statistics — check **Developer Tools → Statistics** |
| Corrected ≈ Raw | `fc_sensor` and PV sensors are the same or too similar; choose a reference that tracks the unshaded forecast |
| Quadratic produces implausible values | Insufficient history for stable quadratic fit; switch to Linear |
| All hour-buckets degenerate | Zero variance in training data; more history needed |

---

## License

MIT
