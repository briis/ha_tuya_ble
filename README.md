# Tuya BLE — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/briis/ha_tuya_ble.svg)](https://github.com/briis/ha_tuya_ble/releases)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2026.3.2%2B-blue.svg)](https://www.home-assistant.io/)

Local Bluetooth (BLE) support for Tuya devices in Home Assistant. Device credentials are fetched once from the Tuya cloud; after that all communication happens directly over Bluetooth — no cloud dependency at runtime.

**IoT class:** Local Push — the device pushes state changes to HA over BLE.

---

## Supported devices

| Device | Category | Entity types |
|---|---|---|
| CO2 Detector | `co2bj` | Sensor (CO2 ppm, CO2 alarm, temperature, humidity, battery), Switch (alarm config) |
| Smart Lock | `ms` | Sensor (alarm, battery, fingerprint ID), Switch (motor state) |
| Raycube K7 Pro+ (Smart Lock Pro) | `jtmspro` | Sensor (alarm, battery, fingerprint, card, password) |
| Fingerbot / CubeTouch 1s & II | `szjqr` | Switch (on/off, reverse positions, manual control, program), Number (position, hold time), Select (mode), Text (program sequence), Sensor (battery) |
| Fingerbot Plus | `kg` | Switch (on/off, reverse positions, manual control, program), Number (position, hold time), Select (mode), Text (program sequence), Sensor (battery) |
| Thermostatic Radiator Valve (TRV) | `wk` | Climate, Switch (window detection, antifreeze, child lock, water scale protection, programming) |
| Soil Moisture Sensor | `wsdcg` | Sensor (soil temperature, moisture %, battery state, battery %) |
| Smart Water Bottle | `znhsb` | Sensor (temperature, water intake, battery) |
| Irrigation Computer | `ggq` | Switch (water valve), Sensor (battery, time remaining) |
| Valve Controller | `sfkzq` | Switch (water valve), Sensor (battery, time remaining) |
| Strip Lights (LGB102, generic) | `dd` | Light (brightness, color temperature, RGB) |
| Curtain motor | `cl` | Cover (open/close/stop/set position, curtain & blind) |
| Curtain Switch module | `clkg` | Cover (open/close/stop/set position) |
| Curtain Robot | `jdcljqr` | Cover (open/close/stop/set position) |

All devices also expose a **Signal Strength (dBm)** diagnostic sensor (disabled by default).

---

## Prerequisites

- Home Assistant 2026.3.2 or newer
- HACS 2.0.5 or newer
- A Bluetooth adapter accessible to your HA host (built-in or USB)
- The built-in **Tuya** integration already configured in HA (provides cloud credential resolution)
- A **Tuya IoT Platform** project with access to your devices ([developer.tuya.com](https://developer.tuya.com))

---

## Installation

### Via HACS (recommended)

1. Open HACS in Home Assistant.
2. Click **⋮ → Custom repositories**.
3. Add `https://github.com/briis/ha_tuya_ble` with category **Integration**.
4. Search for **Tuya BLE** and click **Download**.
5. Restart Home Assistant.

### Manual

1. Copy the `custom_component/tuya_ble` folder into your `<config>/custom_components/` directory.
2. Restart Home Assistant.

---

## Configuration

The integration is configured via the UI (Config Flow). No YAML is needed.

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **Tuya BLE**.
3. Enter your Tuya IoT Platform credentials:
   - **Access ID** and **Access Secret** from your IoT project
   - **App type** (Tuya Smart or Smart Life)
   - **Region endpoint** (matching your account region)
4. HA will scan for nearby BLE devices advertising the Tuya BLE service UUID and present matching devices for selection.
5. Select your device and complete setup.

### Getting Tuya IoT Platform credentials

1. Log in at [developer.tuya.com](https://developer.tuya.com) and create a Cloud project.
2. Link your devices by scanning the QR code in the Tuya Smart / Smart Life app under **Profile → Tap to Scan**.
3. Copy the **Access ID** and **Access Secret** from your project's **Overview** tab.

The integration calls the Tuya cloud API during initial setup (and when re-fetching credentials) but does **not** require cloud connectivity for day-to-day operation.

---

## How it works

1. **Discovery:** Home Assistant's Bluetooth integration passively scans for BLE advertisements containing the Tuya service UUID (`0000a201-0000-1000-8000-00805f9b34fb`).
2. **Credential lookup:** When a matching device is found, the integration queries the Tuya cloud API to retrieve the device's local encryption key.
3. **Local connection:** All subsequent communication uses an encrypted BLE connection directly to the device — no cloud involved.
4. **State updates:** The device pushes datapoint (DP) updates over BLE; HA entities update immediately.

---

## Fingerbot

Fingerbot devices (physical button pushers) expose several modes via the **Select** entity:

| Mode | Description |
|---|---|
| Switch mode | Acts like a toggle switch (push once to activate, push again to release) |
| Program mode | Runs a programmable sequence of positions and delays |

In **Program mode**, the **Text** entity accepts a sequence string in the format:

```
<position>[/<delay_ms>][;<position>[/<delay_ms>]]...
```

Example: `100/500;0` — go to position 100, wait 500 ms, return to position 0.

---

## Thermostatic Radiator Valve (TRV)

The TRV exposes a **Climate** entity with heating mode. Additional configuration switches include:

- **Window detection** — automatically reduces heating when a window is detected open
- **Antifreeze** — maintains a minimum temperature to prevent pipe freeze
- **Child lock** — disables physical controls on the device
- **Water scale protection** — periodic valve exercise to prevent limescale seizure
- **Programming mode / Programming switch** — enables and activates the device's built-in schedule

---

## Reporting issues

Please report bugs and device compatibility requests at the [issue tracker](https://github.com/briis/ha_tuya_ble/issues).

When reporting a new device, include:
- The **device category** and **product ID** (visible in the Tuya IoT Platform device list)
- The **DP functions and status range** from the device specification page on the platform

---

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.
