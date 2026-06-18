"""Config flow for Tuya BLE integration."""

from __future__ import annotations

import logging
import re
from functools import partial
from typing import TYPE_CHECKING, Any

import pycountry
import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_COUNTRY_CODE,
    CONF_DEVICE_ID,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import callback
from tuya_iot import AuthType

from .cloud import HASSTuyaBLEDeviceManager
from .const import (
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_APP_TYPE,
    CONF_AUTH_TYPE,
    CONF_CATEGORY,
    CONF_DEVICE_NAME,
    CONF_ENDPOINT,
    CONF_LOCAL_KEY,
    CONF_PRODUCT_ID,
    CONF_PRODUCT_MODEL,
    CONF_PRODUCT_NAME,
    CONF_UUID,
    DOMAIN,
    SMARTLIFE_APP,
    TUYA_COUNTRIES,
    TUYA_RESPONSE_CODE,
    TUYA_RESPONSE_MSG,
    TUYA_RESPONSE_SUCCESS,
    TUYA_SMART_APP,
)
from .devices import get_device_readable_name
from .tuya_ble import SERVICE_UUID

if TYPE_CHECKING:
    from homeassistant.data_entry_flow import FlowHandler, FlowResult

_LOGGER = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


async def _try_login(
    manager: HASSTuyaBLEDeviceManager,
    user_input: dict[str, Any],
    errors: dict[str, str],
    placeholders: dict[str, Any],
) -> dict[str, Any] | None:
    response: dict[Any, Any] | None
    data: dict[str, Any]

    country = next(
        (c for c in TUYA_COUNTRIES if c.name == user_input[CONF_COUNTRY_CODE]),
        None,
    )
    if country is None:
        errors["base"] = "invalid_auth"
        return None

    data = {
        CONF_ENDPOINT: country.endpoint,
        CONF_AUTH_TYPE: AuthType.CUSTOM,
        CONF_ACCESS_ID: user_input[CONF_ACCESS_ID],
        CONF_ACCESS_SECRET: user_input[CONF_ACCESS_SECRET],
        CONF_USERNAME: user_input[CONF_USERNAME],
        CONF_PASSWORD: user_input[CONF_PASSWORD],
        CONF_COUNTRY_CODE: country.country_code,
    }

    for app_type in (TUYA_SMART_APP, SMARTLIFE_APP, ""):
        data[CONF_APP_TYPE] = app_type
        if app_type == "":
            data[CONF_AUTH_TYPE] = AuthType.CUSTOM
        else:
            data[CONF_AUTH_TYPE] = AuthType.SMART_HOME

        response = await manager._login(data, True)

        if response.get(TUYA_RESPONSE_SUCCESS, False):
            return data

    errors["base"] = "login_error"
    if response:
        placeholders.update(
            {
                TUYA_RESPONSE_CODE: response.get(TUYA_RESPONSE_CODE),
                TUYA_RESPONSE_MSG: response.get(TUYA_RESPONSE_MSG),
            }
        )

    return None


def _show_login_form(
    flow: FlowHandler,
    user_input: dict[str, Any],
    errors: dict[str, str],
    placeholders: dict[str, Any],
    def_country_name: str | None = None,
) -> FlowResult:
    """Shows the Tuya IOT platform login form."""
    if user_input is not None and user_input.get(CONF_COUNTRY_CODE) is not None:
        for country in TUYA_COUNTRIES:
            if country.country_code == user_input[CONF_COUNTRY_CODE]:
                user_input[CONF_COUNTRY_CODE] = country.name
                break

    return flow.async_show_form(
        step_id="login",
        data_schema=vol.Schema(
            {
                vol.Required(
                    CONF_COUNTRY_CODE,
                    default=user_input.get(CONF_COUNTRY_CODE, def_country_name),
                ): vol.In(
                    # We don't pass a dict {code:name} because country codes can be duplicate.
                    [country.name for country in TUYA_COUNTRIES]
                ),
                vol.Required(
                    CONF_ACCESS_ID, default=user_input.get(CONF_ACCESS_ID, "")
                ): str,
                vol.Required(
                    CONF_ACCESS_SECRET,
                    default=user_input.get(CONF_ACCESS_SECRET, ""),
                ): str,
                vol.Required(
                    CONF_USERNAME, default=user_input.get(CONF_USERNAME, "")
                ): str,
                vol.Required(
                    CONF_PASSWORD, default=user_input.get(CONF_PASSWORD, "")
                ): str,
            }
        ),
        errors=errors,
        description_placeholders={
            "tuya_docs_url": "https://www.home-assistant.io/integrations/tuya/",
            **placeholders,
        },
    )


