"""Exercise the real FD50/base/coordinator lifecycle without HA or Bluetooth.

Only external dependency boundaries are faked. The identity cipher is not an
encryption test: real packet framing, fragmentation, notification parsing,
datapoints, adapter locks, retry tasks, and coordinator callbacks still execute.
"""

# Keep the regression suite runnable with standard-library unittest alone.
# ruff: noqa: PT009, PT027
import asyncio
import importlib.util
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from struct import unpack
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble"
PACKAGE = "_lamomo_lifecycle_testpkg"


def _module(name, **attributes):
    module = ModuleType(name)
    module.__dict__.update(attributes)
    return module


def _load(name, path, *, package=False):
    spec = importlib.util.spec_from_file_location(
        name,
        path,
        submodule_search_locations=[str(path.parent)] if package else None,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _IdentityCipher:
    MODE_CBC = object()

    @staticmethod
    def new(key, _mode, iv):
        if not isinstance(key, bytes) or len(key) not in (16, 24, 32):
            raise ValueError("Synthetic cipher requires a valid AES key")
        if len(iv) != 16:
            raise ValueError("Synthetic cipher requires a valid AES IV")
        return _IdentityCipher()

    @staticmethod
    def encrypt(data):
        return bytes(data)

    @staticmethod
    def decrypt(data):
        return bytes(data)


class _Timer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback(None)


class _CoordinatorBoundary:
    def __class_getitem__(cls, _arguments):
        return cls

    def __init__(self, hass, _logger, _name=None, **_kwargs):
        self.hass = hass
        self.listener_updates = 0
        self.fresh_updates = 0

    def async_update_listeners(self):
        self.listener_updates += 1

    def async_set_updated_data(self, _data):
        self.fresh_updates += 1
        self.async_update_listeners()


@contextmanager
def _actual_integration():
    """Import actual repository modules with isolated external dependency fakes."""
    connection_error = type("SyntheticBleakError", (Exception,), {})
    establish = AsyncMock()

    async def establish_connection(*args, **kwargs):
        client = await establish(*args, **kwargs)
        client.disconnected = args[3]
        return client

    connector = _module(
        "bleak_retry_connector",
        BLEAK_BACKOFF_TIME=1,
        BLEAK_RETRY_EXCEPTIONS=(connection_error,),
        BleakClientWithServiceCache=object,
        BleakError=connection_error,
        BleakNotFoundError=connection_error,
        establish_connection=establish_connection,
    )

    def async_call_later(hass, delay, callback):
        timer = _Timer(delay, callback)
        hass.timers.append(timer)
        return timer.cancel

    registry = _module(
        "homeassistant.helpers.device_registry", CONNECTION_BLUETOOTH="bluetooth"
    )
    stubs = {
        PACKAGE: _module(PACKAGE, __path__=[str(COMPONENT)]),
        "bleak": _module("bleak", __path__=[]),
        "bleak.exc": _module("bleak.exc", BleakDBusError=connection_error),
        "bleak_retry_connector": connector,
        "Crypto": _module("Crypto", __path__=[]),
        "Crypto.Cipher": _module("Crypto.Cipher", AES=_IdentityCipher),
        "tuya_iot": _module(
            "tuya_iot",
            TuyaCloudOpenAPIEndpoint=SimpleNamespace(
                AMERICA="America", EUROPE="Europe", CHINA="China", INDIA="India"
            ),
        ),
        "homeassistant": _module("homeassistant", __path__=[]),
        "homeassistant.const": _module(
            "homeassistant.const", CONF_ADDRESS="address", CONF_DEVICE_ID="device_id"
        ),
        "homeassistant.core": _module(
            "homeassistant.core",
            CALLBACK_TYPE=object,
            HomeAssistant=object,
            callback=lambda function: function,
        ),
        "homeassistant.helpers": _module(
            "homeassistant.helpers", __path__=[], device_registry=registry
        ),
        "homeassistant.helpers.device_registry": registry,
        "homeassistant.helpers.entity": _module(
            "homeassistant.helpers.entity", DeviceInfo=dict, EntityDescription=object
        ),
        "homeassistant.helpers.event": _module(
            "homeassistant.helpers.event", async_call_later=async_call_later
        ),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            CoordinatorEntity=object,
            DataUpdateCoordinator=_CoordinatorBoundary,
        ),
    }
    with patch.dict(sys.modules, stubs):
        _load(PACKAGE + ".const", COMPONENT / "const.py")
        transport = _load(
            PACKAGE + ".tuya_ble", COMPONENT / "tuya_ble" / "__init__.py", package=True
        )
        devices = _load(PACKAGE + ".devices", COMPONENT / "devices.py")
        adapter = _load(PACKAGE + ".lamomo_fd50", COMPONENT / "lamomo_fd50.py")
        with patch.object(adapter, "RECONNECT_DELAY", 0.01, create=True):
            yield SimpleNamespace(
                adapter=adapter,
                devices=devices,
                transport=transport,
                code=sys.modules[PACKAGE + ".tuya_ble.const"].TuyaBLECode,
                establish=establish,
                bleak_error=connection_error,
            )


