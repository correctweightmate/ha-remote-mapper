"""Action specs: validation and execution."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceNotFound
import homeassistant.helpers.config_validation as cv

from .const import MAX_MAP_ENTRIES, MAX_REPEAT

# kind -> (allowed entity domains or None for "any", needs entities)
KINDS: dict[str, tuple[frozenset[str] | None, bool]] = {
    "toggle": (None, True),
    "on": (None, True),
    "off": (None, True),
    "brighten": (frozenset({"light"}), True),
    "dim": (frozenset({"light"}), True),
    "set_brightness": (frozenset({"light"}), True),
    "scene": (frozenset({"scene"}), True),
    "script": (frozenset({"script"}), True),
    "play_pause": (frozenset({"media_player"}), True),
    "volume_up": (frozenset({"media_player"}), True),
    "volume_down": (frozenset({"media_player"}), True),
    "custom": (None, False),
}

_SIMPLE: dict[str, tuple[str, str]] = {
    "toggle": ("homeassistant", "toggle"),
    "on": ("homeassistant", "turn_on"),
    "off": ("homeassistant", "turn_off"),
    "scene": ("scene", "turn_on"),
    "script": ("script", "turn_on"),
    "play_pause": ("media_player", "media_play_pause"),
}

_SERVICE_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

SPEC_SCHEMA = vol.Schema(
    {
        vol.Required("kind"): vol.In(list(KINDS)),
        vol.Optional("entity_id", default=list): vol.All(cv.ensure_list, [cv.entity_id], vol.Length(max=50)),
        vol.Optional("step"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
        vol.Optional("brightness_pct"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
        vol.Optional("service"): vol.All(str, vol.Match(_SERVICE_RE)),
        vol.Optional("data"): vol.All(dict, vol.Length(max=50)),
        vol.Optional("label"): vol.All(str, vol.Length(max=120)),
    },
    extra=vol.REMOVE_EXTRA,
)


def validate_spec(raw: Any) -> dict[str, Any]:
    """Validate one action spec. Raises vol.Invalid."""
    spec: dict[str, Any] = SPEC_SCHEMA(raw)
    kind = spec["kind"]
    domains, needs_entities = KINDS[kind]
    entities: list[str] = list(dict.fromkeys(spec["entity_id"]))  # de-dupe, keep order
    spec["entity_id"] = entities
    if needs_entities and not entities:
        raise vol.Invalid(f"'{kind}' needs at least one entity")
    if domains is not None:
        bad = [e for e in entities if e.split(".", 1)[0] not in domains]
        if bad:
            raise vol.Invalid(f"'{kind}' cannot target {', '.join(bad)}")
    if kind in ("brighten", "dim"):
        spec.setdefault("step", 10)
    else:
        spec.pop("step", None)
    if kind == "set_brightness":
        spec.setdefault("brightness_pct", 50)
    else:
        spec.pop("brightness_pct", None)
    if kind == "custom":
        if "service" not in spec:
            raise vol.Invalid("'custom' needs a service, e.g. light.turn_on")
        data = spec.get("data") or {}
        _reject_templates(data)
        spec["data"] = data
    else:
        spec.pop("service", None)
        spec.pop("data", None)
    if "label" in spec:
        spec["label"] = spec["label"].replace("{", "").replace("}", "").strip()
    return spec


def _reject_templates(value: Any, depth: int = 0) -> None:
    """Custom data is passed as-is; refuse template syntax so it is never mistaken for one."""
    if depth > 8:
        raise vol.Invalid("data is nested too deeply")
    if isinstance(value, str):
        if "{{" in value or "{%" in value:
            raise vol.Invalid("templates are not supported in custom data")
    elif isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise vol.Invalid("data keys must be strings")
            _reject_templates(v, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _reject_templates(v, depth + 1)
    elif value is not None and not isinstance(value, (int, float, bool)):
        raise vol.Invalid("unsupported value in data")


def validate_map(raw: Any, slot_keys: set[str]) -> dict[str, dict[str, Any]]:
    """Validate a whole button map against the layout's slot keys."""
    if not isinstance(raw, dict):
        raise vol.Invalid("map must be an object")
    if len(raw) > MAX_MAP_ENTRIES:
        raise vol.Invalid("too many entries")
    out: dict[str, dict[str, Any]] = {}
    for key, spec in raw.items():
        if key not in slot_keys:
            raise vol.Invalid(f"unknown slot '{key}'")
        if spec is None or (isinstance(spec, dict) and spec.get("kind") == "none"):
            continue
        try:
            out[key] = validate_spec(spec)
        except vol.Invalid as err:
            raise vol.Invalid(f"{key}: {err}") from err
    return out


async def async_execute_spec(hass: HomeAssistant, spec: dict[str, Any], n: int, context: Context | None) -> None:
    """Run one mapped action. ``n`` = scroll detents (1 for presses)."""
    n = max(1, min(int(n or 1), MAX_REPEAT))
    kind = spec.get("kind")
    entities = [e for e in spec.get("entity_id") or [] if isinstance(e, str)]

    async def call(domain: str, service: str, data: dict[str, Any]) -> None:
        await hass.services.async_call(domain, service, data, blocking=True, context=context)

    if kind in _SIMPLE:
        if entities:
            domain, service = _SIMPLE[kind]
            await call(domain, service, {ATTR_ENTITY_ID: entities})
    elif kind == "set_brightness":
        if entities:
            await call(
                "light", "turn_on", {ATTR_ENTITY_ID: entities, "brightness_pct": int(spec.get("brightness_pct", 50))}
            )
    elif kind == "brighten":
        if entities:
            # brightness_step_pct also turns an off light on at step %.
            step = min(100, int(spec.get("step", 10)) * n)
            await call("light", "turn_on", {ATTR_ENTITY_ID: entities, "brightness_step_pct": step})
    elif kind == "dim":
        step = int(spec.get("step", 10)) * n
        for entity_id in entities:
            state = hass.states.get(entity_id)
            if state is None or state.state != "on":
                continue  # never switch an off light on by dimming
            current = round((state.attributes.get("brightness") or 0) * 100 / 255)
            await call("light", "turn_on", {ATTR_ENTITY_ID: entity_id, "brightness_pct": max(1, current - step)})
    elif kind in ("volume_up", "volume_down"):
        if entities:
            for _ in range(n):
                await call("media_player", kind, {ATTR_ENTITY_ID: entities})
    elif kind == "custom":
        service = spec.get("service") or ""
        domain, _, name = service.partition(".")
        if not domain or not name or not hass.services.has_service(domain, name):
            raise ServiceNotFound(domain or "?", name or "?")
        data = dict(spec.get("data") or {})
        if entities and ATTR_ENTITY_ID not in data:
            data[ATTR_ENTITY_ID] = entities
        await call(domain, name, data)
