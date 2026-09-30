"""Execute integration setup with mocked Home Assistant and BLE boundaries.

These tests use the actual integration setup/factory source, not a live HA
process. Credentials and addresses are synthetic; no network calls occur.
"""

import asyncio
import importlib.util
import sys
import unittest
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

# Keep these tests executable with standard-library unittest as well as pytest.
# ruff: noqa: PT009, PT027

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble"
TEST_ADDRESS = "02:00:00:00:00:01"


def _module(name, **attributes):
    module = ModuleType(name)
    module.__dict__.update(attributes)
    return module


@contextmanager
def _integration():
    package = "_tuya_ble_setup_testpkg"
    device_instances = []
    managers = []

    class Manager:
        def __init__(self, hass, options):
            self.options = options
            self.credentials = hass.credentials
            self.get_device_credentials = AsyncMock(return_value=self.credentials)
            managers.append(self)

    class LegacyDevice:
        def __init__(self, manager, ble_device):
            self.manager = manager
            self.ble_device = ble_device
            self._device_info = None
            self.initialize_calls = 0
            self.stop = AsyncMock()
            self.start = AsyncMock()
            device_instances.append(self)

        async def initialize(self):
            self.initialize_calls += 1
            if self._device_info is None:
                self._device_info = await self.manager.get_device_credentials(
                    self.ble_device.address, False
                )

        @property
        def product_id(self):
            return self._device_info.product_id if self._device_info else ""

        async def update(self):
            pass

    class LamomoDevice(LegacyDevice):
        pass

    class Platform(Enum):
        BUTTON = "button"
        CLIMATE = "climate"
        COVER = "cover"
        LOCK = "lock"
        NUMBER = "number"
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"
        LIGHT = "light"
        SELECT = "select"
        SWITCH = "switch"
        TEXT = "text"

    class NotReadyError(RuntimeError):
        pass

    def device_data(title, device, product_info, manager, coordinator):
        return SimpleNamespace(
            title=title,
            device=device,
            product_info=product_info,
            manager=manager,
            coordinator=coordinator,
        )

    ble_device = SimpleNamespace(address=TEST_ADDRESS)
    bluetooth = _module(
        "homeassistant.components.bluetooth",
        async_ble_device_from_address=Mock(return_value=ble_device),
        async_register_callback=Mock(return_value=Mock()),
        BluetoothScanningMode=SimpleNamespace(ACTIVE="active"),
    )
    root_package = _module(package, __path__=[str(COMPONENT)])
    stubs = {
        package: root_package,
        package + ".cloud": _module(
            package + ".cloud", HASSTuyaBLEDeviceManager=Manager
        ),
        package + ".const": _module(package + ".const", DOMAIN="tuya_ble"),
        package + ".devices": _module(
            package + ".devices",
            TuyaBLECoordinator=Mock(return_value=SimpleNamespace()),
            TuyaBLEData=device_data,
            get_device_product_info=Mock(return_value=SimpleNamespace()),
        ),
        package + ".tuya_ble": _module(
            package + ".tuya_ble", TuyaBLEDevice=LegacyDevice
        ),
        package + ".lamomo_fd50": _module(
            package + ".lamomo_fd50",
            LamomoFD50Device=LamomoDevice,
            PRODUCT_ID="0qgrjxum",
            LAMOMO_PRODUCT_ID="0qgrjxum",
        ),
        "bleak_retry_connector": _module(
            "bleak_retry_connector", get_device=AsyncMock(return_value=ble_device)
        ),
        "homeassistant": _module("homeassistant", __path__=[]),
        "homeassistant.components": _module(
            "homeassistant.components", bluetooth=bluetooth
        ),
        "homeassistant.components.bluetooth": bluetooth,
        "homeassistant.components.bluetooth.match": _module(
            "homeassistant.components.bluetooth.match",
            ADDRESS="address",
            BluetoothCallbackMatcher=dict,
        ),
        "homeassistant.const": _module(
            "homeassistant.const",
            CONF_ADDRESS="address",
            EVENT_HOMEASSISTANT_STOP="stop",
            Platform=Platform,
        ),
        "homeassistant.core": _module(
            "homeassistant.core",
            Event=object,
            HomeAssistant=object,
            callback=lambda callback: callback,
        ),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions", ConfigEntryNotReady=NotReadyError
        ),
    }
    spec = importlib.util.spec_from_file_location(
        package, COMPONENT / "__init__.py", submodule_search_locations=[str(COMPONENT)]
    )
    integration = importlib.util.module_from_spec(spec)
    stubs[package] = integration
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(integration)
        yield SimpleNamespace(
            integration=integration,
            legacy=LegacyDevice,
            lamomo=LamomoDevice,
            managers=managers,
            devices=device_instances,
        )


