"""Pure local adapter tests; no HA imports, BLE calls, or credentials.

Run with: python3 -m unittest discover -s tests -p test_lamomo_fd50.py -v
The dependency stubs only exist during this test module's import.
"""

# Keep protocol tests runnable with Python's standard-library unittest alone.
# ruff: noqa: PT009, PT027
import asyncio
import importlib.util
import sys
import unittest
from contextlib import suppress
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch


class DPType(Enum):
    BOOLEAN = "Boolean"
    ENUM = "Enum"
    INTEGER = "Integer"
    STRING = "String"


class DataPointType(Enum):
    DT_RAW = 0
    DT_BOOL = 1
    DT_VALUE = 2
    DT_STRING = 3
    DT_ENUM = 4
    DT_BITMAP = 5


class Code(Enum):
    FUN_SENDER_DEVICE_INFO = 0
    FUN_SENDER_PAIR = 1
    FUN_SENDER_DEVICE_STATUS = 3
    FUN_SENDER_DPS_V4 = 39
    FUN_RECEIVE_DP_V4 = 0x8006


class DeviceStub:
    def __init__(self):
        self._expected_disconnect = False
        self._connect_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._client = None
        self._is_paired = False
        self._ble_device = SimpleNamespace(address="TEST-ADDRESS")
        self._input_expected_responses = {}
        self._background_tasks = set()
        self._notification_generation = 0
        self.function = {}
        self.status_range = {}

    async def initialize(self):
        pass

    def _create_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def append_functions(self, functions, status_range):
        self.function.update({item["code"]: item for item in functions})
        self.status_range.update({item["code"]: item for item in status_range})

    @property
    def address(self):
        return self._ble_device.address

    def _disconnected(self, client):
        pass

    def _clean_input(self):
        pass

    def _handle_command_or_response(self, _sequence, response_to, _code, _data):
        # Match the stock unhandled-opcode behavior under review: it defaults
        # the result to success. The product adapter must intercept write ACKs.
        future = self._input_expected_responses.pop(response_to, None)
        if future is not None and not future.done():
            future.set_result(0)