class _Manager:
    async def get_device_credentials(self, *_args):
        return SimpleNamespace(
            uuid="synthetic_uuid_01",
            local_key="synthetic_key_01",
            device_id="synthetic_device_id",
            category="dd",
            product_id="0qgrjxum",
            device_name="Synthetic strip",
            product_model=None,
            product_name=None,
            functions=[],
            status_range=[],
        )


class _Client:
    """Synthetic BLE peripheral accepting and producing actual framed packets."""

    def __init__(self, context, device, *, report_status=True, srand=b"abcdef"):
        self.context = context
        self.device = device
        self.is_connected = True
        self.report_status = report_status
        self.drop_on_control = False
        self.write_error = None
        self.write_error_codes = set()
        self.hold_device_info = False
        self.held_packets = []
        self.srand = srand
        self.status_requests = 0
        self.write_count = 0
        self.buffer = bytearray()
        self.expected_length = 0
        self.notification_sequence = 100
        self.notify = None
        self.disconnected = None
        self.disconnect_calls = 0
        self.block_first_disconnect = False
        self.disconnect_entered = asyncio.Event()
        self.disconnect_cancelled = False
        self.values = {
            1: (1, b"\x00"),
            2: (4, b"\x00"),
            5: (3, b"000003e803e8"),
        }

    async def start_notify(self, _uuid, callback, **_kwargs):
        self.notify = callback

    async def disconnect(self):
        self.disconnect_calls += 1
        if self.block_first_disconnect and self.disconnect_calls == 1:
            self.disconnect_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.disconnect_cancelled = True
                raise
        self.drop()

    def drop(self):
        self.is_connected = False
        if self.disconnected:
            self.disconnected(self)

    def notification_packets(self, code, data, response_to=0):
        self.notification_sequence += 1
        return self.device._build_packets(
            self.notification_sequence, code, data, response_to
        )

    def emit_packets(self, packets):
        for packet in packets:
            self.notify(0, packet)

    def _notify(self, code, data, response_to=0):
        packets = self.notification_packets(code, data, response_to)
        if code == self.context.code.FUN_SENDER_DEVICE_INFO and self.hold_device_info:
            self.held_packets.extend(packets)
        else:
            self.emit_packets(packets)

    def report(self, dp_ids=(1, 2, 5)):
        for dp_id in dp_ids:
            dtype, raw = self.values[dp_id]
            data = (
                bytes(4)
                + bytes([1, 128, 0, dp_id, dtype])
                + len(raw).to_bytes(2, "big")
                + raw
            )
            self._notify(self.context.code.FUN_RECEIVE_DP_V4, data)

    async def write_gatt_char(self, _uuid, packet, **_kwargs):
        self.write_count += 1
        packet_num, position = self.device._unpack_int(packet, 0)
        if packet_num == 0:
            self.buffer.clear()
            self.expected_length, position = self.device._unpack_int(packet, position)
            position += 1
        self.buffer.extend(packet[position:])
        if len(self.buffer) != self.expected_length:
            return
        # Encryption is an identity external boundary, but framing remains real.
        raw = self.buffer[17:]
        sequence, _response_to, code_value, length = unpack(">IIHH", raw[:12])
        code = self.context.code(code_value)
        data = raw[12 : 12 + length]
        if self.write_error and (
            not self.write_error_codes or code in self.write_error_codes
        ):
            raise self.write_error
        if code == self.context.code.FUN_SENDER_DEVICE_INFO:
            info = bytearray(46)
            info[:6] = bytes([1, 1, 4, 4, 0, 1])
            info[6:12] = self.srand
            self._notify(code, bytes(info), sequence)
        elif code == self.context.code.FUN_SENDER_PAIR:
            self._notify(code, b"\x00", sequence)
        elif code == self.context.code.FUN_SENDER_DPS_V4:
            if self.drop_on_control:
                self.drop()
                return
            self.values[data[5]] = (data[6], bytes(data[9:]))
            self._notify(code, bytes(4) + bytes([data[4], 0]), sequence)
        elif code == self.context.code.FUN_SENDER_DEVICE_STATUS:
            self.status_requests += 1
            self._notify(code, b"\x00", sequence)
            if self.report_status:
                self.report()


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def device(self, context):
        device = context.adapter.LamomoFD50Device(
            _Manager(), SimpleNamespace(address="02:00:00:00:00:01")
        )
        await device.initialize()
        hass = SimpleNamespace(timers=[])
        coordinator = context.devices.TuyaBLECoordinator(hass, device)
        return device, coordinator, hass

    async def connected(self, context):
        device, coordinator, hass = await self.device(context)
        client = _Client(context, device)
        context.establish.return_value = client
        await device.update()
        return device, coordinator, hass, client

    async def settle(self):
        for _ in range(8):
            await asyncio.sleep(0)

    async def eventually(self, predicate):
        async with asyncio.timeout(1):
            # Observe synthetic state within a hard test deadline, not production work.
            while not predicate():  # noqa: ASYNC110
                await asyncio.sleep(0.001)

    async def cleanup(self, device):
        device._expected_disconnect = True
        tasks = list(device._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_startup_failure_has_retry_owner_and_recovers(self):
        with _actual_integration() as context:
            device, coordinator, _hass = await self.device(context)
            client = _Client(context, device)
            context.establish.side_effect = [
                ConnectionError("Temporary radio loss"),
                client,
            ]
            try:
                with self.assertRaises(ConnectionError):
                    await device.update()
                self.assertIsNotNone(device._reconnect_task)
                device.set_ble_device_and_advertisement_data(
                    SimpleNamespace(address=device.address), SimpleNamespace()
                )
                await self.eventually(
                    lambda: device._is_paired and coordinator.connected
                )
                self.assertGreaterEqual(context.establish.await_count, 2)
            finally:
                await self.cleanup(device)

    async def test_start_owns_initial_retry_loop(self):
        with _actual_integration() as context:
            device, coordinator, _hass = await self.device(context)
            client = _Client(context, device)
            context.establish.side_effect = [
                ConnectionError("Temporary radio loss"),
                client,
            ]
            try:
                await device.start()
                self.assertIsNotNone(device._reconnect_task)
                self.assertIn(device._reconnect_task, device._background_tasks)
                await self.eventually(
                    lambda: device._is_paired and coordinator.connected
                )
                self.assertGreaterEqual(context.establish.await_count, 2)
            finally:
                await self.cleanup(device)

    async def test_cached_rollback_preserves_disconnect_timer(self):
        with _actual_integration() as context:
            device, coordinator, hass, client = await self.connected(context)
            client.drop_on_control = True
            context.establish.side_effect = ConnectionError("Device is out of range")
            listeners_before = coordinator.listener_updates
            fresh_before = coordinator.fresh_updates
            try:
                with self.assertRaises(ConnectionError):
                    await device.datapoints[1].set_value(True)
                self.assertIsNone(device._client)
                self.assertFalse(device.datapoints[1].value)
                self.assertGreater(coordinator.listener_updates, listeners_before)
                self.assertEqual(coordinator.fresh_updates, fresh_before)
                self.assertEqual(len(hass.timers), 1)
                self.assertFalse(hass.timers[0].cancelled)
                hass.timers[0].fire()
                self.assertFalse(coordinator.connected)
            finally:
                await self.cleanup(device)

    async def test_connect_failure_rolls_back_without_reviving_unavailable_entity(self):
        with _actual_integration() as context:
            device, coordinator, hass, client = await self.connected(context)
            client.drop()
            hass.timers[-1].fire()
            context.establish.side_effect = ConnectionError("Device is out of range")
            listeners_before = coordinator.listener_updates
            fresh_before = coordinator.fresh_updates
            try:
                with self.assertRaises(ConnectionError):
                    await device.datapoints[1].set_value(True)
                self.assertFalse(device.datapoints[1].value)
                self.assertFalse(coordinator.connected)
                self.assertGreater(coordinator.listener_updates, listeners_before)
                self.assertEqual(coordinator.fresh_updates, fresh_before)
            finally:
                await self.cleanup(device)

    async def test_final_reports_then_disconnect_do_not_lose_active_retry(self):
        with _actual_integration() as context:
            device, _coordinator, _hass, original = await self.connected(context)
            first_retry = _Client(context, device, report_status=False)
            recovered = _Client(context, device)
            context.establish.side_effect = [first_retry, recovered]
            original.drop()
            try:
                await self.eventually(lambda: first_retry.status_requests == 1)
                await self.settle()
                self.assertFalse(device._reconnect_task.done())
                # Both BLE callbacks arrive before the report waiter resumes.
                first_retry.report()
                first_retry.drop()
                await self.eventually(
                    lambda: device._client is recovered and device._is_paired
                )
                self.assertTrue(recovered.is_connected)
            finally:
                await self.cleanup(device)

    async def test_repeated_failures_share_one_retry_task(self):
        with _actual_integration() as context:
            device, _coordinator, _hass = await self.device(context)
            context.establish.side_effect = ConnectionError("Device is out of range")
            try:
                with self.assertRaises(ConnectionError):
                    await device.update()
                retry = device._reconnect_task
                self.assertIsNotNone(retry)
                for _ in range(2):
                    with self.assertRaises(ConnectionError):
                        await device.update()
                    self.assertIs(device._reconnect_task, retry)
                self.assertEqual(device._background_tasks, {retry})
            finally:
                await self.cleanup(device)

    async def test_stop_cancels_retry_and_prevents_new_attempts(self):
        with _actual_integration() as context:
            device, _coordinator, _hass, client = await self.connected(context)
            context.establish.side_effect = ConnectionError("Device is out of range")
            client.drop()
            try:
                await self.eventually(lambda: context.establish.await_count >= 2)
                retry = device._reconnect_task
                await device.stop()
                await self.settle()
                self.assertTrue(retry.done())
                self.assertFalse(device._background_tasks)
                calls_at_stop = context.establish.await_count
                with self.assertRaises(ConnectionError):
                    await device.update()
                await asyncio.sleep(0.02)
                self.assertEqual(context.establish.await_count, calls_at_stop)
                self.assertIsNone(device._client)
            finally:
                await self.cleanup(device)

    async def test_stop_promptly_cancels_in_flight_retry_connection(self):
        with _actual_integration() as context:
            device, _coordinator, _hass, client = await self.connected(context)
            entered = asyncio.Event()

            async def blocked_connection(*_args, **_kwargs):
                entered.set()
                await asyncio.Event().wait()

            context.establish.side_effect = blocked_connection
            client.drop()
            try:
                await asyncio.wait_for(entered.wait(), 1)
                retry = device._reconnect_task
                await asyncio.wait_for(device.stop(), 0.3)
                self.assertTrue(retry.done())
                self.assertTrue(all(task.done() for task in device._background_tasks))
                await self.settle()
                self.assertFalse(device._background_tasks)
                self.assertIsNone(device._client)
                self.assertFalse(device._connect_lock.locked())
            finally:
                await self.cleanup(device)

    async def test_real_reconnect_rearms_next_disconnect_timer(self):
        with _actual_integration() as context:
            device, coordinator, hass, client = await self.connected(context)
            next_client = _Client(context, device)
            context.establish.return_value = next_client
            client.drop()
            try:
                await self.eventually(
                    lambda: device._client is next_client and device._is_paired
                )
                await self.settle()
                self.assertTrue(hass.timers[0].cancelled)
                next_client.drop()
                self.assertEqual(len(hass.timers), 2)
                hass.timers[1].fire()
                self.assertFalse(coordinator.connected)
            finally:
                await self.cleanup(device)

    async def test_failed_connected_session_is_replaced_before_retry(self):
        for error_kind in ("bleak", "connection"):
            with self.subTest(error_kind=error_kind), _actual_integration() as context:
                device, _coordinator, _hass, original = await self.connected(context)
                await self.settle()
                recovered = _Client(context, device, srand=b"uvwxyz")
                context.establish.return_value = recovered
                error_type = (
                    context.bleak_error if error_kind == "bleak" else ConnectionError
                )
                original.write_error = error_type(
                    "GATT session failed but link flag stayed true"
                )
                try:
                    with self.assertRaises(error_type):
                        await device.datapoints[1].set_value(True)
                    await self.eventually(
                        lambda device=device, recovered=recovered: (
                            device._client is recovered and device._is_paired
                        )
                    )
                    self.assertFalse(original.is_connected)
                    self.assertGreaterEqual(context.establish.await_count, 2)
                    await device.datapoints[1].set_value(True)
                    self.assertTrue(device.datapoints[1].value)
                finally:
                    await self.cleanup(device)

    async def test_bleak_notification_ack_error_is_handled_and_recovers(self):
        with _actual_integration() as context:
            device, _coordinator, _hass, original = await self.connected(context)
            await self.settle()
            recovered = _Client(context, device, srand=b"uvwxyz")
            context.establish.return_value = recovered
            original.write_error = context.bleak_error(
                "Synthetic notification ACK failure"
            )
            original.write_error_codes = {context.code.FUN_RECEIVE_DP_V4}
            unhandled = []
            loop = asyncio.get_running_loop()
            previous_handler = loop.get_exception_handler()
            loop.set_exception_handler(lambda _loop, error: unhandled.append(error))
            try:
                original.report((1,))
                await self.settle()
                self.assertEqual(unhandled, [])
                await self.eventually(
                    lambda: device._client is recovered and device._is_paired
                )
                self.assertFalse(original.is_connected)
                self.assertIsNotNone(device._reconnect_task)
            finally:
                await self.cleanup(device)
                loop.set_exception_handler(previous_handler)

    async def test_stop_cancels_external_control_inner_connection(self):
        with _actual_integration() as context:
            device, _coordinator, _hass, original = await self.connected(context)
            await self.settle()
            entered = asyncio.Event()
            release = asyncio.Event()
            recovered = _Client(context, device)
            attempts = 0

            async def connection(*_args, **_kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise ConnectionError("Initial retry failed")
                entered.set()
                await release.wait()
                return recovered

            context.establish.side_effect = connection
            control = None
            with patch.object(context.adapter, "RECONNECT_DELAY", 10):
                original.drop()
                try:
                    await self.eventually(lambda: attempts == 1)
                    await self.settle()
                    # HA owns this outer service task; its inner BLE work must
                    # still be cancellable by the device during entry unload.
                    control = asyncio.create_task(device.datapoints[1].set_value(True))
                    await asyncio.wait_for(entered.wait(), 1)
                    self.assertTrue(device._connect_lock.locked())
                    await asyncio.wait_for(device.stop(), 0.3)
                    release.set()
                    await self.settle()
                    self.assertTrue(control.done())
                    self.assertIsNone(device._client)
                    self.assertFalse(device._connect_lock.locked())
                    calls_at_stop = context.establish.await_count
                    await asyncio.sleep(0.02)
                    self.assertEqual(context.establish.await_count, calls_at_stop)
                finally:
                    release.set()
                    if control is not None:
                        control.cancel()
                        await asyncio.gather(control, return_exceptions=True)
                    await self.cleanup(device)

    async def test_initial_pair_and_partial_reports_remain_unavailable(self):
        with _actual_integration() as context:
            device, coordinator, _hass = await self.device(context)
            client = _Client(context, device, report_status=False)
            context.establish.return_value = client
            update = asyncio.create_task(device.update())
            try:
                await self.eventually(lambda: client.status_requests == 1)
                await self.settle()
                self.assertTrue(device._is_paired)
                readiness = [coordinator.connected]
                for dp_id in (1, 2, 5):
                    client.report((dp_id,))
                    await self.settle()
                    readiness.append(coordinator.connected)
                await asyncio.wait_for(update, 1)
                self.assertEqual(readiness, [False, False, False, True])
            finally:
                update.cancel()
                await asyncio.gather(update, return_exceptions=True)
                await self.cleanup(device)

    async def test_old_device_info_callback_cannot_replace_new_session(self):
        with _actual_integration() as context:
            device, _coordinator, _hass = await self.device(context)
            original = _Client(context, device, srand=b"abcdef")
            original.hold_device_info = True
            recovered = _Client(context, device, srand=b"uvwxyz")
            context.establish.side_effect = [original, recovered]
            update = asyncio.create_task(device.update())
            try:
                await self.eventually(lambda: bool(original.held_packets))
                original.drop()
                with self.assertRaises(ConnectionError):
                    await update
                await self.eventually(
                    lambda: device._client is recovered and device._is_paired
                )
                await self.settle()
                session = device._session_key
                original.emit_packets(original.held_packets)
                self.assertIs(device._client, recovered)
                self.assertEqual(device._session_key, session)
            finally:
                update.cancel()
                await asyncio.gather(update, return_exceptions=True)
                await self.cleanup(device)

    async def test_retained_notification_callback_is_inert_after_stop(self):
        with _actual_integration() as context:
            device, coordinator, _hass, client = await self.connected(context)
            await self.settle()
            info = bytearray(46)
            info[:6] = bytes([1, 1, 4, 4, 0, 1])
            info[6:12] = b"abcdef"
            late_info = client.notification_packets(
                context.code.FUN_SENDER_DEVICE_INFO, bytes(info), 1
            )
            late_power = client.notification_packets(
                context.code.FUN_RECEIVE_DP_V4,
                bytes(4) + bytes([1, 128, 0, 1, 1, 0, 1, 1]),
            )
            try:
                await device.stop()
                reports = device._report_versions.copy()
                updates = coordinator.listener_updates
                client.emit_packets(late_info)
                client.emit_packets(late_power)
                self.assertIsNone(device._session_key)
                self.assertEqual(device._report_versions, reports)
                self.assertFalse(device.datapoints[1].value)
                self.assertEqual(coordinator.listener_updates, updates)
                self.assertFalse(device._background_tasks)
            finally:
                await self.cleanup(device)

    async def test_queued_old_session_ack_does_not_write_to_new_client(self):
        with _actual_integration() as context:
            device, _coordinator, _hass, original = await self.connected(context)
            await self.settle()
            recovered = _Client(context, device, srand=b"uvwxyz")
            context.establish.return_value = recovered
            queued_ack = device._send_response(context.code.FUN_RECEIVE_DP_V4, b"", 777)
            try:
                original.drop()
                await self.eventually(
                    lambda: device._client is recovered and device._is_paired
                )
                await self.settle()
                writes_before = recovered.write_count
                await queued_ack
                self.assertEqual(recovered.write_count, writes_before)
            finally:
                queued_ack.close()
                await self.cleanup(device)

    async def test_stop_closes_client_whose_control_error_cleanup_was_cancelled(self):
        with _actual_integration() as context:
            device, _coordinator, _hass, original = await self.connected(context)
            await self.settle()
            original.write_error = context.bleak_error("Control GATT write failed")
            original.write_error_codes = {context.code.FUN_SENDER_DPS_V4}
            original.block_first_disconnect = True
            control = asyncio.create_task(device.datapoints[1].set_value(True))
            try:
                await asyncio.wait_for(original.disconnect_entered.wait(), 1)
                await asyncio.wait_for(device.stop(), 0.3)
                await self.settle()
                self.assertTrue(original.disconnect_cancelled)
                self.assertFalse(original.is_connected)
                self.assertGreaterEqual(original.disconnect_calls, 2)
                self.assertTrue(control.done())
                self.assertTrue(all(task.done() for task in device._background_tasks))
                self.assertIsNone(device._client)
                self.assertFalse(device._connect_lock.locked())
            finally:
                control.cancel()
                await asyncio.gather(control, return_exceptions=True)
                await self.cleanup(device)
                if original.is_connected:
                    await original.disconnect()

    async def test_stop_closes_client_whose_auth_error_cleanup_was_cancelled(self):
        with _actual_integration() as context:
            device, _coordinator, _hass = await self.device(context)
            original = _Client(context, device)
            original.write_error = context.bleak_error("Device info GATT write failed")
            original.write_error_codes = {context.code.FUN_SENDER_DEVICE_INFO}
            original.block_first_disconnect = True
            context.establish.return_value = original
            update = asyncio.create_task(device.update())
            try:
                await asyncio.wait_for(original.disconnect_entered.wait(), 1)
                await asyncio.wait_for(device.stop(), 0.3)
                await self.settle()
                self.assertTrue(original.disconnect_cancelled)
                self.assertFalse(original.is_connected)
                self.assertGreaterEqual(original.disconnect_calls, 2)
                self.assertTrue(update.done())
                self.assertTrue(all(task.done() for task in device._background_tasks))
                self.assertIsNone(device._client)
                self.assertFalse(device._connect_lock.locked())
            finally:
                update.cancel()
                await asyncio.gather(update, return_exceptions=True)
                await self.cleanup(device)
                if original.is_connected:
                    await original.disconnect()


if __name__ == "__main__":
    unittest.main()
