"""Fixtures for Remote Mapper tests."""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import MockConfigEntry

pytest_plugins = "pytest_homeassistant_custom_component"

PRESS_TYPES = ["multi_press_1", "multi_press_2", "multi_press_3", "long_press", "long_release"]
SCROLL_TYPES = [f"multi_press_{i}" for i in range(1, 9)]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> Generator[None]:
    """Allow loading custom_components/remote_mapper."""
    yield


@pytest.fixture(autouse=True)
def mock_frontend() -> Generator[dict[str, Any]]:
    """frontend/panel_custom need the compiled frontend package; stub just what we call."""
    with (
        patch("custom_components.remote_mapper.add_extra_js_url") as js,
        patch("custom_components.remote_mapper.panel_custom.async_register_panel", new=AsyncMock()) as panel,
    ):
        yield {"js": js, "panel": panel}


@pytest.fixture
async def base(hass: HomeAssistant) -> None:
    """Mark the frontend-only dependencies as loaded and set up http + websocket for real."""
    hass.config.components.update({"frontend", "panel_custom"})
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "websocket_api", {})


def _now() -> str:
    return dt_util.utcnow().isoformat(timespec="milliseconds")


@pytest.fixture
def bilresa(hass: HomeAssistant) -> dict[str, Any]:
    """A fake IKEA BILRESA scroll wheel (Matter): 9 event entities, plus a battery sensor."""
    source = MockConfigEntry(domain="matter", title="Matter")
    source.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={("matter", "bilresa-1")},
        manufacturer="IKEA of Sweden",
        model="BILRESA scroll wheel",
        name="BILRESA scroll wheel",
    )
    entity_ids: list[str] = []
    # Register in a scrambled order to prove endpoint-based ordering works.
    for i in (3, 1, 2, 6, 4, 5, 9, 7, 8):
        types = PRESS_TYPES if i % 3 == 0 else SCROLL_TYPES
        entry = ent_reg.async_get_or_create(
            "event",
            "matter",
            f"68F4-0000001C-MatterNodeDevice-{i}-GenericSwitch-59-1",
            device_id=device.id,
            config_entry=source,
            suggested_object_id=f"bilresa_scroll_wheel_button_{i}",
            capabilities={"event_types": types},
            original_name=f"Button ({i})",
        )
        hass.states.async_set(
            entry.entity_id, "2026-01-01T00:00:00.000+00:00", {"event_types": types, "event_type": types[0]}
        )
        entity_ids.append(entry.entity_id)
    battery = ent_reg.async_get_or_create(
        "sensor",
        "matter",
        "68F4-battery",
        device_id=device.id,
        config_entry=source,
        suggested_object_id="bilresa_scroll_wheel_battery",
        original_device_class="battery",
    )
    hass.states.async_set(battery.entity_id, "42")
    return {"device": device, "battery": battery.entity_id, "entity_ids": sorted(entity_ids)}


@pytest.fixture
def press(hass: HomeAssistant):
    """Simulate a button event on an event entity."""

    counter = {"ms": 0}

    def _press(entity_id: str, event_type: str, when: str | None = None) -> None:
        # Every real event carries a fresh timestamp; step 2 ms so consecutive presses differ.
        counter["ms"] += 2
        if when is None:
            when = (dt_util.utcnow() + timedelta(milliseconds=counter["ms"])).isoformat(timespec="milliseconds")
        old = hass.states.get(entity_id)
        attrs = dict(old.attributes) if old else {}
        attrs["event_type"] = event_type
        hass.states.async_set(entity_id, when, attrs, force_update=True)

    return _press
