# Shady – Solar Forecast Sensor for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue.svg)](https://www.home-assistant.io/)

**Shady** exposes the solar production forecast from Home Assistant's built-in Energy Dashboard as proper sensor entities — corrected for real-world shading and string-specific losses using per-5-minute-bucket regression models trained on your own historical recorder data.

---

## How It Works

```
Energy Dashboard forecast (Wh/slot, native provider resolution)
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│  Per-string, per-5-min-bucket correction                    │
│                                                             │
│  Hourly provider (e.g. Forecast.Solar):                     │
│    FC T10:00 = 400 Wh (sum for 10:00–11:00)                 │
│    → applied to all 12 five-min buckets of that hour        │
│    → predictions summed → corrected hourly Wh               │
│                                                             │
│  Sub-hourly provider (e.g. Solcast 30-min):                 │
│    Each slot matched to its exact 5-min bucket              │
│                                                             │
│  Buckets without a model → 0.0 (e.g. night hours)          │
└─────────────────────────────────────────────────────────────┘
        │
        ├── Today   → retains provider resolution (hourly or sub-hourly)
        └── Tomorrow → aggregated into full hours
```

### Why 5-Minute Buckets?

A chimney that shades string 2 between 09:10 and 09:40 will only be visible in the data at 5-minute resolution. Hour-level models average it away; 5-minute models capture it precisely.

For hourly forecast providers, the hourly Wh value is applied to all 12 five-minute bucket models of that hour. Each bucket contributes its own correction (reflecting shading at that specific time), and the 12 predictions are summed back into a single corrected hourly Wh value. If fewer than 12 buckets have fitted models, the result is scaled proportionally.

### Neighbour Smoothing

Each bucket's training set is augmented with observations from adjacent buckets to improve stability for buckets with few samples:

| Distance | Weight |
|---|---|
| Self (exact bucket) | 1.0 |
| ±5 min | 0.8 |
| ±10 min | 0.3 |

### Correction Algorithms

Three algorithms are available, all using the same per-5-min-bucket structure and weighted least squares:

| Algorithm | Model | Best for |
|---|---|---|
| **Factor** | `y = factor(B) × raw` | Simple setups, limited history |
| **Linear** | `y = slope(B) × raw + intercept(B)` | General use (default) |
| **Quadratic** | `y = a(B) × raw² + b(B) × raw` | Non-linear shading (no free intercept) |

All predictions are clamped to `max(0, predicted)`.  
Quadratic uses no free intercept (physically correct: fc=0 → pv=0) and falls back to linear if fewer than 3 training points are available.  
If all string models fail, the raw forecast is passed through unchanged.

### Per-String Models

Up to 4 PV strings can be configured independently. Each string gets its own set of bucket models, allowing different shading profiles per string. The corrected outputs are summed into the aggregate forecast.

### Forecast Resolution

- **Today**: retains the provider's native resolution (hourly for Forecast.Solar, 30-min for Solcast)
- **Tomorrow**: aggregated into full hours

### Persistence

The last successful forecast is saved to HA storage and restored on restart so sensors have values immediately after a reboot.

---

## Requirements

