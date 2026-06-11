# Shady – Solar Forecast Sensor for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue.svg)](https://www.home-assistant.io/)

**Shady** exposes the solar production forecast from Home Assistant's built-in Energy Dashboard as proper sensor entities — corrected for real-world shading and string-specific losses using per-5-minute-bucket regression models trained on your own historical recorder data.

The HA-independent correction logic lives in [shadylib](https://github.com/eschnepel/shadylib), which Shady depends on.

---

## How It Works

```
Energy Dashboard forecast  →  {ISO-timestamp: Wh}  (native provider resolution)
        │
        ▼
┌─────────────────────────────────────────────────────────────┐
│  Per-string, per-5-min-bucket correction                    │
│                                                             │
│  Each forecast slot is matched to its exact 5-min bucket.  │
│  Slots without a bucket model → 0.0 (e.g. night hours).    │
└─────────────────────────────────────────────────────────────┘
        │
        ├── Today   → retains provider's native resolution
        └── Tomorrow → aggregated into full hours
```

Shady reads the forecast directly from the Energy Dashboard API in whatever resolution the provider delivers (5-min, 30-min, hourly, or any other). Each slot is matched to the corresponding 5-min bucket model and corrected individually. The resulting slots are passed through unchanged for today, and aggregated into full hours for tomorrow.

### Why 5-Minute Buckets?

A chimney that shades string 2 between 09:10 and 09:40 will only be visible in the data at 5-minute resolution. Hour-level models average it away; 5-minute models capture it precisely.

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

### Effective Sensor Mode

When **Use effective sensors** is enabled, Shady computes loss-corrected PV string values before training the correction models. System-level losses (grid import/export, battery import/export) are distributed across strings using a waterfall cascade algorithm (implemented in [shadylib](https://github.com/eschnepel/shadylib)). This produces more accurate models when inverter output is regularly reduced by grid export limits or battery charging.

### Supported Input Units

Shady reads the unit from the HA entity registry for each configured sensor.
All sensors (`fc_sensor` and `pv_sensors`) are supported in:

| Unit | Type | Conversion |
|------|------|------------|
| `W`  | Power / measurement | `W × 5/60 → Wh/slot` |
| `kW` | Power / measurement | `kW × 1000 × 5/60 → Wh/slot` |
| `Wh` | Energy / total | no conversion |
| `kWh`| Energy / total | `kWh × 1000 → Wh/slot` |
| `MWh`| Energy / total | `MWh × 1 000 000 → Wh/slot` |

Internal processing always uses Wh/slot. All output sensors use the **fc_sensor's unit and state_class** for their output.

If `pv_sensors` have mixed units, Shady logs a warning but continues — each sensor is converted individually.

### Per-String Models

Any number of PV strings can be configured independently. Each string gets its own set of bucket models, allowing different shading profiles per string. The corrected outputs are summed into the aggregate forecast.

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
- [shadylib](https://github.com/eschnepel/shadylib) (installed automatically)

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
| **PV string sensors** | ✓ | One or more actual production sensors (5-min recorder statistics required); at least 1 required |
| **History days** | – | Days of 5-min recorder history used for model training (default: 28, range: 7–365) |
| **Correction algorithm** | – | `factor`, `linear` (default), or `quadratic` |
| **Grid import sensor** | – | Sensor measuring power drawn from the grid (positive values only) |
| **Grid export sensor** | – | Sensor measuring power fed into the grid (positive values only) |
| **Battery import sensor** | – | Sensor measuring power drawn from the battery |
| **Battery export sensor** | – | Sensor measuring power fed into the battery |
| **Use effective sensors** | – | Train models on loss-corrected PV values (requires at least one system I/O sensor; default: off) |

All options can be changed later via **Settings → Devices & Services → Shady → Configure**.

> **Migrating from an older version?** If your config entry still uses the old `pv_sensor_1…4` keys, Shady will automatically migrate them to the new `pv_sensors` list on the next HA restart.

> **Tip:** Sensors must have 5-minute statistics in the recorder. Verify under **Developer Tools → Statistics**. In `configuration.yaml`, ensure `recorder:` does not exclude these sensors.

---

## Sensors

All sensors are grouped under the **Shady** device. Values are rounded to 2 decimal places.

### Aggregate

| Entity | Unit | Description |
|---|---|---|
| `sensor.shady_solar_forecast_current` | fc_sensor unit | Corrected forecast for the **current slot** |
| `sensor.shady_solar_forecast_today` | fc_sensor unit | Total corrected forecast for **today** |
| `sensor.shady_solar_forecast_remaining` | fc_sensor unit | Corrected forecast for the **rest of today** |
| `sensor.shady_solar_forecast_raw` | fc_sensor unit | Raw (uncorrected) forecast for the current slot |

The `solar_forecast_current` sensor carries two forecast attributes:

```yaml
# Today at provider's native resolution
forecast:
  "2025-05-23T10:00:00+02:00": 287.40
  "2025-05-23T10:30:00+02:00": 312.10
  ...

# Tomorrow aggregated into full hours
forecast_tomorrow:
  "2025-05-24T06:00:00+02:00": 142.50
  "2025-05-24T07:00:00+02:00": 318.20
  ...
```

The `solar_forecast_raw` sensor carries the full raw forecast:

```yaml
forecast:
  "2025-05-23T10:00:00+02:00": 400.00
  "2025-05-23T10:30:00+02:00": 430.00
  ...
```

### Per-String

For each configured PV string two additional sensors are created:

| Entity (example) | Unit | Description |
|---|---|---|
| `sensor.shady_solar_string_pv_string_dach_ost_forecast` | fc_sensor unit | Corrected forecast for the current slot for string `sensor.pv_string_dach_ost` |
| `sensor.shady_solar_string_pv_string_dach_ost_effective` | fc_sensor unit | Loss-corrected (effective) current value for the string (available when **Use effective sensors** is on) |

Each per-string forecast sensor carries a `forecast` attribute (today's string-specific `{ts: value}` dict) and a `pv_sensor` attribute with the source entity ID.

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

### Template sensor for a specific slot

```yaml
template:
  - sensor:
      - name: "Solar Forecast 14:00"
        unit_of_measurement: "Wh"
        state: >
          {% set fc = state_attr('sensor.shady_solar_forecast_current', 'forecast') %}
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

| Symptom | Likely cause |
|---|---|
| Sensors show `unavailable` | No solar forecast provider configured in Energy Dashboard |
| `forecast` attribute is `{}` | Forecast provider has no data yet; wait up to 1 h |
| `No bucket models for sensor.x` | No 5-min statistics — check **Developer Tools → Statistics** |
| Corrected ≈ Raw | `fc_sensor` is too similar to the PV sensor; use a sensor that tracks the unshaded potential |
| Quadratic produces implausible values | Insufficient 5-min history; switch to Linear |
| `forecast_tomorrow` is empty | Provider only delivers today's forecast |
| Effective sensors show 0 | System I/O sensors not configured or all loss absorbed |

---

## License

Apache 2.0
