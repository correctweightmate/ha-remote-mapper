"""Diagnostics (Settings → Devices & services → Remote Mapper → ⋮ → Download diagnostics)."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from . import RemoteMapperConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: RemoteMapperConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    rt = entry.runtime_data
    device = dr.async_get(hass).async_get(rt.device_id)
    return {
        "options": dict(entry.options),
        "device": {
            "manufacturer": device.manufacturer if device else None,
            "model": device.model if device else None,
            "model_id": device.model_id if device else None,
            "sw_version": device.sw_version if device else None,
        },
        "event_entities": [
            {
                "entity_id": e.entity_id,
                "unique_id": e.unique_id,
                "platform": e.platform,
                "event_types": (e.capabilities or {}).get("event_types"),
            }
            for e in rt.profile.entities
        ],
        "layout": rt.profile.layout(),
        "map": rt.button_map,
        "edit_mode": rt.edit_mode,
    }