- Home Assistant **2024.1** or later
- **Energy Dashboard** configured with at least one solar source
- A solar **forecast provider** linked to that source in the Energy Dashboard:
  - [Forecast.Solar](https://www.home-assistant.io/integrations/forecast_solar/)
  - [Solcast PV Solar](https://github.com/BJReplay/ha-solcast-solar)
  - Any other integration that implements `async_get_solar_forecast`
- **Recorder** enabled (default in HA) with 5-minute statistics for the configured sensors

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
| **Forecast reference sensor** | ✓ | Sensor with 5-min recorder statistics that correlates with the raw forecast (e.g. `sensor.power_production_now`) |
| **PV String 1** | ✓ | Actual production sensor for string 1 (5-min recorder statistics required) |
| **PV String 2–4** | – | Additional string sensors (leave empty if not applicable) |
| **History days** | – | Days of 5-min recorder history used for model training (default: 28) |
| **Correction algorithm** | – | `factor`, `linear` (default), or `quadratic` |

All options can be changed later via **Settings → Devices & Services → Shady → Configure**.

> **Tip:** Sensors must have 5-minute statistics in the recorder. Verify under **Developer Tools → Statistics**. In `configuration.yaml`, ensure `recorder:` does not exclude these sensors.

---

## Sensors

All sensors are grouped under the **Shady** device. Values are rounded to 2 decimal places.

### Aggregate

| Entity | Unit | Description |
|---|---|---|
| `sensor.shady_solar_forecast_hourly` | Wh | Corrected forecast for the **current slot** |
| `sensor.shady_solar_forecast_today` | Wh | Total corrected forecast for **today** |
| `sensor.shady_solar_forecast_remaining` | Wh | Corrected forecast for the **rest of today** |
| `sensor.shady_solar_forecast_hourly_raw` | Wh | Raw (uncorrected) forecast for the current slot |

The `solar_forecast_hourly` sensor carries two forecast attributes:

```yaml
# Today at provider resolution (hourly for Forecast.Solar, 30-min for Solcast)
forecast:
  "2025-05-23T10:00:00+02:00": 287.40   # hourly corrected Wh
  "2025-05-23T11:00:00+02:00": 341.20
  ...

# Tomorrow aggregated into full hours
forecast_tomorrow:
  "2025-05-24T06:00:00+02:00": 142.50
  "2025-05-24T07:00:00+02:00": 318.20
  ...
```

The `solar_forecast_hourly_raw` sensor carries the full raw forecast:

```yaml
forecast:
  "2025-05-23T10:00:00+02:00": 400.00
  "2025-05-23T11:00:00+02:00": 480.00
  ...
```

### Per-String

For each configured PV string one additional sensor is created:

| Entity (example) | Unit | Description |
|---|---|---|
| `sensor.shady_solar_forecast_hourly_solakon_one_string_1_leistung` | Wh | Corrected forecast current slot, string 1 only |

Each per-string sensor carries a `forecast` attribute (today's string-specific `{ts: Wh}` dict) and a `pv_sensor` attribute with the source entity ID.

---

## Choosing an Algorithm

| Situation | Recommendation |
|---|---|
| Less than 2–3 weeks of 5-min history | **Factor** – needs fewest data points |
| Typical residential installation | **Linear** – good balance of accuracy and stability |
| Non-linear shading (partial roof obstruction that grows with sun angle) | **Quadratic** – models curvature without intercept bias |
| Quadratic produces implausible values | Switch back to **Linear** |

---

## Use in Automations

### Start appliance when solar forecast is sufficient

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

On each update the log shows:

```
INFO  Bucket models for sensor.string_1: algorithm=linear  168 buckets
      fc_rows=8064  pv_rows=7718  fc=[0.00, 614.20]  pv=[0.00, 330.70]
DEBUG   bucket 09:05 → (0.288817, 3.09)
DEBUG   bucket 09:10 → (0.271340, 2.84)
...
INFO  Midday slot: raw=400.00 Wh  corrected=287.40 Wh
```

| Symptom | Likely cause |
|---|---|
| Sensors show `unavailable` | No solar forecast provider configured in Energy Dashboard |
| `forecast` attribute is `{}` | Forecast provider has no data yet; wait up to 1 h |
| `No bucket models for sensor.x` | No 5-min statistics — check **Developer Tools → Statistics** |
| Corrected ≈ Raw | `fc_sensor` is too similar to the PV sensor; use a sensor that tracks the unshaded potential |
| Quadratic produces implausible values | Insufficient 5-min history; switch to Linear |
| `forecast_tomorrow` is empty | Provider only delivers today's forecast |

---

## License

MIT
