# CLAUDE.md — Project context for AI assistants

## What this is

A Home Assistant custom component (HACS integration) that adds local BLE support for Tuya devices. It bridges BLE communication (direct, push-based) with Tuya's cloud API — the cloud is used only to fetch device credentials (local key); after that all communication is local over Bluetooth.

- **Domain:** `tuya_ble`
- **IoT class:** `local_push`
- **BLE service UUID:** `0000a201-0000-1000-8000-00805f9b34fb`
- **Min HA version:** 2026.3.2
- **Min HACS version:** 2.0.5
- **HA dependency:** requires the built-in `tuya` integration to be configured

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

## File structure

```
custom_component/tuya_ble/
├── __init__.py          # Integration setup, coordinator wiring
├── config_flow.py       # UI config flow (Tuya cloud credentials → BLE device)
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
└── tuya_ble/            # Low-level BLE protocol library
    ├── __init__.py
    ├── tuya_ble.py      # Core BLE device class, connection management
    └── manager.py       # Device manager dataclasses
```

## Device database

Device categories and product IDs are registered in [`devices.py`](custom_component/tuya_ble/devices.py) in the `devices_database` dict. Each entry maps a Tuya category code (e.g. `"szjqr"`) to a `TuyaBLECategoryInfo` containing product-ID-specific `TuyaBLEProductInfo` entries.

Entity mappings (which DP IDs map to which HA entities) live in the per-platform files (`sensor.py`, `switch.py`, etc.) in a top-level `mapping` dict keyed by category code.

## Adding a new device

1. Add an entry to `devices_database` in `devices.py`
2. Add DP mappings to whichever platform files are relevant
3. Run `bash scripts/lint` to verify no regressions
