"""Remote Mapper: assign actions to any event-based remote by pressing its buttons."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import panel_custom
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEVICE_ID,
    CONF_EDIT_TIMEOUT,
    CONF_PROFILE,
    DATA_SETUP_DONE,
    DEFAULT_EDIT_TIMEOUT,
    DOMAIN,
    FRONTEND_FILE,
    LOGGER,
    PANEL_COMPONENT,
    PANEL_URL,
    PROFILE_AUTO,
    STATIC_URL,
    STORAGE_VERSION,
    VERSION,
)
from .profiles import PROFILES, GenericProfile, detect_profile
from .runtime import RemoteRuntime
from .websocket_api import async_register_websocket_commands

PLATFORMS: list[Platform] = [Platform.SWITCH]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type RemoteMapperConfigEntry = ConfigEntry[RemoteRuntime]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the websocket API, the frontend bundle and the sidebar panel (once)."""
    if hass.data.get(DATA_SETUP_DONE):
        return True
    hass.data[DATA_SETUP_DONE] = True

    async_register_websocket_commands(hass)

    frontend_dir = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, str(frontend_dir), cache_headers=False)])
    module_url = f"{STATIC_URL}/{FRONTEND_FILE}?v={VERSION}"
    # Loads the card on every dashboard so `type: custom:remote-mapper-card` works without a resource entry.
    add_extra_js_url(hass, module_url)

    async def _register_panel(_event: Event | None = None) -> None:
        try:
            await panel_custom.async_register_panel(
                hass,
                frontend_url_path=PANEL_URL,
                webcomponent_name=PANEL_COMPONENT,
                sidebar_title="Remote Mapper",
                sidebar_icon="mdi:remote",
                module_url=module_url,
                require_admin=True,
                config={},
            )
        except ValueError as err:  # already registered (e.g. integration reloaded)
            LOGGER.debug("Panel not registered: %s", err)

    if hass.state is CoreState.running:
        await _register_panel()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _register_panel)
    return True


def _event_entities(hass: HomeAssistant, device_id: str) -> list[er.RegistryEntry]:
    registry = er.async_get(hass)
    return [
        e
        for e in er.async_entries_for_device(registry, device_id, include_disabled_entities=False)
        if e.domain == "event"
    ]


def _battery_entity(hass: HomeAssistant, device_id: str) -> str | None:
    registry = er.async_get(hass)
    for entry in er.async_entries_for_device(registry, device_id, include_disabled_entities=False):
        if entry.domain == "sensor" and (entry.device_class or entry.original_device_class) == "battery":
            return entry.entity_id
    return None


async def async_setup_entry(hass: HomeAssistant, entry: RemoteMapperConfigEntry) -> bool:
    """Set up one mapped remote."""
    device_id: str = entry.data[CONF_DEVICE_ID]
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ConfigEntryError(f"Device {device_id} no longer exists; remove this Remote Mapper entry")

    entities = _event_entities(hass, device_id)
    if not entities:
        raise ConfigEntryNotReady(f"{device.name_by_user or device.name} has no enabled event entities yet")

    profile_id = entry.options.get(CONF_PROFILE, PROFILE_AUTO)
    if profile_id == PROFILE_AUTO or profile_id not in PROFILES:
        profile_id = detect_profile(device, entities)
    profile_cls = PROFILES[profile_id]
    if not profile_cls.matches(device, entities):
        LOGGER.warning("%s: profile %s no longer fits this device, using generic", entry.title, profile_id)
        profile_cls = GenericProfile

    profile = profile_cls(entities, hass.states)
    runtime = RemoteRuntime(
        hass,
        entry,
        profile,
        int(entry.options.get(CONF_EDIT_TIMEOUT, DEFAULT_EDIT_TIMEOUT)),
        _battery_entity(hass, device_id),
    )
    await runtime.async_start()
    entry.runtime_data = runtime

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Reload when the remote gains/loses event entities (e.g. firmware update adds buttons).
    tracked = {e.entity_id for e in entities}

    @callback
    def _registry_changed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        entity_id = event.data["entity_id"]
        if not entity_id.startswith("event."):
            return
        if event.data["action"] == "remove":
            relevant = entity_id in tracked
        else:
            reg = er.async_get(hass).async_get(entity_id)
            relevant = entity_id in tracked or (reg is not None and reg.device_id == device_id)
        if relevant:
            LOGGER.debug("%s: event entities changed, reloading", entry.title)
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _registry_changed))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: RemoteMapperConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RemoteMapperConfigEntry) -> bool:
    """Unload a mapped remote."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: RemoteMapperConfigEntry) -> None:
    """Delete the stored button map when the entry is deleted."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
