# CLAUDE.md — Project context for AI assistants

## What this is

A Home Assistant custom component (HACS integration) that adds local BLE support for Tuya devices. It bridges BLE communication (direct, push-based) with Tuya's cloud API — the cloud is used only to fetch device credentials (local key); after that all communication is local over Bluetooth.

- **Domain:** `tuya_ble`
- **IoT class:** `local_push`
- **BLE service UUID:** `0000a201-0000-1000-8000-00805f9b34fb`
- **Min HA version:** 2026.3.2
- **Min HACS version:** 2.0.5
- **HA dependency:** requires the built-in `tuya` integration to be configured (for cloud-based setup; not required for manual device entry)

## Running lint

```bash
bash scripts/lint
```

This runs `ruff format .` then `ruff check . --fix`. Both must pass with zero errors. The config is in [`.ruff.toml`](.ruff.toml) at the project root (not `ruff.toml` — `.ruff.toml` takes precedence).

## Linting history (June 2026)

A full lint pass was done to bring `scripts/lint` from ~100+ errors to zero. Key decisions:

### Rules added to `.ruff.toml` ignore list

These were suppressed rather than fixed because fixing them would require large structural changes to the integration API:

| Rule(s) | Reason suppressed |
|---|---|
| `D100–D107, D205, D401` | Docstrings on every public member — excessive for an HA component |
| `FBT001, FBT002, FBT003` | Boolean function args — HA callbacks have fixed signatures |
| `PLR2004` | Magic values — device DP IDs are protocol constants, not refactorable |
| `PLR0912, PLR0913, PLR0915, PLR0911` | Complexity limits — existing logic is inherently complex |
| `ANN001–ANN003, ANN201, ANN202` | Type annotations on all args — not standard in HA integrations |
| `A001, A002` | Shadow builtins — `list`, `type` args are idiomatic in HA |
| `SLF001` | Private member access — cross-entity access is normal in HA |
| `EXE001, EXE003, EXE005` | Shebang rules — not applicable to Python packages |
| `S105, S101, S110, S324` | Security false positives — S324 specifically for MD5 in Tuya key derivation (protocol-mandated, cannot change) |
| `TRY003, EM101, EM102` | Exception message style — highly opinionated, no real benefit |
| `BLE001` | Broad `except Exception` — BLE connection handlers must catch all protocol errors |

### Real fixes made across 17 files

- **ERA001** — removed 14 commented-out code blocks
- **E722** — 6 bare `except:` → `except Exception:`
- **G201** — 7 `_LOGGER.error(..., exc_info=True)` → `_LOGGER.exception(...)`
- **RUF006** — added `_create_task()` helper + `_background_tasks: set` to `TuyaBLEDevice` to properly track 13 fire-and-forget asyncio tasks
- **SIM102** — collapsed 6 nested `if` blocks
- **PLW0602** — removed 5 unnecessary `global` declarations
- **ARG001/ARG002** — prefixed 13 unused callback parameters with `_`
- **PERF401** — converted 11 for-append loops to list comprehensions or `extend`
- **F401/F811** — removed 3 unused/duplicate imports
- **F821** — added missing `color_util` import in `light.py`; changed `List` → `list` in `manager.py` (was importing from `typing` without the import)
- **B018/B904** — removed useless expression; added `from None` to bare re-raise
- **PLW2901/PLC0206/F601** — fixed loop variable overwrite, dict iteration pattern, duplicate dict key in `select.py`

## Config flow

The config flow (`config_flow.py`) has two setup paths selectable at the first step:

### Cloud path (auto-discover)
`async_step_user` → `async_step_login` → `async_step_device`

The user logs in with Tuya IoT Platform credentials. HA scans for nearby BLE devices advertising the Tuya service UUID and lets the user pick one. Credentials are fetched from the cloud cache and stored in the config entry options.

### Manual path
`async_step_user` → `async_step_manual`

The user enters all device credentials directly (MAC address, UUID, local key, device ID, category, product ID, and optionally product name/model). No cloud login is required. Credentials are stored directly in options, and `HASSTuyaBLEDeviceManager.get_device_credentials()` returns them immediately via the `_has_credentials()` fast-path without any cloud call.

