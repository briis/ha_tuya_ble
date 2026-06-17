"""The Tuya BLE integration - cover (curtain) support."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityDescription,
    CoverEntityFeature,
)

from .const import DOMAIN, DPCode, DPType
from .devices import TuyaBLEData, TuyaBLEEntity, TuyaBLEProductInfo

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    from .base import IntegerTypeData
    from .tuya_ble import TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)


@dataclass
class TuyaBLECoverEntityDescription(CoverEntityDescription):
    """Describe a Tuya BLE cover entity.

    This intentionally mirrors the dpcode-driven cover description that
    Home Assistant's official (cloud) Tuya integration used before it was
    rewritten to depend on the external tuya_device_handlers package, since
    the local-key/cloud-schema data this BLE integration already exposes via
    device.function / device.status_range / device.status is structurally
    the same.
    """

    current_state: DPCode | None = None
    current_state_inverse: bool = False
    current_position: DPCode | tuple[DPCode, ...] | None = None
    set_position: DPCode | None = None
    open_instruction_value: str = "open"
    close_instruction_value: str = "close"
    stop_instruction_value: str = "stop"


# Mappings are by DPCode, not raw dp_id, so any curtain/blind motor that
# exposes the standard Tuya "cl" / "clkg" / "jdcljqr" category functions
# will work without needing a product_id specific entry.
COVERS: dict[str, tuple[TuyaBLECoverEntityDescription, ...]] = {
    # Curtain
    # https://developer.tuya.com/en/docs/iot/categorycl?id=Kaiuz1hnpo7df
    "cl": (
        TuyaBLECoverEntityDescription(
            key=DPCode.CONTROL,
            translation_key="curtain",
            current_state=DPCode.SITUATION_SET,
            current_position=(DPCode.PERCENT_CONTROL, DPCode.PERCENT_STATE),
            set_position=DPCode.PERCENT_CONTROL,
            device_class=CoverDeviceClass.CURTAIN,
        ),
        TuyaBLECoverEntityDescription(
            key=DPCode.CONTROL_2,
            translation_key="curtain_2",
            current_position=DPCode.PERCENT_STATE_2,
            set_position=DPCode.PERCENT_CONTROL_2,
            device_class=CoverDeviceClass.CURTAIN,
        ),
        TuyaBLECoverEntityDescription(
            key=DPCode.CONTROL_3,
            translation_key="curtain_3",
            current_position=DPCode.PERCENT_STATE_3,
            set_position=DPCode.PERCENT_CONTROL_3,
            device_class=CoverDeviceClass.CURTAIN,
        ),
        # switch_1 is an undocumented code that behaves identically to
        # control on some blind drivers (kept for parity with upstream).
        TuyaBLECoverEntityDescription(
            key=DPCode.SWITCH_1,
            translation_key="blind",
            current_position=DPCode.PERCENT_CONTROL,
            set_position=DPCode.PERCENT_CONTROL,
            device_class=CoverDeviceClass.BLIND,
        ),
    ),
    # Curtain Switch (motor controlled via a separate wired switch module)
    # https://developer.tuya.com/en/docs/iot/category-clkg?id=Kaiuz0gitil39
    "clkg": (
        TuyaBLECoverEntityDescription(
            key=DPCode.CONTROL,
            translation_key="curtain",
            current_position=DPCode.PERCENT_CONTROL,
            set_position=DPCode.PERCENT_CONTROL,
            device_class=CoverDeviceClass.CURTAIN,
        ),
        TuyaBLECoverEntityDescription(
            key=DPCode.CONTROL_2,
            translation_key="curtain_2",
            current_position=DPCode.PERCENT_CONTROL_2,
            set_position=DPCode.PERCENT_CONTROL_2,
            device_class=CoverDeviceClass.CURTAIN,
        ),
    ),
    # Curtain Robot
    "jdcljqr": (
        TuyaBLECoverEntityDescription(
            key=DPCode.CONTROL,
            translation_key="curtain",
            current_position=DPCode.PERCENT_STATE,
            set_position=DPCode.PERCENT_CONTROL,
            device_class=CoverDeviceClass.CURTAIN,
        ),
    ),
}


def get_mapping_by_device(
    device: TuyaBLEDevice,
) -> tuple[TuyaBLECoverEntityDescription, ...]:
    """Return the cover descriptions applicable to this device's category."""
    return COVERS.get(device.category, ())