def _entry(options):
    return SimpleNamespace(
        data={"address": TEST_ADDRESS},
        options=options,
        entry_id="synthetic-entry",
        title="Synthetic strip",
        async_on_unload=Mock(),
        add_update_listener=Mock(return_value=Mock()),
    )


def _hass(product_id):
    credentials = SimpleNamespace(product_id=product_id)
    jobs = []

    def add_job(coroutine):
        jobs.append(coroutine)
        coroutine.close()  # Do not start background BLE work in a setup test.

    return SimpleNamespace(
        credentials=credentials,
        jobs=jobs,
        data={},
        add_job=add_job,
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
        bus=SimpleNamespace(async_listen_once=Mock(return_value=Mock())),
    )


class IntegrationSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_lamomo_platform_setup_stops_without_start_or_retained_data(
        self,
    ):
        for error_type in (RuntimeError, asyncio.CancelledError):
            with self.subTest(error_type=error_type), _integration() as context:
                entry = _entry({"product_id": "0qgrjxum"})
                hass = _hass("0qgrjxum")
                hass.config_entries.async_forward_entry_setups.side_effect = error_type(
                    "Synthetic platform setup failure"
                )
                with self.assertRaises(error_type):
                    await context.integration.async_setup_entry(hass, entry)
                device = context.devices[-1]
                self.assertEqual(
                    (
                        device.start.await_count,
                        device.stop.await_count,
                        entry.entry_id in hass.data.get("tuya_ble", {}),
                    ),
                    (0, 1, False),
                )
                self.assertEqual(hass.jobs, [])

    async def test_lamomo_start_follows_platform_setup_and_stop_hook_registration(self):
        with _integration() as context:
            entry = _entry({"product_id": "0qgrjxum"})
            hass = _hass("0qgrjxum")
            forwarded = False

            async def forward(_entry, _platforms):
                nonlocal forwarded
                device = context.devices[-1]
                device.start.assert_not_awaited()

                async def start():
                    self.assertTrue(forwarded)
                    hass.bus.async_listen_once.assert_called_once()

                device.start.side_effect = start
                forwarded = True

            hass.config_entries.async_forward_entry_setups.side_effect = forward
            self.assertTrue(await context.integration.async_setup_entry(hass, entry))
            device = hass.data["tuya_ble"][entry.entry_id].device
            device.start.assert_awaited_once()
            device.stop.assert_not_awaited()

    async def test_manual_lamomo_uses_product_adapter(self):
        with _integration() as context:
            entry = _entry({"product_id": "0qgrjxum"})
            hass = _hass("0qgrjxum")
            self.assertTrue(await context.integration.async_setup_entry(hass, entry))
            device = hass.data["tuya_ble"][entry.entry_id].device
            self.assertIsInstance(device, context.lamomo)
            self.assertEqual(device.initialize_calls, 1)
            device.start.assert_awaited_once()
            self.assertEqual(hass.jobs, [])
            hass.config_entries.async_forward_entry_setups.assert_awaited_once()

    async def test_resolved_credentials_select_lamomo_without_product_option(self):
        with _integration() as context:
            entry = _entry({"access_id": "synthetic-cloud-account"})
            hass = _hass("0qgrjxum")
            self.assertTrue(await context.integration.async_setup_entry(hass, entry))
            device = hass.data["tuya_ble"][entry.entry_id].device
            self.assertIsInstance(device, context.lamomo)
            self.assertEqual(device.initialize_calls, 1)

    async def test_other_product_retains_legacy_controller(self):
        with _integration() as context:
            entry = _entry({"product_id": "synthetic-legacy-product"})
            hass = _hass("synthetic-legacy-product")
            self.assertTrue(await context.integration.async_setup_entry(hass, entry))
            device = hass.data["tuya_ble"][entry.entry_id].device
            self.assertIs(type(device), context.legacy)
            self.assertEqual(device.initialize_calls, 1)
            device.start.assert_not_awaited()
            self.assertEqual(len(hass.jobs), 1)

    async def test_unload_stops_only_created_controller(self):
        with _integration() as context:
            entry = _entry({"product_id": "synthetic-legacy-product"})
            hass = _hass("synthetic-legacy-product")
            await context.integration.async_setup_entry(hass, entry)
            device = hass.data["tuya_ble"][entry.entry_id].device
            self.assertTrue(await context.integration.async_unload_entry(hass, entry))
            device.stop.assert_awaited_once()
            self.assertNotIn(entry.entry_id, hass.data["tuya_ble"])


class DeviceFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_lamomo_product_replaces_initialized_legacy_controller(self):
        with _integration() as context:
            hass = _hass("0qgrjxum")
            manager = context.integration.HASSTuyaBLEDeviceManager(hass, {})
            device = await context.integration.async_create_device(
                manager, SimpleNamespace(address=TEST_ADDRESS)
            )
            self.assertIsInstance(device, context.lamomo)
            self.assertEqual(device.initialize_calls, 1)
            self.assertEqual(len(context.devices), 2)
            self.assertIs(type(context.devices[0]), context.legacy)
            self.assertEqual(context.devices[0].initialize_calls, 1)

    async def test_legacy_product_is_initialized_once_without_adapter(self):
        with _integration() as context:
            hass = _hass("synthetic-legacy-product")
            manager = context.integration.HASSTuyaBLEDeviceManager(hass, {})
            device = await context.integration.async_create_device(
                manager, SimpleNamespace(address=TEST_ADDRESS)
            )
            self.assertIs(type(device), context.legacy)
            self.assertEqual(device.initialize_calls, 1)
            self.assertEqual(len(context.devices), 1)
            manager.get_device_credentials.assert_awaited_once()

    async def test_unknown_product_retains_legacy_fallback(self):
        with _integration() as context:
            hass = _hass("")
            manager = context.integration.HASSTuyaBLEDeviceManager(hass, {})
            device = await context.integration.async_create_device(
                manager, SimpleNamespace(address=TEST_ADDRESS)
            )
            self.assertIs(type(device), context.legacy)
            self.assertEqual(len(context.devices), 1)

    async def test_credential_initialization_error_propagates_without_adapter(self):
        with _integration() as context:
            manager = context.integration.HASSTuyaBLEDeviceManager(
                _hass("0qgrjxum"), {}
            )
            with (
                patch.object(
                    context.legacy, "initialize", AsyncMock(side_effect=ConnectionError)
                ),
                self.assertRaises(ConnectionError),
            ):
                await context.integration.async_create_device(
                    manager, SimpleNamespace(address=TEST_ADDRESS)
                )
            self.assertEqual(len(context.devices), 1)

    async def test_lamomo_initialization_error_is_not_silently_downgraded(self):
        with _integration() as context:
            manager = context.integration.HASSTuyaBLEDeviceManager(
                _hass("0qgrjxum"), {}
            )
            with (
                patch.object(
                    context.lamomo, "initialize", AsyncMock(side_effect=ValueError)
                ),
                self.assertRaises(ValueError),
            ):
                await context.integration.async_create_device(
                    manager, SimpleNamespace(address=TEST_ADDRESS)
                )
            self.assertEqual(len(context.devices), 2)


if __name__ == "__main__":
    unittest.main()
