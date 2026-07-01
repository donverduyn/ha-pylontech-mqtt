"""Config flow for Pylontech Serial integration."""
import serialx
import voluptuous as vol
from homeassistant.helpers.selector import selector

from homeassistant import config_entries
from homeassistant.helpers.service_info.usb import UsbServiceInfo
from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_SERIAL_PORT, CONF_BAUD_RATE, CONF_POLL_INTERVAL,
    CONF_CONNECTION_TYPE, CONF_TCP_HOST, CONF_TCP_PORT,
    CONNECTION_TYPE_SERIAL, CONNECTION_TYPE_TCP,
    DEFAULT_BAUD_RATE, DEFAULT_POLL_INTERVAL, DEFAULT_TCP_PORT,
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Pylontech Serial."""

    VERSION = 1

    async def async_step_usb(self, discovery_info: UsbServiceInfo):
        """Handle USB discovery."""
        await self.async_set_unique_id(discovery_info.serial_number or discovery_info.device)
        self._abort_if_unique_id_configured()

        return await self.async_step_serial(usb_device=discovery_info.device)

    async def async_step_user(self, user_input=None):
        """Handle the initial step: choose connection type."""
        if user_input is not None:
            self._connection_type = user_input[CONF_CONNECTION_TYPE]
            if self._connection_type == CONNECTION_TYPE_TCP:
                return await self.async_step_tcp()
            return await self.async_step_serial()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_CONNECTION_TYPE, default=CONNECTION_TYPE_SERIAL): selector({
                    "select": {
                        "options": [CONNECTION_TYPE_SERIAL, CONNECTION_TYPE_TCP],
                        "translation_key": "connection_type",
                    }
                }),
            }),
        )

    async def async_step_serial(self, user_input=None, usb_device=None):
        """Handle serial port configuration."""
        errors = {}

        if user_input is not None:
            if user_input[CONF_SERIAL_PORT] == "Enter Manually":
                self.user_input = {
                    CONF_CONNECTION_TYPE: CONNECTION_TYPE_SERIAL,
                    CONF_POLL_INTERVAL: user_input[CONF_POLL_INTERVAL],
                    CONF_BAUD_RATE: user_input[CONF_BAUD_RATE],
                }
                return await self.async_step_manual_path()
            return self.async_create_entry(
                title="Pylontech Battery",
                data={**user_input, CONF_CONNECTION_TYPE: CONNECTION_TYPE_SERIAL},
            )

        ports = await self.hass.async_add_executor_job(serialx.list_serial_ports)
        list_of_ports = {}
        for port in ports:
            list_of_ports[port.device] = f"{port.device} - {port.product or port.device}"

        if usb_device and usb_device not in list_of_ports:
            list_of_ports[usb_device] = usb_device

        list_of_ports["Enter Manually"] = "Enter Manually"
        default_port = usb_device if usb_device else vol.UNDEFINED

        schema = vol.Schema({
            vol.Required(CONF_SERIAL_PORT, default=default_port): vol.In(list_of_ports),
            vol.Required(CONF_BAUD_RATE, default=DEFAULT_BAUD_RATE): int,
            vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): int,
        })

        return self.async_show_form(step_id="serial", data_schema=schema, errors=errors)

    async def async_step_tcp(self, user_input=None):
        """Handle TCP socket configuration."""
        errors = {}

        if user_input is not None:
            return self.async_create_entry(
                title="Pylontech Battery",
                data={
                    CONF_CONNECTION_TYPE: CONNECTION_TYPE_TCP,
                    CONF_TCP_HOST: user_input[CONF_TCP_HOST],
                    CONF_TCP_PORT: user_input[CONF_TCP_PORT],
                    CONF_POLL_INTERVAL: user_input[CONF_POLL_INTERVAL],
                },
            )

        schema = vol.Schema({
            vol.Required(CONF_TCP_HOST): str,
            vol.Required(CONF_TCP_PORT, default=DEFAULT_TCP_PORT): int,
            vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): int,
        })

        return self.async_show_form(step_id="tcp", data_schema=schema, errors=errors)

    async def async_step_manual_path(self, user_input=None):
        """Handle manual serial port entry."""
        if user_input is not None:
            self.user_input[CONF_SERIAL_PORT] = user_input[CONF_SERIAL_PORT]
            return self.async_create_entry(title="Pylontech Battery", data=self.user_input)

        return self.async_show_form(
            step_id="manual_path",
            data_schema=vol.Schema({
                vol.Required(CONF_SERIAL_PORT): str
            }),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler()


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow."""

    async def async_step_init(self, user_input=None):
        """Manage the options: choose connection type."""
        if user_input is not None:
            self._connection_type = user_input[CONF_CONNECTION_TYPE]
            if self._connection_type == CONNECTION_TYPE_TCP:
                return await self.async_step_tcp()
            return await self.async_step_serial()

        current_type = self.config_entry.options.get(
            CONF_CONNECTION_TYPE,
            self.config_entry.data.get(CONF_CONNECTION_TYPE, CONNECTION_TYPE_SERIAL),
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_CONNECTION_TYPE, default=current_type): selector({
                    "select": {
                        "options": [CONNECTION_TYPE_SERIAL, CONNECTION_TYPE_TCP],
                        "translation_key": "connection_type",
                    }
                }),
            }),
        )

    async def async_step_serial(self, user_input=None):
        """Handle serial port options."""
        errors = {}

        current_port = self.config_entry.options.get(
            CONF_SERIAL_PORT, self.config_entry.data.get(CONF_SERIAL_PORT)
        )
        current_baud = self.config_entry.options.get(
            CONF_BAUD_RATE, self.config_entry.data.get(CONF_BAUD_RATE, DEFAULT_BAUD_RATE)
        )
        current_poll = self.config_entry.options.get(
            CONF_POLL_INTERVAL, self.config_entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        )

        if user_input is not None:
            if user_input[CONF_SERIAL_PORT] == "Enter Manually":
                self.user_input = {
                    CONF_CONNECTION_TYPE: CONNECTION_TYPE_SERIAL,
                    CONF_POLL_INTERVAL: user_input[CONF_POLL_INTERVAL],
                    CONF_BAUD_RATE: user_input[CONF_BAUD_RATE],
                }
                return await self.async_step_manual_path()
            return self.async_create_entry(
                title="",
                data={**user_input, CONF_CONNECTION_TYPE: CONNECTION_TYPE_SERIAL},
            )

        ports = await self.hass.async_add_executor_job(serialx.list_serial_ports)
        list_of_ports = {}
        for port in ports:
            list_of_ports[port.device] = f"{port.device} - {port.product or port.device}"

        if current_port is not None and current_port not in list_of_ports:
            list_of_ports[current_port] = current_port

        list_of_ports["Enter Manually"] = "Enter Manually"
        default_port = current_port if current_port is not None else vol.UNDEFINED

        schema = vol.Schema({
            vol.Required(CONF_SERIAL_PORT, default=default_port): vol.In(list_of_ports),
            vol.Required(CONF_BAUD_RATE, default=current_baud): int,
            vol.Required(CONF_POLL_INTERVAL, default=current_poll): int,
        })

        return self.async_show_form(step_id="serial", data_schema=schema, errors=errors)

    async def async_step_tcp(self, user_input=None):
        """Handle TCP socket options."""
        errors = {}

        current_host = self.config_entry.options.get(
            CONF_TCP_HOST, self.config_entry.data.get(CONF_TCP_HOST, "")
        )
        current_tcp_port = self.config_entry.options.get(
            CONF_TCP_PORT, self.config_entry.data.get(CONF_TCP_PORT, DEFAULT_TCP_PORT)
        )
        current_poll = self.config_entry.options.get(
            CONF_POLL_INTERVAL, self.config_entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        )

        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_CONNECTION_TYPE: CONNECTION_TYPE_TCP,
                    CONF_TCP_HOST: user_input[CONF_TCP_HOST],
                    CONF_TCP_PORT: user_input[CONF_TCP_PORT],
                    CONF_POLL_INTERVAL: user_input[CONF_POLL_INTERVAL],
                },
            )

        schema = vol.Schema({
            vol.Required(CONF_TCP_HOST, default=current_host): str,
            vol.Required(CONF_TCP_PORT, default=current_tcp_port): int,
            vol.Required(CONF_POLL_INTERVAL, default=current_poll): int,
        })

        return self.async_show_form(step_id="tcp", data_schema=schema, errors=errors)

    async def async_step_manual_path(self, user_input=None):
        """Handle manual serial port entry."""
        if user_input is not None:
            self.user_input[CONF_SERIAL_PORT] = user_input[CONF_SERIAL_PORT]
            return self.async_create_entry(title="", data=self.user_input)

        return self.async_show_form(
            step_id="manual_path",
            data_schema=vol.Schema({
                vol.Required(CONF_SERIAL_PORT, default=self.user_input.get(CONF_SERIAL_PORT, "")): str
            }),
        )
