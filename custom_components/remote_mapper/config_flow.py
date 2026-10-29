"""Config and options flow for Remote Mapper."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er, selector

from .const import (
    CONF_DEVICE_ID,
    CONF_EDIT_TIMEOUT,
    CONF_PROFILE,
    DEFAULT_EDIT_TIMEOUT,
    DOMAIN,
    MAX_EDIT_TIMEOUT,
    MIN_EDIT_TIMEOUT,
    PROFILE_AUTO,
)
from .profiles import PROFILES, compatible_profiles


def _event_entities(hass: Any, device_id: str) -> list[er.RegistryEntry]:
    registry = er.async_get(hass)
    return [
        e
        for e in er.async_entries_for_device(registry, device_id, include_disabled_entities=False)
        if e.domain == "event"
    ]


class RemoteMapperConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pick a remote."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose the device to map."""
        errors: dict[str, str] = {}
        if user_input is not None:
            device_id = user_input[CONF_DEVICE_ID]
            device = dr.async_get(self.hass).async_get(device_id)
            if device is None:
                errors[CONF_DEVICE_ID] = "device_not_found"
            elif not _event_entities(self.hass, device_id):
                errors[CONF_DEVICE_ID] = "no_event_entities"
            else:
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=device.name_by_user or device.name or "Remote",
                    data={CONF_DEVICE_ID: device_id},
                    options={CONF_PROFILE: PROFILE_AUTO, CONF_EDIT_TIMEOUT: DEFAULT_EDIT_TIMEOUT},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): selector.selector({"device": {"entity": [{"domain": "event"}]}}),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options."""
        return RemoteMapperOptionsFlow()


class RemoteMapperOptionsFlow(OptionsFlow):
    """Layout + edit timeout."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_PROFILE: user_input[CONF_PROFILE],
                    CONF_EDIT_TIMEOUT: int(user_input[CONF_EDIT_TIMEOUT]),
                }
            )

        device_id = self.config_entry.data[CONF_DEVICE_ID]
        device = dr.async_get(self.hass).async_get(device_id)
        profiles = compatible_profiles(device, _event_entities(self.hass, device_id)) if device else []
        options = [{"value": PROFILE_AUTO, "label": "Automatic"}] + [
            {"value": pid, "label": PROFILES[pid].name} for pid in profiles
        ]
        current = self.config_entry.options.get(CONF_PROFILE, PROFILE_AUTO)
        if current not in {o["value"] for o in options}:
            current = PROFILE_AUTO
        schema = vol.Schema(
            {
                vol.Required(CONF_PROFILE, default=current): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options, mode=selector.SelectSelectorMode.DROPDOWN)
                ),
                vol.Required(
                    CONF_EDIT_TIMEOUT,
                    default=self.config_entry.options.get(CONF_EDIT_TIMEOUT, DEFAULT_EDIT_TIMEOUT),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_EDIT_TIMEOUT,
                        max=MAX_EDIT_TIMEOUT,
                        step=30,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
