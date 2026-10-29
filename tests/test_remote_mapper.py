"""End-to-end tests for Remote Mapper."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.remote_mapper.actions import validate_map, validate_spec
from custom_components.remote_mapper.const import DOMAIN, EVENT_BUTTON

E = "event.bilresa_scroll_wheel_button_{}"


async def _setup(hass: HomeAssistant, device_id: str) -> MockConfigEntry:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"device_id": device_id})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done(wait_background_tasks=True)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.state is config_entries.ConfigEntryState.LOADED
    return entry


# --------------------------------------------------------------------------- validation
def test_validate_spec_rules() -> None:
    assert validate_spec({"kind": "toggle", "entity_id": "light.a"})["entity_id"] == ["light.a"]
    assert validate_spec({"kind": "dim", "entity_id": ["light.a"]})["step"] == 10
    with pytest.raises(vol.Invalid):
        validate_spec({"kind": "toggle", "entity_id": []})
    with pytest.raises(vol.Invalid):
        validate_spec({"kind": "brighten", "entity_id": ["switch.a"]})
    with pytest.raises(vol.Invalid):
        validate_spec({"kind": "custom"})
    with pytest.raises(vol.Invalid):
        validate_spec({"kind": "custom", "service": "light.turn_on", "data": {"x": "{{ states('a') }}"}})
    spec = validate_spec({"kind": "custom", "service": "light.turn_on", "data": {"color_name": "red"}, "step": 5})
    assert "step" not in spec and spec["data"] == {"color_name": "red"}
    assert validate_spec({"kind": "toggle", "entity_id": ["light.a"], "label": "a {{x}}"})["label"] == "a x"
    with pytest.raises(vol.Invalid):
        validate_map({"nope": {"kind": "toggle", "entity_id": ["light.a"]}}, {"ch1_up"})
    assert validate_map({"ch1_up": {"kind": "none"}}, {"ch1_up"}) == {}


# --------------------------------------------------------------------------- flow
async def test_flow_rejects_device_without_buttons(hass: HomeAssistant, base: None) -> None:
    source = MockConfigEntry(domain="demo")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("demo", "x")}, name="Lamp"
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"device_id": device.id})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"device_id": "no_event_entities"}


async def test_flow_duplicate_aborts(hass: HomeAssistant, base: None, bilresa: dict[str, Any]) -> None:
    await _setup(hass, bilresa["device"].id)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"device_id": bilresa["device"].id})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --------------------------------------------------------------------------- runtime
async def test_bilresa_layout_and_dispatch(hass: HomeAssistant, base: None, bilresa: dict[str, Any], press) -> None:
    entry = await _setup(hass, bilresa["device"].id)
    rt = entry.runtime_data
    assert rt.profile.profile_id == "ikea_bilresa_scroll"
    assert rt.battery_entity == bilresa["battery"]
    assert [g["label"] for g in rt.profile.layout()["groups"]] == ["Channel 1", "Channel 2", "Channel 3"]

    # Edit-mode switch is linked to the remote's device.
    ent = er.async_get(hass).async_get("switch.bilresa_scroll_wheel_remote_mapper_edit_mode")
    assert ent is not None and ent.device_id == bilresa["device"].id

    toggles = async_mock_service(hass, "homeassistant", "toggle")
    light_on = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set("light.bed", "on", {"brightness": 128})
    await rt.async_save_map(
        {
            "ch2_double": {"kind": "toggle", "entity_id": ["light.bed"]},
            "ch1_up": {"kind": "brighten", "entity_id": ["light.bed"], "step": 5},
            "ch1_down": {"kind": "dim", "entity_id": ["light.bed"], "step": 5},
        }
    )

    fired: list[Any] = []
    hass.bus.async_listen(EVENT_BUTTON, fired.append)

    press(E.format(6), "multi_press_2")  # ch2 press button, double
    press(E.format(6), "multi_press_2")  # duplicate within 250 ms -> ignored
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(toggles) == 1 and toggles[0].data["entity_id"] == ["light.bed"]
    assert fired[0].data["slot"] == "ch2_double" and fired[0].data["mapped"] is True

    press(E.format(1), "multi_press_3")  # ch1 scroll up, 3 detents
    await hass.async_block_till_done(wait_background_tasks=True)
    assert light_on[-1].data["brightness_step_pct"] == 15

    press(E.format(2), "multi_press_4")  # ch1 scroll down, 4 detents: 50% - 20 = 30%
    await hass.async_block_till_done(wait_background_tasks=True)
    assert light_on[-1].data["brightness_pct"] == 30

    # long_release and unmapped slots do nothing.
    press(E.format(9), "long_release")
    press(E.format(9), "multi_press_1")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(toggles) == 1
    assert fired[-1].data["slot"] == "ch3_single" and fired[-1].data["mapped"] is False


async def test_ignores_restore_and_stale_states(
    hass: HomeAssistant, base: None, bilresa: dict[str, Any], press
) -> None:
    entry = await _setup(hass, bilresa["device"].id)
    toggles = async_mock_service(hass, "homeassistant", "toggle")
    await entry.runtime_data.async_save_map({"ch1_single": {"kind": "toggle", "entity_id": ["light.x"]}})

    hass.states.async_set(E.format(3), "unavailable", {})
    await hass.async_block_till_done(wait_background_tasks=True)
    press(E.format(3), "multi_press_1")  # unavailable -> timestamp = reconnect, not a press
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not toggles

    old = (dt_util.utcnow() - timedelta(minutes=5)).isoformat()
    press(E.format(3), "multi_press_1", when=old)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not toggles

    press(E.format(3), "multi_press_1")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(toggles) == 1


async def test_edit_mode_learns_and_times_out(hass: HomeAssistant, base: None, bilresa: dict[str, Any], press) -> None:
    entry = await _setup(hass, bilresa["device"].id)
    rt = entry.runtime_data
    toggles = async_mock_service(hass, "homeassistant", "toggle")
    await rt.async_save_map({"ch1_single": {"kind": "toggle", "entity_id": ["light.x"]}})
    messages: list[dict[str, Any]] = []
    rt.async_add_listener(messages.append)

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.bilresa_scroll_wheel_remote_mapper_edit_mode"}, blocking=True
    )
    assert rt.edit_mode
    assert hass.states.get("switch.bilresa_scroll_wheel_remote_mapper_edit_mode").state == "on"

    press(E.format(3), "multi_press_1")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not toggles, "presses must not run while editing"
    assert {"type": "press", "slot": "ch1_single", "n": 1, "event_type": "multi_press_1"} in messages

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=601))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not rt.edit_mode
    assert messages[-1] == {"type": "edit_mode", "enabled": False, "reason": "timeout"}
    assert hass.states.get("switch.bilresa_scroll_wheel_remote_mapper_edit_mode").state == "off"


async def test_generic_profile(hass: HomeAssistant, base: None, press) -> None:
    source = MockConfigEntry(domain="hue")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id,
        identifiers={("hue", "dimmer")},
        manufacturer="Signify",
        model="RWL022",
        name="Dimmer",
    )
    ent_reg = er.async_get(hass)
    for i in (1, 2, 10):
        ent = ent_reg.async_get_or_create(
            "event",
            "hue",
            f"dimmer-{i}",
            device_id=device.id,
            config_entry=source,
            suggested_object_id=f"dimmer_button_{i}",
            capabilities={"event_types": ["initial_press", "short_release", "long_press"]},
        )
        hass.states.async_set(ent.entity_id, "2026-01-01T00:00:00+00:00", {"event_type": "initial_press"})
    entry = await _setup(hass, device.id)
    rt = entry.runtime_data
    layout = rt.profile.layout()
    assert layout["profile"] == "generic"
    assert [len(g["slots"]) for g in layout["groups"]] == [3, 3, 3]
    assert layout["groups"][2]["key"] == ent_reg.async_get("event.dimmer_button_10").id  # natural sort
    slot = f"{ent_reg.async_get('event.dimmer_button_2').id}:short_release"
    scene = async_mock_service(hass, "scene", "turn_on")
    await rt.async_save_map({slot: {"kind": "scene", "entity_id": ["scene.night"]}})
    press("event.dimmer_button_2", "short_release")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(scene) == 1


# --------------------------------------------------------------------------- websocket
async def test_websocket_api(hass: HomeAssistant, base: None, bilresa: dict[str, Any], hass_ws_client, press) -> None:
    entry = await _setup(hass, bilresa["device"].id)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "remote_mapper/list"})
    res = await client.receive_json()
    assert res["success"] and res["result"]["remotes"][0]["entry_id"] == entry.entry_id

    await client.send_json_auto_id({"type": "remote_mapper/subscribe", "entry_id": entry.entry_id})
    sub = await client.receive_json()
    assert sub["success"]
    sub_id = sub["id"]

    await client.send_json_auto_id({"type": "remote_mapper/edit_mode", "entry_id": entry.entry_id, "enabled": True})
    msg = await client.receive_json()  # event and result may arrive in either order
    msgs = [msg, await client.receive_json()]
    assert any(m.get("type") == "event" and m["id"] == sub_id and m["event"]["enabled"] for m in msgs)

    press(E.format(9), "long_press")
    await hass.async_block_till_done(wait_background_tasks=True)
    ev = await client.receive_json()
    assert ev["event"] == {"type": "press", "slot": "ch3_hold", "n": 1, "event_type": "long_press"}

    await client.send_json_auto_id(
        {
            "type": "remote_mapper/save",
            "entry_id": entry.entry_id,
            "map": {"ch3_hold": {"kind": "scene", "entity_id": ["scene.night"], "label": "Night"}},
        }
    )
    msgs = [await client.receive_json(), await client.receive_json()]
    result = next(m for m in msgs if m["type"] == "result")
    assert result["success"] and result["result"]["map"]["ch3_hold"]["kind"] == "scene"

    await client.send_json_auto_id(
        {"type": "remote_mapper/save", "entry_id": entry.entry_id, "map": {"bogus": {"kind": "toggle"}}}
    )
    res = await client.receive_json()
    assert not res["success"] and res["error"]["code"] == "invalid_format"

    await client.send_json_auto_id({"type": "remote_mapper/get", "entry_id": entry.entry_id})
    res = await client.receive_json()
    assert res["result"]["map"]["ch3_hold"]["entity_id"] == ["scene.night"]
    assert res["result"]["edit_mode"] is True

    turned = async_mock_service(hass, "scene", "turn_on")
    await client.send_json_auto_id(
        {
            "type": "remote_mapper/test",
            "entry_id": entry.entry_id,
            "spec": {"kind": "scene", "entity_id": ["scene.night"]},
        }
    )
    res = await client.receive_json()
    assert res["success"] and len(turned) == 1

    await client.send_json_auto_id({"type": "remote_mapper/get", "entry_id": "nope"})
    res = await client.receive_json()
    assert not res["success"] and res["error"]["code"] == "not_found"


async def test_websocket_requires_admin_for_writes(
    hass: HomeAssistant, base: None, bilresa: dict[str, Any], hass_ws_client, hass_read_only_access_token
) -> None:
    entry = await _setup(hass, bilresa["device"].id)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id({"type": "remote_mapper/save", "entry_id": entry.entry_id, "map": {}})
    res = await client.receive_json()
    assert not res["success"] and res["error"]["code"] == "unauthorized"
    await client.send_json_auto_id({"type": "remote_mapper/get", "entry_id": entry.entry_id})
    res = await client.receive_json()
    assert res["success"]


# --------------------------------------------------------------------------- persistence + lifecycle
async def test_map_persists_and_entry_removal(
    hass: HomeAssistant, base: None, bilresa: dict[str, Any], hass_storage: dict[str, Any]
) -> None:
    entry = await _setup(hass, bilresa["device"].id)
    await entry.runtime_data.async_save_map({"ch1_single": {"kind": "toggle", "entity_id": ["light.x"]}})
    key = f"{DOMAIN}.{entry.entry_id}"
    assert hass_storage[key]["data"]["map"]["ch1_single"]["kind"] == "toggle"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.button_map["ch1_single"]["entity_id"] == ["light.x"]

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert key not in hass_storage


async def test_options_flow_switches_profile(hass: HomeAssistant, base: None, bilresa: dict[str, Any]) -> None:
    entry = await _setup(hass, bilresa["device"].id)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"profile": "generic", "edit_timeout": 120}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.profile.profile_id == "generic"
    assert entry.runtime_data.edit_timeout == 120
    assert len(entry.runtime_data.profile.groups) == 9


async def test_queue_limit(hass: HomeAssistant, base: None, bilresa: dict[str, Any], press) -> None:
    entry = await _setup(hass, bilresa["device"].id)
    gate = asyncio.Event()
    calls: list[Any] = []

    async def slow(call: Any) -> None:
        calls.append(call)
        await gate.wait()

    hass.services.async_register("homeassistant", "toggle", slow)
    await entry.runtime_data.async_save_map({"ch1_up": {"kind": "toggle", "entity_id": ["light.x"]}})
    for _ in range(15):
        entry.runtime_data._last_seen.clear()  # bypass dedupe to flood
        press(E.format(1), "multi_press_1")
        for _ in range(3):
            await asyncio.sleep(0)
    assert entry.runtime_data._pending == 10
    gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls) == 10 and entry.runtime_data._pending == 0


async def test_diagnostics(hass: HomeAssistant, base: None, bilresa: dict[str, Any]) -> None:
    from custom_components.remote_mapper.diagnostics import async_get_config_entry_diagnostics

    entry = await _setup(hass, bilresa["device"].id)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["device"]["model"] == "BILRESA scroll wheel"
    assert len(diag["event_entities"]) == 9
    assert diag["layout"]["profile"] == "ikea_bilresa_scroll"