async def _async_get_default_country_name(hass) -> str | None:
    """Resolve the system country name without blocking the event loop."""
    if not hass.config.country:
        return None

    def_country = await hass.async_add_executor_job(
        partial(pycountry.countries.get, alpha_2=hass.config.country)
    )
    return def_country.name if def_country else None


class TuyaBLEOptionsFlow(OptionsFlowWithConfigEntry):
    """Handle a Tuya BLE options flow."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__(config_entry)
        self._data: dict[str, Any] = {}
        self._manager: HASSTuyaBLEDeviceManager | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if self.config_entry.options.get(CONF_ACCESS_ID):
            return await self.async_step_login(user_input)

        # Manual entry: offer inline edit or switch to cloud credentials.
        if user_input is not None:
            if user_input.get("setup_method") == "cloud":
                if self._manager is None:
                    self._manager = HASSTuyaBLEDeviceManager(self.hass, self._data)
                await self._manager.build_cache()
                return await self.async_step_login()
            return await self.async_step_manual()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("setup_method", default="manual"): vol.In(
                        {
                            "manual": "Edit credentials manually",
                            "cloud": "Fetch updated credentials from Tuya cloud",
                        }
                    ),
                }
            ),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle editing credentials for a manually-added device."""
        current = self.config_entry.options
        address = self.config_entry.data.get(CONF_ADDRESS, "")

        if user_input is not None:
            device_name = user_input.get(CONF_DEVICE_NAME, "").strip() or address
            return self.async_create_entry(
                title=self.config_entry.title,
                data={
                    CONF_ADDRESS: address,
                    CONF_UUID: user_input.get(CONF_UUID, "").strip(),
                    CONF_LOCAL_KEY: user_input.get(CONF_LOCAL_KEY, "").strip(),
                    CONF_DEVICE_ID: user_input.get(CONF_DEVICE_ID, "").strip(),
                    CONF_CATEGORY: user_input.get(CONF_CATEGORY, "").strip(),
                    CONF_PRODUCT_ID: user_input.get(CONF_PRODUCT_ID, "").strip(),
                    CONF_DEVICE_NAME: device_name,
                    CONF_PRODUCT_NAME: user_input.get(CONF_PRODUCT_NAME, "").strip(),
                    CONF_PRODUCT_MODEL: user_input.get(CONF_PRODUCT_MODEL, "").strip(),
                },
            )

        if user_input is None:
            user_input = {}

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE_NAME,
                        default=user_input.get(
                            CONF_DEVICE_NAME, current.get(CONF_DEVICE_NAME, "")
                        ),
                    ): str,
                    vol.Required(
                        CONF_UUID,
                        default=user_input.get(CONF_UUID, current.get(CONF_UUID, "")),
                    ): str,
                    vol.Required(
                        CONF_LOCAL_KEY,
                        default=user_input.get(
                            CONF_LOCAL_KEY, current.get(CONF_LOCAL_KEY, "")
                        ),
                    ): str,
                    vol.Required(
                        CONF_DEVICE_ID,
                        default=user_input.get(
                            CONF_DEVICE_ID, current.get(CONF_DEVICE_ID, "")
                        ),
                    ): str,
                    vol.Required(
                        CONF_CATEGORY,
                        default=user_input.get(
                            CONF_CATEGORY, current.get(CONF_CATEGORY, "")
                        ),
                    ): str,
                    vol.Required(
                        CONF_PRODUCT_ID,
                        default=user_input.get(
                            CONF_PRODUCT_ID, current.get(CONF_PRODUCT_ID, "")
                        ),
                    ): str,
                    vol.Optional(
                        CONF_PRODUCT_NAME,
                        default=user_input.get(
                            CONF_PRODUCT_NAME, current.get(CONF_PRODUCT_NAME, "")
                        ),
                    ): str,
                    vol.Optional(
                        CONF_PRODUCT_MODEL,
                        default=user_input.get(
                            CONF_PRODUCT_MODEL, current.get(CONF_PRODUCT_MODEL, "")
                        ),
                    ): str,
                }
            ),
        )

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the Tuya IOT platform login step."""
        errors: dict[str, str] = {}
        placeholders: dict[str, Any] = {}
        address: str | None = self.config_entry.data.get(CONF_ADDRESS)

        if user_input is not None:
            # Prefer a freshly-created manager (cloud switch path); fall back to
            # the manager carried by the already-loaded entry.
            manager = self._manager
            if manager is None:
                domain_data = self.hass.data.get(DOMAIN)
                if domain_data:
                    loaded = domain_data.get(self.config_entry.entry_id)
                    if loaded:
                        manager = loaded.manager

            if manager is not None:
                login_data = await _try_login(manager, user_input, errors, placeholders)
                if login_data:
                    credentials = await manager.get_device_credentials(
                        address, True, True
                    )
                    if credentials:
                        return self.async_create_entry(
                            title=self.config_entry.title,
                            data=manager.data,
                        )
                    errors["base"] = "device_not_registered"
            else:
                errors["base"] = "manager_unavailable"

        if user_input is None:
            user_input = {}
            if self._data:
                user_input.update(self._data)
            else:
                user_input.update(self.config_entry.options)
            user_input.pop(CONF_PASSWORD, None)

        def_country_name = None
        if not user_input.get(CONF_COUNTRY_CODE):
            def_country_name = await _async_get_default_country_name(self.hass)

        return _show_login_form(
            self,
            user_input,
            errors,
            placeholders,
            def_country_name,
        )


class TuyaBLEConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tuya BLE."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._data: dict[str, Any] = {}
        self._manager: HASSTuyaBLEDeviceManager | None = None
        self._get_device_info_error = False

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        if self._manager is None:
            self._manager = HASSTuyaBLEDeviceManager(self.hass, self._data)
        await self._manager.build_cache()
        self.context["title_placeholders"] = {
            "name": await get_device_readable_name(
                discovery_info,
                self._manager,
            )
        }
        return await self.async_step_login()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step — choose cloud discovery or manual entry."""
        if user_input is not None:
            if user_input.get("setup_method") == "manual":
                return await self.async_step_manual()
            if self._manager is None:
                self._manager = HASSTuyaBLEDeviceManager(self.hass, self._data)
            await self._manager.build_cache()
            return await self.async_step_login()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("setup_method", default="cloud"): vol.In(
                        {
                            "cloud": "Auto-discover via Bluetooth",
                            "manual": "Enter device credentials manually",
                        }
                    ),
                }
            ),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle manual device credential entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper().strip()
            if not _MAC_RE.match(address):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                device_name = user_input.get(CONF_DEVICE_NAME, "").strip() or address
                return self.async_create_entry(
                    title=device_name,
                    data={CONF_ADDRESS: address},
                    options={
                        CONF_ADDRESS: address,
                        CONF_UUID: user_input.get(CONF_UUID, "").strip(),
                        CONF_LOCAL_KEY: user_input.get(CONF_LOCAL_KEY, "").strip(),
                        CONF_DEVICE_ID: user_input.get(CONF_DEVICE_ID, "").strip(),
                        CONF_CATEGORY: user_input.get(CONF_CATEGORY, "").strip(),
                        CONF_PRODUCT_ID: user_input.get(CONF_PRODUCT_ID, "").strip(),
                        CONF_DEVICE_NAME: device_name,
                        CONF_PRODUCT_NAME: user_input.get(
                            CONF_PRODUCT_NAME, ""
                        ).strip(),
                        CONF_PRODUCT_MODEL: user_input.get(
                            CONF_PRODUCT_MODEL, ""
                        ).strip(),
                    },
                )

        if user_input is None:
            user_input = {}

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ADDRESS,
                        default=user_input.get(CONF_ADDRESS, ""),
                    ): str,
                    vol.Required(
                        CONF_DEVICE_NAME,
                        default=user_input.get(CONF_DEVICE_NAME, ""),
                    ): str,
                    vol.Required(
                        CONF_UUID,
                        default=user_input.get(CONF_UUID, ""),
                    ): str,
                    vol.Required(
                        CONF_LOCAL_KEY,
                        default=user_input.get(CONF_LOCAL_KEY, ""),
                    ): str,
                    vol.Required(
                        CONF_DEVICE_ID,
                        default=user_input.get(CONF_DEVICE_ID, ""),
                    ): str,
                    vol.Required(
                        CONF_CATEGORY,
                        default=user_input.get(CONF_CATEGORY, ""),
                    ): str,
                    vol.Required(
                        CONF_PRODUCT_ID,
                        default=user_input.get(CONF_PRODUCT_ID, ""),
                    ): str,
                    vol.Optional(
                        CONF_PRODUCT_NAME,
                        default=user_input.get(CONF_PRODUCT_NAME, ""),
                    ): str,
                    vol.Optional(
                        CONF_PRODUCT_MODEL,
                        default=user_input.get(CONF_PRODUCT_MODEL, ""),
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the Tuya IOT platform login step."""
        data: dict[str, Any] | None = None
        errors: dict[str, str] = {}
        placeholders: dict[str, Any] = {}

        if user_input is not None:
            data = await _try_login(
                self._manager,
                user_input,
                errors,
                placeholders,
            )
            if data:
                self._data.update(data)
                return await self.async_step_device()

        if user_input is None:
            user_input = {}
            if self._discovery_info:
                await self._manager.get_device_credentials(
                    self._discovery_info.address,
                    False,
                    True,
                )
            if self._data is None or len(self._data) == 0:
                self._manager.get_login_from_cache()
            if self._data is not None and len(self._data) > 0:
                user_input.update(self._data)
                user_input.pop(CONF_PASSWORD, None)

        def_country_name = None
        if not user_input.get(CONF_COUNTRY_CODE):
            def_country_name = await _async_get_default_country_name(self.hass)

        return _show_login_form(
            self,
            user_input,
            errors,
            placeholders,
            def_country_name,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step to pick discovered device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            discovery_info = self._discovered_devices[address]
            local_name = await get_device_readable_name(discovery_info, self._manager)
            await self.async_set_unique_id(
                discovery_info.address, raise_on_progress=False
            )
            self._abort_if_unique_id_configured()
            credentials = await self._manager.get_device_credentials(
                discovery_info.address, self._get_device_info_error, True
            )
            self._data[CONF_ADDRESS] = discovery_info.address
            if credentials is None:
                self._get_device_info_error = True
                errors["base"] = "device_not_registered"
            else:
                return self.async_create_entry(
                    title=local_name,
                    data={CONF_ADDRESS: discovery_info.address},
                    options=self._data,
                )

        if discovery := self._discovery_info:
            self._discovered_devices[discovery.address] = discovery
        else:
            current_addresses = self._async_current_ids()
            for discovery in async_discovered_service_info(self.hass):
                if (
                    discovery.address in current_addresses
                    or discovery.address in self._discovered_devices
                    or (
                        SERVICE_UUID not in (discovery.service_data or {})
                        and SERVICE_UUID not in (discovery.service_uuids or [])
                    )
                ):
                    continue
                self._discovered_devices[discovery.address] = discovery

        if not self._discovered_devices:
            return self.async_abort(reason="no_unconfigured_devices")

        def_address: str
        if user_input:
            def_address = user_input.get(CONF_ADDRESS)
        else:
            def_address = next(iter(self._discovered_devices))

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ADDRESS,
                        default=def_address,
                    ): vol.In(
                        {
                            service_info.address: await get_device_readable_name(
                                service_info,
                                self._manager,
                            )
                            for service_info in self._discovered_devices.values()
                        }
                    ),
                },
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> TuyaBLEOptionsFlow:
        """Get the options flow for this handler."""
        return TuyaBLEOptionsFlow(config_entry)
