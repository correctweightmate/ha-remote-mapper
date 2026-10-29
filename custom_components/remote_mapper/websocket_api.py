"""Websocket API used by the Remote Mapper card and panel."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .runtime import RemoteRuntime


@callback
def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Register all commands."""
    for handler in (ws_list, ws_get, ws_save, ws_edit_mode, ws_subscribe, ws_test):
        websocket_api.async_register_command(hass, handler)


def _runtime(hass: HomeAssistant, entry_id: str) -> RemoteRuntime | None:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        return None
    return entry.runtime_data


def _not_found(connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Remote not found or not loaded")


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/list"})
@callback
def ws_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """List configured remotes."""
    remotes = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        loaded = entry.state is ConfigEntryState.LOADED
        rt: RemoteRuntime | None = entry.runtime_data if loaded else None
        remotes.append(
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "loaded": loaded,
                "state": entry.state.value,
                "device_id": rt.device_id if rt else entry.data.get("device_id"),
                "battery_entity": rt.battery_entity if rt else None,
                "profile": rt.profile.profile_id if rt else None,
                "edit_mode": rt.edit_mode if rt else False,
            }
        )
    connection.send_result(
        msg["id"], {"remotes": remotes, "is_admin": bool(connection.user and connection.user.is_admin)}
    )


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/get", vol.Required("entry_id"): str})
@callback
def ws_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Layout + map for one remote."""
    if (rt := _runtime(hass, msg["entry_id"])) is None:
        _not_found(connection, msg)
        return
    connection.send_result(msg["id"], rt.as_dict())


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/save", vol.Required("entry_id"): str, vol.Required("map"): dict}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Validate + persist a map."""
    if (rt := _runtime(hass, msg["entry_id"])) is None:
        _not_found(connection, msg)
        return
    try:
        saved = await rt.async_save_map(msg["map"])
    except vol.Invalid as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    connection.send_result(msg["id"], {"map": saved})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/edit_mode", vol.Required("entry_id"): str, vol.Required("enabled"): bool}
)
@websocket_api.require_admin
@callback
def ws_edit_mode(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Enter/leave learn mode."""
    if (rt := _runtime(hass, msg["entry_id"])) is None:
        _not_found(connection, msg)
        return
    rt.async_set_edit_mode(msg["enabled"])
    connection.send_result(msg["id"], {"edit_mode": rt.edit_mode})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe", vol.Required("entry_id"): str})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Stream presses (in edit mode), edit-mode changes and saves."""
    if (rt := _runtime(hass, msg["entry_id"])) is None:
        _not_found(connection, msg)
        return

    @callback
    def forward(message: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], message))

    connection.subscriptions[msg["id"]] = rt.async_add_listener(forward)
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/test", vol.Required("entry_id"): str, vol.Required("spec"): dict}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_test(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Run a spec once."""
    if (rt := _runtime(hass, msg["entry_id"])) is None:
        _not_found(connection, msg)
        return
    rt.async_touch()
    try:
        await rt.async_test(msg["spec"])
    except vol.Invalid as err:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, str(err))
        return
    except (HomeAssistantError, TimeoutError) as err:
        connection.send_error(msg["id"], websocket_api.ERR_HOME_ASSISTANT_ERROR, str(err) or type(err).__name__)
        return
    connection.send_result(msg["id"])