The MAC address is validated against `_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")` and normalised to uppercase.

### Options flow
`TuyaBLEOptionsFlow.async_step_init` detects which path was used by checking whether `CONF_ACCESS_ID` is present in the entry options:
- Cloud entries → `async_step_login` (re-authenticate with Tuya cloud)
- Manual entries → `async_step_manual` (edit credentials form, pre-filled with current values)

### Credential storage
| Key | Source |
|---|---|
| `CONF_ADDRESS` | BLE MAC address (also in `entry.data`) |
| `CONF_UUID` | Tuya device UUID |
| `CONF_LOCAL_KEY` | BLE encryption key |
| `CONF_DEVICE_ID` | Tuya cloud device ID |
| `CONF_CATEGORY` | Category code (e.g. `cl`, `ms`) |
| `CONF_PRODUCT_ID` | Product ID |
| `CONF_DEVICE_NAME` | Display name |
| `CONF_PRODUCT_NAME` | Product/commercial name (optional) |
| `CONF_PRODUCT_MODEL` | Hardware model (optional) |

Cloud entries additionally store login keys (`CONF_ACCESS_ID`, `CONF_ACCESS_SECRET`, `CONF_ENDPOINT`, etc.) in options. Manual entries do not.

## Translations

Translation files live in `translations/`. The following locales are provided:

| File | Language |
|---|---|
| `en.json` | English (canonical) |
| `da.json` | Danish |
| `de.json` | German |

`strings.json` is the HA-internal source file that uses `[%key:...]` references; `translations/en.json` carries the resolved English strings. Both must be kept in sync when adding new steps or fields.

## File structure

```
custom_component/tuya_ble/
├── __init__.py          # Integration setup, coordinator wiring
├── config_flow.py       # UI config flow — cloud discovery or manual credential entry
├── cloud.py             # Tuya cloud API client (credential fetching + caching)
├── const.py             # Domain constants, DPCode/DPType enums
├── devices.py           # Device database, TuyaBLEEntity base class, coordinator
├── base.py              # IntegerTypeData, EnumTypeData value type helpers
├── util.py              # Shared utilities
├── binary_sensor.py     # Binary sensor platform
├── button.py            # Button platform
├── climate.py           # Climate platform (TRV)
├── cover.py             # Cover platform (curtains/blinds)
├── light.py             # Light platform (strip lights)
├── lock.py              # Lock platform
├── number.py            # Number platform (Fingerbot position/hold-time settings)
├── select.py            # Select platform (Fingerbot mode)
├── sensor.py            # Sensor platform
├── switch.py            # Switch platform
├── text.py              # Text platform (Fingerbot program sequences)
├── strings.json         # HA-internal translation source (uses %key: references)
├── translations/
│   ├── en.json          # English (resolved strings, must mirror strings.json)
│   ├── da.json          # Danish
│   └── de.json          # German
└── tuya_ble/            # Low-level BLE protocol library
    ├── __init__.py
    ├── tuya_ble.py      # Core BLE device class, connection management
    └── manager.py       # Device manager dataclasses (TuyaBLEDeviceCredentials)
```

## Device database

Device categories and product IDs are registered in [`devices.py`](custom_component/tuya_ble/devices.py) in the `devices_database` dict. Each entry maps a Tuya category code (e.g. `"szjqr"`) to a `TuyaBLECategoryInfo` containing product-ID-specific `TuyaBLEProductInfo` entries.

Entity mappings (which DP IDs map to which HA entities) live in the per-platform files (`sensor.py`, `switch.py`, etc.) in a top-level `mapping` dict keyed by category code.

## Adding a new device

1. Add an entry to `devices_database` in `devices.py`
2. Add DP mappings to whichever platform files are relevant
3. Run `bash scripts/lint` to verify no regressions

## Adding a new translation step or field

1. Add the new step/field to `strings.json` (use `[%key:...]` references for anything shared)
2. Add the resolved English text to `translations/en.json`
3. Add the translated text to `translations/da.json` and `translations/de.json`
4. Run `bash scripts/lint` to verify no regressions
