"""Edit-mode switch: while on, the remote is paused and presses are captured by the editor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device import async_device_info_to_link_from_device_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RemoteMapperConfigEntry
from .runtime import RemoteRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RemoteMapperConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the edit-mode switch."""
    async_add_entities([EditModeSwitch(hass, entry.runtime_data)])


class EditModeSwitch(SwitchEntity):
    """Turns learn/edit mode on and off."""

    _attr_has_entity_name = True
    _attr_translation_key = "edit_mode"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, runtime: RemoteRuntime) -> None:
        self._runtime = runtime
        self._attr_unique_id = f"{runtime.entry.entry_id}_edit_mode"
        self._attr_device_info = async_device_info_to_link_from_device_id(hass, runtime.device_id)

    @property
    def is_on(self) -> bool:
        return self._runtime.edit_mode

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._runtime.async_set_edit_mode(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._runtime.async_set_edit_mode(False)

    async def async_added_to_hass(self) -> None:
        @callback
        def _on_message(message: dict[str, Any]) -> None:
            if message.get("type") == "edit_mode":
                self.async_write_ha_state()

        self.async_on_remove(self._runtime.async_add_listener(_on_message))
