"""Product-scoped FD50 transport for Lamomo RGB strips (0qgrjxum).

Uses existing credentials and the legacy Tuya encryption/fragmentation layer.
Power, RGB, brightness and reconnect were tested with Tuya BLE 4.4 / firmware 1.1
on Linux BlueZ with a CSR8510 USB adapter. Other FD50 products are not enabled.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from struct import pack

from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import DPType
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice
from .tuya_ble.const import TuyaBLECode

_LOGGER = logging.getLogger(__name__)
LAMOMO_PRODUCT_ID = "0qgrjxum"
RECONNECT_DELAY = 15
WRITE = "00000001-0000-1001-8001-00805f9b07d0"
NOTIFY = "00000002-0000-1001-8001-00805f9b07d0"


def _range(maximum):
    return {"min": 0, "max": maximum, "scale": 0, "step": 1}


SCHEMA = [
    {"code": "switch_led", "dp_id": 1, "type": DPType.BOOLEAN, "values": {}},
    {
        "code": "work_mode",
        "dp_id": 2,
        "type": DPType.ENUM,
        "values": {"range": ["colour", "scene"]},
    },
    {
        "code": "colour_data",
        "dp_id": 5,
        "type": DPType.STRING,
        "values": {"h": _range(360), "s": _range(1000), "v": _range(1000)},
    },
]


def encode_write(counter, dp_id, dtype, value):
    """V4 typed command, bounded to reviewed lighting controls."""
    expected = {1: 1, 2: 4, 3: 2, 5: 3}
    if expected.get(dp_id) != dtype:
        raise ValueError("Unsupported Lamomo control")
    if dp_id == 1 and value not in (b"\x00", b"\x01"):
        raise ValueError("Invalid power")
    if dp_id == 2 and value not in (b"\x00", b"\x01"):
        raise ValueError("Invalid mode")
    if dp_id == 3 and (
        len(value) != 4 or not 10 <= int.from_bytes(value, "big") <= 1000
    ):
        raise ValueError("Invalid brightness")
    if dp_id == 5:
        if len(value) != 12 or any(c not in b"0123456789abcdefABCDEF" for c in value):
            raise ValueError("Invalid color length")
        h, s, v = (int(value[i : i + 4], 16) for i in (0, 4, 8))
        if not (0 <= h <= 360 and 0 <= s <= 1000 and 0 <= v <= 1000):
            raise ValueError("Invalid color range")
    return (
        bytes(4) + bytes([counter & 255, dp_id, dtype]) + pack(">H", len(value)) + value
    )


def decode_status(data):
    """Observed V4 event: header, counter, 80, kind, DP, type, length, value."""
    if len(data) < 11 or data[:4] != bytes(4) or data[5] != 128:
        return None
    length = int.from_bytes(data[9:11], "big")
    if len(data) != 11 + length or data[6] not in (0, 1, 2):
        return None
    raw = data[11:]
    try:
        encode_write(data[4], data[7], data[8], raw)
    except ValueError, TypeError:
        return None
    dtype = TuyaBLEDataPointType(data[8])
    if dtype == TuyaBLEDataPointType.DT_BOOL:
        if raw not in (b"\x00", b"\x01"):
            return None
        value = bool(raw[0])
    elif dtype in (TuyaBLEDataPointType.DT_VALUE, TuyaBLEDataPointType.DT_ENUM):
        value = int.from_bytes(
            raw, "big", signed=dtype == TuyaBLEDataPointType.DT_VALUE
        )
    elif dtype == TuyaBLEDataPointType.DT_STRING:
        value = raw.decode()
    else:
        value = raw
    return data[7], dtype, value


class LamomoFD50Device(TuyaBLEDevice):
    async def initialize(self):
        await super().initialize()
        if self.product_id != LAMOMO_PRODUCT_ID:
            raise ValueError("Lamomo adapter is product-scoped")
        # Cloud schemas may list unverified controls. Expose only the DPs
        # implemented by this transport, including brightness via HSV value.
        self.function.clear()
        self.status_range.clear()
        self.append_functions(SCHEMA, SCHEMA)
        self._dp_counter = 0
        self._write_lock = asyncio.Lock()
        self._pending_write_counters = {}
        self._reported = {}
        self._report_versions = {}
        self._report_event = asyncio.Event()
        self._reconnect_task = None
        self._local_update_callbacks = []
        self._notification_generation = 0
        self._session_reports = set()
        self._retiring_clients = set()

    async def start(self):
        """Own initial connection attempts as well as later reconnects."""
        self._schedule_reconnect()

    def _schedule_reconnect(self):
        if not self._expected_disconnect and (
            not self._reconnect_task or self._reconnect_task.done()
        ):
            self._reconnect_task = self._create_task(self._reconnect())

    def register_local_update_callback(self, callback):
        """Register cached-state notifications, not connectivity evidence."""
        self._local_update_callbacks.append(callback)

        def unregister():
            self._local_update_callbacks.remove(callback)

        return unregister

    def _fire_local_update_callbacks(self, datapoints):
        if not self._expected_disconnect:
            for callback in self._local_update_callbacks:
                callback(datapoints)

    def _check_live_connection(self):
        if (
            self._expected_disconnect
            or not self._client
            or not self._client.is_connected
            or not self._is_paired
        ):
            raise ConnectionError("Lamomo disconnected before state confirmation")

    async def _run_owned_operation(self, operation):
        # HA owns entity-control callers. Own the transport work separately so
        # unload can cancel it without cancelling an unrelated caller task.
        if self._expected_disconnect:
            operation.close()
            raise ConnectionError("Lamomo BLE is stopped")
        task = self._create_task(operation)
        try:
            return await task
        finally:
            self._background_tasks.discard(task)

    async def _discard_failed_connection(self, client):
        if client is None:
            return False
        async with self._connect_lock:
            # A delayed failure from an old ACK must not close a new session.
            if self._client is not client:
                return False
            was_paired = self._is_paired
            self._retiring_clients.add(client)
            self._client = None
            self._is_paired = False
            self._reset_transport()
            if was_paired and not self._expected_disconnect:
                self._fire_disconnected_callbacks()
            with suppress(Exception):
                await self._disconnect_client(client)
            return True

    async def _disconnect_client(self, client):
        # Cancellation or a timeout must not lose a still-connected client.
        # Stop/retry retain ownership until another bounded close succeeds.
        self._retiring_clients.add(client)
        try:
            if client.is_connected:
                await asyncio.wait_for(client.disconnect(), 5)
            if client.is_connected:
                raise ConnectionError("Lamomo previous connection did not close")
        finally:
            if not client.is_connected:
                self._retiring_clients.discard(client)

    def _decode_advertisement_data(self):
        # FD50's advertisement differs from the stock A201 manufacturer layout.
        # Protocol and binding status are authenticated by DEVICE_INFO instead.
        self._protocol_version = 2

    def _build_packets(self, sequence, code, data, response_to=0):
        previous = self._protocol_version
        if code == TuyaBLECode.FUN_SENDER_DEVICE_INFO:
            self._protocol_version = 2
        try:
            return super()._build_packets(sequence, code, data, response_to)
        finally:
            self._protocol_version = previous

    async def _ensure_connected(self):
        if self._expected_disconnect:
            return
        async with self._connect_lock:
            if self._expected_disconnect:
                return
            if self._client and self._client.is_connected and self._is_paired:
                return
            client = None
            try:
                for retiring in list(self._retiring_clients):
                    await self._disconnect_client(retiring)
                client = await asyncio.wait_for(
                    establish_connection(
                        BleakClientWithServiceCache,
                        self._ble_device,
                        self.address,
                        self._disconnected,
                        max_attempts=2,
                    ),
                    40,
                )
                self._client = client
                self._clean_input()
                self._is_paired = False
                self._session_key = None
                self._current_seq_num = 1
                self._notification_generation += 1
                generation = self._notification_generation

                def notify(sender, data):
                    if (
                        not self._expected_disconnect
                        and self._client is client
                        and self._notification_generation == generation
                    ):
                        self._notification_handler(sender, data)

                await asyncio.wait_for(
                    client.start_notify(
                        NOTIFY,
                        notify,
                        bluez={"use_start_notify": True},
                    ),
                    8,
                )
                acquire = getattr(
                    getattr(client, "_backend", None), "_acquire_mtu", None
                )
                if acquire:
                    await asyncio.wait_for(acquire(), 8)
                await self._send_packet_while_connected(
                    TuyaBLECode.FUN_SENDER_DEVICE_INFO, b"\x00\xf3", 0, True
                )
                await self._send_packet_while_connected(
                    TuyaBLECode.FUN_SENDER_PAIR, self._build_pairing_request(), 0, True
                )
                self._check_authenticated_protocol()
                self._session_reports.clear()
            except BaseException:
                if client is not None:
                    self._retiring_clients.add(client)
                self._is_paired = False
                self._client = None
                self._reset_transport()
                if client is not None:
                    with suppress(Exception):
                        await self._disconnect_client(client)
                raise

    def _check_authenticated_protocol(self):
        if not self._is_paired or self._protocol_version != 4:
            raise ValueError("Unexpected Lamomo authentication/protocol")

    async def _send_packet_while_connected(
        self, code, data, response_to, wait_for_response, *, expected_session=None
    ):
        # Serialize the complete send/wait cycle, not just individual ATT writes.
        async with self._operation_lock:
            if (
                self._expected_disconnect
                or not self._client
                or not self._client.is_connected
            ):
                raise ConnectionError("Lamomo BLE is disconnected")
            if expected_session is not None:
                client, generation = expected_session
                if (
                    self._client is not client
                    or self._notification_generation != generation
                ):
                    raise ConnectionError("Lamomo notification session expired")
            sequence = await self._get_seq_num()
            future = (
                asyncio.get_running_loop().create_future()
                if wait_for_response
                else None
            )
            if future:
                self._input_expected_responses[sequence] = future
                if code == TuyaBLECode.FUN_SENDER_DPS_V4:
                    self._pending_write_counters[sequence] = data[4]
            try:
                for packet in self._build_packets(sequence, code, data, response_to):
                    await asyncio.wait_for(
                        self._client.write_gatt_char(WRITE, packet, response=False), 5
                    )
                if future:
                    await asyncio.wait_for(future, 8)
                return True
            finally:
                self._input_expected_responses.pop(sequence, None)
                self._pending_write_counters.pop(sequence, None)
                if future and not future.done():
                    future.cancel()

    def _handle_command_or_response(self, sequence, response_to, code, data):
        if code == TuyaBLECode.FUN_SENDER_DPS_V4:
            future = self._input_expected_responses.get(response_to)
            if future and not future.done():
                counter = self._pending_write_counters.get(response_to)
                if (
                    len(data) == 6
                    and data[:4] == bytes(4)
                    and data[4] == counter
                    and data[5] == 0
                ):
                    future.set_result(0)
                else:
                    future.set_exception(
                        ConnectionError("Lamomo write was not acknowledged")
                    )
            return
        if code == TuyaBLECode.FUN_RECEIVE_DP_V4:
            parsed = decode_status(data)
            if parsed:
                dp_id, dtype, value = parsed
                self._datapoints._update_from_device(
                    dp_id, time.time(), 0, dtype, value
                )
                self._reported[dp_id] = data[11:].lower() if dp_id == 5 else data[11:]
                self._report_versions[dp_id] = self._report_versions.get(dp_id, 0) + 1
                self._report_event.set()
                self._session_reports.add(dp_id)
                # Pairing and partial state cannot make RGB controls usable.
                # The light entity needs all three current-session datapoints.
                if (
                    {1, 2, 5} <= self._session_reports
                    and self._is_paired
                    and self._client
                    and self._client.is_connected
                ):
                    self._fire_callbacks([self._datapoints[dp_id]])
            self._create_task(self._send_response(code, b"", sequence))
            return
        super()._handle_command_or_response(sequence, response_to, code, data)

    def _send_response(self, code, data, response_to):
        # Capture ownership at creation, not when a queued ACK starts running.
        return self._send_session_response(
            self._client, self._notification_generation, code, data, response_to
        )

    async def _send_session_response(self, client, generation, code, data, response_to):
        if (
            self._expected_disconnect
            or client is None
            or self._client is not client
            or self._notification_generation != generation
            or not client.is_connected
        ):
            return
        try:
            await self._send_packet_while_connected(
                code,
                data,
                response_to,
                False,
                expected_session=(client, generation),
            )
        except Exception:
            # Bleak errors are not ConnectionError subclasses. Detached ACK
            # tasks must handle transport failures and recover the failed link.
            _LOGGER.debug("Lamomo notification ACK failed", exc_info=True)
            if await self._discard_failed_connection(client):
                self._schedule_reconnect()

    async def _wait_reported(self, expected, baseline):
        deadline = asyncio.get_running_loop().time() + 8
        while True:
            self._report_event.clear()
            # Reports can arrive immediately before a disconnect callback.
            # Cached matching reports alone cannot finish a reconnect.
            self._check_live_connection()
            if all(
                self._report_versions.get(dp_id, 0) > baseline.get(dp_id, 0)
                and (
                    raw is None
                    or self._reported.get(dp_id) == (raw.lower() if dp_id == 5 else raw)
                )
                for dp_id, raw in expected.items()
            ):
                return
            await asyncio.wait_for(
                self._report_event.wait(),
                max(0, deadline - asyncio.get_running_loop().time()),
            )

    async def update(self):
        await self._run_owned_operation(self._update())

    async def _update(self):
        async with self._write_lock:
            client = None
            try:
                await self._ensure_connected()
                client = self._client
                baseline = self._report_versions.copy()
                await self._send_packet_while_connected(
                    TuyaBLECode.FUN_SENDER_DEVICE_STATUS, b"", 0, True
                )
                await self._wait_reported({1: None, 2: None, 5: None}, baseline)
                self._fire_connected_callbacks()
            except Exception:
                await self._discard_failed_connection(client)
                # Setup is asynchronous; a transient first failure still needs
                # a tracked retry owner. Existing retries are never duplicated.
                self._schedule_reconnect()
                raise

    async def _send_datapoints(self, datapoint_ids):
        # Stock entities schedule power, mode and color as separate tasks.
        # Capture desired values before yielding; an earlier task's readback
        # otherwise overwrites a later task's queued local value.
        desired = {
            dp_id: (
                self._datapoints[dp_id].type.value,
                self._datapoints[dp_id]._get_value(),
            )
            for dp_id in datapoint_ids
        }
        await self._run_owned_operation(self._send_desired_datapoints(desired))

    async def _send_desired_datapoints(self, desired):
        async with self._write_lock:
            expected = {dp_id: raw for dp_id, (_, raw) in desired.items()}
            client = None
            try:
                await self._ensure_connected()
                client = self._client
                baseline = self._report_versions.copy()
                for dp_id, raw in expected.items():
                    self._dp_counter = (self._dp_counter + 1) & 255
                    payload = encode_write(
                        self._dp_counter, dp_id, desired[dp_id][0], raw
                    )
                    await self._send_packet_while_connected(
                        TuyaBLECode.FUN_SENDER_DPS_V4, payload, 0, True
                    )
                await self._send_packet_while_connected(
                    TuyaBLECode.FUN_SENDER_DEVICE_STATUS, b"", 0, True
                )
                await self._wait_reported(expected, baseline)
            except BaseException as error:
                restored = []
                for dp_id in expected:
                    raw = self._reported.get(dp_id)
                    if raw is not None:
                        dp = self._datapoints[dp_id]
                        report = (
                            bytes(4)
                            + bytes([0, 128, 0, dp_id, dp.type.value])
                            + pack(">H", len(raw))
                            + raw
                        )
                        parsed = decode_status(report)
                        if parsed:
                            dp._update_from_device(time.time(), 0, parsed[1], parsed[2])
                            restored.append(dp)
                if restored:
                    self._fire_local_update_callbacks(restored)
                if isinstance(error, Exception):
                    await self._discard_failed_connection(client)
                    self._schedule_reconnect()
                raise

    def _reset_transport(self):
        self._notification_generation += 1
        self._session_reports.clear()
        self._session_key = None
        self._clean_input()
        for future in list(self._input_expected_responses.values()):
            if future and not future.done():
                future.set_exception(ConnectionError("Lamomo BLE disconnected"))
        self._input_expected_responses.clear()
        self._pending_write_counters.clear()
        self._current_seq_num = 1
        self._report_event.set()

    def _disconnected(self, client):
        if self._client is not client:
            return
        was_paired = self._is_paired
        self._client = None
        self._is_paired = False
        self._reset_transport()
        self._fire_disconnected_callbacks()
        if was_paired:
            self._schedule_reconnect()

    async def _execute_disconnect(self):
        self._expected_disconnect = True
        tasks = [
            task
            for task in self._background_tasks
            if task is not asyncio.current_task()
        ]
        # Cancel an in-flight retry before waiting for its connection lock.
        # Otherwise unload could wait through the entire connect timeout.
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._background_tasks.difference_update(
                task for task in tasks if task.done()
            )
        async with self._connect_lock:
            if self._client is not None:
                self._retiring_clients.add(self._client)
            self._client = None
            self._is_paired = False
            try:
                for client in list(self._retiring_clients):
                    await self._disconnect_client(client)
            finally:
                self._reset_transport()

    async def _reconnect(self):
        while not self._expected_disconnect:
            try:
                await self.update()
                self._check_live_connection()
            except Exception:
                if self._expected_disconnect:
                    return
                _LOGGER.warning("Lamomo BLE reconnect failed; retrying")
                await asyncio.sleep(RECONNECT_DELAY)
            else:
                return
