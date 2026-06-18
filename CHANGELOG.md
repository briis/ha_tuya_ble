# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [2.1.3] — 2026-06-18

### Fixed

- **"Unknown error occurred" when configuring an unavailable device** — `_try_login` used a bare `next()` with no default, causing a `StopIteration` → `RuntimeError` when the stored country was a raw country code (`"DK"`) rather than a name (`"Denmark"`). Now returns an `invalid_auth` error gracefully instead of crashing.
- **Options flow silently re-displayed the login form** when the device's integration entry was not loaded (BLE device unavailable during setup retry). Now shows an explicit *"integration not loaded — restart Home Assistant"* error.
- **Password pre-fill** removed from the options flow cloud login form (was already fixed in the config flow in 2.1.2; now consistent in both places).

### Added

- **"Update method" selector in Configure dialog for manual entries** — clicking *Configure* on a manually-added device now shows a choice between *Edit credentials manually* (existing behaviour, pre-filled form) and *Fetch updated credentials from Tuya cloud* (runs the cloud login flow without needing to delete and re-add the device).

---

## [2.1.2] — 2026-06-18

### Fixed

- Manual entry form: added helper text for the optional *Product name* and *Model* fields in all three languages, matching the descriptions already present on the required fields.
- Cloud login form: password field no longer pre-fills from the Tuya integration credential cache, preventing confusing dots from appearing in an apparently empty field.

---

## [2.1.1] — 2026-06-18

### Added

- **Manual device entry** — devices can now be added without a Tuya cloud login by entering credentials directly in the config flow. When you choose *"Enter device credentials manually"* you are prompted for the MAC address, UUID, local key, device ID, category code, product ID, and optionally product name and model. All of these fields are present in a standard [tinytuya](https://github.com/jasonacox/tinytuya) `devices.json` export.
- **Options flow for manual devices** — opening *Configure* on a manually-added entry now shows a pre-filled credential edit form instead of the cloud login form, making it easy to update the local key if it rotates.
- **Danish translation** (`da.json`) — full translation of all config flow steps, error messages, and entity strings.
- **German translation** (`de.json`) — full translation of all config flow steps, error messages, and entity strings.
- **README: manual entry guide** — new *Option B* section explains how to export credentials with `tinytuya wizard` and maps every JSON field to the corresponding form field.

### Changed

- The initial config flow user step now presents a setup method selector (*Auto-discover via Bluetooth* vs *Enter device credentials manually*) instead of going straight to the cloud login form.

---

## [2.1.0] — 2026-06-17

First public release of this fork, based on [airy10/ha_tuya_ble](https://github.com/airy10/ha_tuya_ble) which itself originates from [PlusPlus-ua/ha_tuya_ble](https://github.com/PlusPlus-ua/ha_tuya_ble).

### Added

- **Cover platform** — support for curtain motors and blinds (`cl`, `clkg`, `jdcljqr` categories), including open/close/stop and explicit position control.
- **All other platforms carried over** from the upstream integration: binary sensor, button, climate (TRV), light (strip lights), lock, number, select, sensor, switch, and text (Fingerbot program sequences).
- **Signal strength sensor** (disabled by default) on every device.
- **`tuya_ble/` low-level library** — BLE protocol implementation with AES encryption, chunked packet reassembly, and fire-and-forget task tracking via a `_background_tasks` set.
- **Full lint pass** — brought `scripts/lint` (ruff format + ruff check) from ~100 errors to zero; suppressions documented in `.ruff.toml`.

### Fixed

- BLE device discovery now checks both `service_data` and `service_uuids` advertisement fields, fixing devices that advertise the Tuya service UUID in the latter.
- `EnumTypeData.from_json` accepts a pre-parsed `dict` in addition to a raw JSON string, fixing a crash when setting up covers whose DP spec was already decoded by the cloud client.
- `get_category_mapping` returns an empty tuple instead of `None` when no mapping is found, preventing a `TypeError` during cover entity setup.
- Protocol error messages downgraded from `ERROR` to `WARNING` (with `exc_info=True`) to avoid flooding the HA error log during normal BLE reconnection churn.
- Corrected log message typos: *"Unexpcted packet"* → *"Unexpected packet"* (×2).

---

[2.1.3]: https://github.com/briis/ha_tuya_ble/compare/v2.1.2...v2.1.3
[2.1.2]: https://github.com/briis/ha_tuya_ble/compare/v2.1.1...v2.1.2
[2.1.1]: https://github.com/briis/ha_tuya_ble/compare/v2.1.0...v2.1.1
[2.1.0]: https://github.com/briis/ha_tuya_ble/releases/tag/v2.1.0