class TuyaBLECoverEntity(TuyaBLEEntity, CoverEntity):
    """Representation of a Tuya BLE curtain/cover."""

    entity_description: TuyaBLECoverEntityDescription

    _current_position: IntegerTypeData | None = None
    _set_position: IntegerTypeData | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        description: TuyaBLECoverEntityDescription,
    ) -> None:
        super().__init__(hass, coordinator, device, product, description)
        self._attr_supported_features = CoverEntityFeature(0)

        # Determine open/close/stop support from the instruction dpcode.
        dptype = self.get_dptype(description.key, prefer_function=True)
        if dptype == DPType.BOOLEAN:
            self._attr_supported_features |= (
                CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
            )
        elif enum_type := self.find_dpcode(
            description.key, dptype=DPType.ENUM, prefer_function=True
        ):
            if description.open_instruction_value in enum_type.range:
                self._attr_supported_features |= CoverEntityFeature.OPEN
            if description.close_instruction_value in enum_type.range:
                self._attr_supported_features |= CoverEntityFeature.CLOSE
            if description.stop_instruction_value in enum_type.range:
                self._attr_supported_features |= CoverEntityFeature.STOP

        # Determine the dpcode to use for setting position.
        if int_type := self.find_dpcode(
            description.set_position, dptype=DPType.INTEGER, prefer_function=True
        ):
            self._attr_supported_features |= CoverEntityFeature.SET_POSITION
            self._set_position = int_type
            # Default to the same dpcode for reading position too, unless
            # a (possibly different, read-only) one is found below.
            self._current_position = int_type

        # Determine the dpcode to use for reading the current position.
        if int_type := self.find_dpcode(
            description.current_position, dptype=DPType.INTEGER, prefer_function=True
        ):
            self._current_position = int_type

    @property
    def current_cover_position(self) -> int | None:
        """Return the current position of the cover, 0 (closed) - 100 (open)."""
        if self._current_position is None:
            return None

        if (position := self.device.status.get(self._current_position.dpcode)) is None:
            return None

        return round(
            self._current_position.remap_value_to(position, 0, 100, reverse=True)
        )

    @property
    def is_closed(self) -> bool | None:
        """Return true if the cover is closed."""
        if (
            self.entity_description.current_state is not None
            and (
                current_state := self.device.status.get(
                    self.entity_description.current_state
                )
            )
            is not None
        ):
            return self.entity_description.current_state_inverse is not (
                current_state in (True, "fully_close")
            )

        if (position := self.current_cover_position) is not None:
            return position == 0

        return None

    def open_cover(self, **_kwargs: Any) -> None:
        """Open the cover."""
        value: bool | str = True
        if self.find_dpcode(
            self.entity_description.key, dptype=DPType.ENUM, prefer_function=True
        ):
            value = self.entity_description.open_instruction_value

        commands: list[dict[str, Any]] = [
            {"code": self.entity_description.key, "value": value}
        ]

        if self._set_position is not None:
            commands.append(
                {
                    "code": self._set_position.dpcode,
                    "value": round(
                        self._set_position.remap_value_from(100, 0, 100, reverse=True)
                    ),
                }
            )

        self._send_command(commands)

    def close_cover(self, **_kwargs: Any) -> None:
        """Close the cover."""
        value: bool | str = False
        if self.find_dpcode(
            self.entity_description.key, dptype=DPType.ENUM, prefer_function=True
        ):
            value = self.entity_description.close_instruction_value

        commands: list[dict[str, Any]] = [
            {"code": self.entity_description.key, "value": value}
        ]

        if self._set_position is not None:
            commands.append(
                {
                    "code": self._set_position.dpcode,
                    "value": round(
                        self._set_position.remap_value_from(0, 0, 100, reverse=True)
                    ),
                }
            )

        self._send_command(commands)

    def set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position."""
        if self._set_position is None:
            msg = "Cannot set position, device doesn't provide a way to set it"
            raise RuntimeError(msg)

        self._send_command(
            [
                {
                    "code": self._set_position.dpcode,
                    "value": round(
                        self._set_position.remap_value_from(
                            kwargs[ATTR_POSITION], 0, 100, reverse=True
                        )
                    ),
                }
            ]
        )

    def stop_cover(self, **_kwargs: Any) -> None:
        """Stop the cover."""
        self._send_command(
            [
                {
                    "code": self.entity_description.key,
                    "value": self.entity_description.stop_instruction_value,
                }
            ]
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tuya BLE covers."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    descriptions = get_mapping_by_device(data.device)
    entities = [
        TuyaBLECoverEntity(
            hass, data.coordinator, data.device, data.product, description
        )
        for description in descriptions
        if description.key in data.device.function
        or description.key in data.device.status_range
    ]
    async_add_entities(entities)
