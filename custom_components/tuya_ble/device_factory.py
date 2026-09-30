"""Select product-specific transports without changing legacy Tuya BLE devices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .lamomo_fd50 import LAMOMO_PRODUCT_ID, LamomoFD50Device
from .tuya_ble import TuyaBLEDevice

if TYPE_CHECKING:
    from bleak.backends.device import BLEDevice

    from .tuya_ble import AbstaractTuyaBLEDeviceManager


async def async_create_device(
    manager: AbstaractTuyaBLEDeviceManager, ble_device: BLEDevice
) -> TuyaBLEDevice:
    """Resolve credentials before selecting the narrowly scoped FD50 transport.

    Initialization only resolves credentials/schema, not a Bluetooth connection.
    The manager caches cloud credentials (or uses manual entry credentials), so
    recreating the known product does not repeat cloud provisioning. Unknown
    products keep the existing device implementation and initialization behavior.
    """
    device = TuyaBLEDevice(manager, ble_device)
    await device.initialize()
    if device.product_id == LAMOMO_PRODUCT_ID:
        device = LamomoFD50Device(manager, ble_device)
        await device.initialize()
    return device