def _load_adapter():
    package_name = "_lamomo_adapter_testpkg"
    package = ModuleType(package_name)
    package.__path__ = []
    const = ModuleType(package_name + ".const")
    const.DPType = DPType
    transport = ModuleType(package_name + ".tuya_ble")
    transport.__path__ = []
    transport.TuyaBLEDevice = DeviceStub
    transport.TuyaBLEDataPointType = DataPointType
    transport_const = ModuleType(package_name + ".tuya_ble.const")
    transport_const.TuyaBLECode = Code
    bleak = ModuleType("bleak_retry_connector")
    bleak.BleakClientWithServiceCache = object
    bleak.establish_connection = AsyncMock()
    stubs = {
        package_name: package,
        package_name + ".const": const,
        package_name + ".tuya_ble": transport,
        package_name + ".tuya_ble.const": transport_const,
        "bleak_retry_connector": bleak,
    }
    spec = importlib.util.spec_from_file_location(
        package_name + ".lamomo_fd50",
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "tuya_ble"
        / "lamomo_fd50.py",
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


adapter = _load_adapter()


def frame(dp_id, dtype, value, *, counter=1, kind=0):
    return (
        bytes(4)
        + bytes([counter, 128, kind, dp_id, dtype])
        + len(value).to_bytes(2, "big")
        + value
    )


class AdapterInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_product_rejected_without_clearing_schema(self):
        for product_id in ("", "synthetic-other-product"):
            with self.subTest(product_id=product_id):
                device = adapter.LamomoFD50Device()
                device.product_id = product_id
                device.function["legacy_feature"] = {"dp_id": 20}
                base_initialize = AsyncMock()
                with (
                    patch.object(DeviceStub, "initialize", base_initialize),
                    self.assertRaises(ValueError),
                ):
                    await device.initialize()
                base_initialize.assert_awaited_once()
                self.assertIn("legacy_feature", device.function)

    async def test_target_schema_replaces_unverified_cloud_capabilities(self):
        device = adapter.LamomoFD50Device()
        device.product_id = adapter.LAMOMO_PRODUCT_ID
        device.function.update({"temp_value": {}, "bright_value": {}})
        device.status_range.update({"temp_value": {}, "unverified_effect": {}})
        base_initialize = AsyncMock()
        with patch.object(DeviceStub, "initialize", base_initialize):
            await device.initialize()
        base_initialize.assert_awaited_once()
        verified_codes = {"switch_led", "work_mode", "colour_data"}
        self.assertEqual(set(device.function), verified_codes)
        self.assertEqual(set(device.status_range), verified_codes)
        self.assertEqual(
            {item["dp_id"] for item in device.function.values()}, {1, 2, 5}
        )
        self.assertIsInstance(device._write_lock, asyncio.Lock)
        self.assertEqual(device._pending_write_counters, {})
        self.assertEqual(device._reported, {})
        self.assertEqual(device._report_versions, {})
        self.assertIsInstance(device._report_event, asyncio.Event)


class EncodeWriteTests(unittest.TestCase):
    def test_switch_matches_verified_command(self):
        self.assertEqual(
            adapter.encode_write(1, 1, 1, b"\x00"),
            bytes.fromhex("00000000010101000100"),
        )
        self.assertEqual(
            adapter.encode_write(2, 1, 1, b"\x01"),
            bytes.fromhex("00000000020101000101"),
        )

    def test_brightness_bounds(self):
        for value in (10, 1000):
            self.assertEqual(
                adapter.encode_write(1, 3, 2, value.to_bytes(4, "big"))[-4:],
                value.to_bytes(4, "big"),
            )
        for value in (0, 9, 1001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                adapter.encode_write(1, 3, 2, value.to_bytes(4, "big"))

    def test_hsv_valid_bounds(self):
        for value in (b"000000000000", b"016803e803e8", b"016803E803E8"):
            self.assertEqual(adapter.encode_write(1, 5, 3, value)[-12:], value)

    def test_hsv_rejects_non_hex_tokens(self):
        for value in (
            b"-00100000000",
            b"0_0100000000",
            b" 00100000000",
            b"+00100000000",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                adapter.encode_write(1, 5, 3, value)

    def test_unsupported_datapoints_and_types(self):
        for dp_id, dtype, value in (
            (7, 2, bytes(4)),
            (1, 3, b"true"),
            (2, 4, b"\x02"),
            (5, 3, b"016903e803e8"),
        ):
            with self.subTest(dp_id=dp_id, dtype=dtype), self.assertRaises(ValueError):
                adapter.encode_write(1, dp_id, dtype, value)


class DecodeStatusTests(unittest.TestCase):
    def test_verified_status_types(self):
        cases = (
            (1, 1, b"\x00", False),
            (1, 1, b"\x01", True),
            (2, 4, b"\x01", 1),
            (3, 2, (500).to_bytes(4, "big"), 500),
            (5, 3, b"007803e801f4", "007803e801f4"),
        )
        for dp_id, dtype, raw, expected in cases:
            with self.subTest(dp_id=dp_id):
                self.assertEqual(
                    adapter.decode_status(frame(dp_id, dtype, raw)),
                    (dp_id, DataPointType(dtype), expected),
                )

    def test_truncated_or_excess_frames(self):
        valid = frame(1, 1, b"\x00")
        for raw in (b"", valid[:10], valid[:-1], valid + b"\x00", frame(1, 7, b"\x00")):
            with self.subTest(raw=raw):
                self.assertIsNone(adapter.decode_status(raw))

    def test_known_datapoint_type_mismatch(self):
        for dp_id, dtype, raw in (
            (1, 3, b"true"),
            (2, 1, b"\x01"),
            (3, 4, b"\x01"),
            (5, 2, bytes(4)),
        ):
            with self.subTest(dp_id=dp_id, dtype=dtype):
                self.assertIsNone(adapter.decode_status(frame(dp_id, dtype, raw)))

    def test_typed_lengths_are_strict(self):
        for dp_id, dtype, raw in (
            (1, 1, b""),
            (2, 4, b""),
            (2, 4, b"\x00\x01"),
            (3, 2, b"\x01"),
            (3, 2, bytes(5)),
        ):
            with self.subTest(dp_id=dp_id, dtype=dtype, raw=raw):
                self.assertIsNone(adapter.decode_status(frame(dp_id, dtype, raw)))

    def test_malformed_utf8_ignored(self):
        self.assertIsNone(adapter.decode_status(frame(5, 3, bytes([255]) * 12)))


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_connect_does_not_run_after_shutdown(self):
        device = adapter.LamomoFD50Device()
        await device._connect_lock.acquire()
        establish = AsyncMock(side_effect=AssertionError("Connected after shutdown"))
        with patch.object(adapter, "establish_connection", establish):
            pending = asyncio.create_task(device._ensure_connected())
            await asyncio.sleep(0)
            device._expected_disconnect = True
            device._connect_lock.release()
            await pending
            establish.assert_not_awaited()

    async def test_queued_notification_ack_tolerates_disconnect(self):
        device = adapter.LamomoFD50Device()
        device._client = SimpleNamespace(is_connected=True)
        await device._operation_lock.acquire()
        pending = asyncio.create_task(
            device._send_response(Code.FUN_RECEIVE_DP_V4, b"", 100)
        )
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        device._client = None
        device._operation_lock.release()
        await asyncio.wait_for(pending, 0.2)

    async def test_notification_ack_skipped_during_expected_unload(self):
        device = adapter.LamomoFD50Device()
        device._client = SimpleNamespace(is_connected=True)
        device._expected_disconnect = True
        sender = AsyncMock()
        with patch.object(device, "_send_packet_while_connected", sender):
            await device._send_response(Code.FUN_RECEIVE_DP_V4, b"", 100)
            sender.assert_not_awaited()


class WriteAckTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self, counter=42):
        device = adapter.LamomoFD50Device()
        future = asyncio.get_running_loop().create_future()
        device._pending_write_counters = {9: counter}
        device._input_expected_responses = {9: future}
        return device, future

    async def test_verified_ack_success(self):
        device, future = self.prepare(counter=1)
        device._handle_command_or_response(
            100, 9, Code.FUN_SENDER_DPS_V4, bytes.fromhex("000000000100")
        )
        self.assertTrue(future.done())
        self.assertEqual(future.result(), 0)

    async def test_invalid_ack_is_not_accepted(self):
        cases = (
            bytes.fromhex("000000002b00"),  # Wrong command counter.
            bytes.fromhex("000000002a01"),  # Device error result.
            bytes.fromhex("000000002a"),  # Truncated result.
            bytes.fromhex("000000002a0000"),  # Excess data.
            bytes.fromhex("010000002a00"),  # Unexpected header.
            b"",
        )
        for raw in cases:
            with self.subTest(raw=raw):
                device, future = self.prepare()
                device._handle_command_or_response(100, 9, Code.FUN_SENDER_DPS_V4, raw)
                self.assertTrue(
                    future.done(), "Malformed write ACK should fail promptly"
                )
                self.assertIsInstance(future.exception(), ConnectionError)

    async def test_unsolicited_ack_cannot_satisfy_other_request(self):
        device, future = self.prepare()
        device._handle_command_or_response(
            100, 8, Code.FUN_SENDER_DPS_V4, bytes.fromhex("000000002a00")
        )
        self.assertFalse(future.done())
        future.cancel()


class QueuedControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_queued_color_keeps_desired_value_after_other_task_readback(self):
        device = adapter.LamomoFD50Device()
        desired = b"007803e801f4"
        previous_report = b"000003e801f4"
        datapoint = SimpleNamespace(type=DataPointType.DT_STRING, value=desired)
        datapoint._get_value = lambda: datapoint.value
        device._datapoints = {5: datapoint}
        device._dp_counter = 0
        device._write_lock = asyncio.Lock()
        device._report_versions = {}
        device._reported = {}
        ensure_connected = AsyncMock()
        sender = AsyncMock(return_value=True)
        wait_reported = AsyncMock()
        await device._write_lock.acquire()
        with (
            patch.object(device, "_ensure_connected", ensure_connected),
            patch.object(device, "_send_packet_while_connected", sender),
            patch.object(device, "_wait_reported", wait_reported),
        ):
            pending = asyncio.create_task(device._send_datapoints([5]))
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            # Simulate the preceding power task's state readback overwriting
            # the shared datapoint while the requested color is still queued.
            datapoint.value = previous_report
            device._write_lock.release()
            await asyncio.wait_for(pending, 0.2)
            self.assertEqual(
                sender.await_args_list[0].args,
                (
                    Code.FUN_SENDER_DPS_V4,
                    adapter.encode_write(1, 5, 3, desired),
                    0,
                    True,
                ),
            )
            wait_reported.assert_awaited_once_with({5: desired}, {})


class FreshReadbackTests(unittest.IsolatedAsyncioTestCase):
    def prepare(self):
        device = adapter.LamomoFD50Device()
        device._client = SimpleNamespace(is_connected=True)
        device._is_paired = True
        device._reported = {1: b"\x00"}
        device._report_versions = {1: 3}
        device._report_event = asyncio.Event()
        return device

    async def cancel(self, task):
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def test_stale_matching_report_cannot_succeed(self):
        device = self.prepare()
        device._report_event.set()  # A stale event must also be disregarded.
        task = asyncio.create_task(device._wait_reported({1: b"\x00"}, {1: 3}))
        try:
            await asyncio.sleep(0.02)
            self.assertFalse(
                task.done(), "Old matching state was accepted as fresh readback"
            )
        finally:
            await self.cancel(task)

    async def test_only_fresh_exact_report_succeeds(self):
        device = self.prepare()
        task = asyncio.create_task(device._wait_reported({1: b"\x00"}, {1: 3}))
        try:
            await asyncio.sleep(0)
            device._reported[1] = b"\x01"
            device._report_versions[1] = 4
            device._report_event.set()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done(), "Fresh but nonmatching value was accepted")
            device._reported[1] = b"\x00"
            device._report_versions[1] = 5
            device._report_event.set()
            await asyncio.wait_for(task, 0.2)
        finally:
            await self.cancel(task)

    async def test_any_fresh_value_allowed_when_expected_none(self):
        device = self.prepare()
        task = asyncio.create_task(device._wait_reported({1: None}, {1: 3}))
        try:
            await asyncio.sleep(0)
            device._reported[1] = b"\x01"
            device._report_versions[1] = 4
            device._report_event.set()
            await asyncio.wait_for(task, 0.2)
        finally:
            await self.cancel(task)

    async def test_all_requested_datapoints_must_be_fresh(self):
        device = self.prepare()
        device._reported[5] = b"007803e801f4"
        device._report_versions[5] = 2
        task = asyncio.create_task(
            device._wait_reported({1: b"\x00", 5: b"007803e801f4"}, {1: 3, 5: 2})
        )
        try:
            await asyncio.sleep(0)
            device._report_versions[1] = 4
            device._report_event.set()
            await asyncio.sleep(0.02)
            self.assertFalse(
                task.done(), "One fresh DP was accepted while another was stale"
            )
            device._report_versions[5] = 3
            device._report_event.set()
            await asyncio.wait_for(task, 0.2)
        finally:
            await self.cancel(task)

    async def test_binary_values_are_not_case_folded(self):
        device = self.prepare()
        raw_65 = (65).to_bytes(4, "big")
        raw_97 = (97).to_bytes(4, "big")
        device._report_versions[3] = 1
        device._reported[3] = raw_65
        task = asyncio.create_task(device._wait_reported({3: raw_65}, {3: 1}))
        try:
            await asyncio.sleep(0)
            device._reported[3] = raw_97
            device._report_versions[3] = 2
            device._report_event.set()
            await asyncio.sleep(0.02)
            self.assertFalse(
                task.done(), "Different binary values were equated by lowercasing"
            )
            device._reported[3] = raw_65
            device._report_versions[3] = 3
            device._report_event.set()
            await asyncio.wait_for(task, 0.2)
        finally:
            await self.cancel(task)

    async def test_hsv_hex_case_is_equivalent(self):
        device = self.prepare()
        device._reported[5] = b"016803e801f4"
        device._report_versions[5] = 2
        task = asyncio.create_task(device._wait_reported({5: b"016803E801F4"}, {5: 2}))
        try:
            await asyncio.sleep(0)
            device._report_versions[5] = 3
            device._report_event.set()
            await asyncio.wait_for(task, 0.2)
        finally:
            await self.cancel(task)


if __name__ == "__main__":
    unittest.main()
