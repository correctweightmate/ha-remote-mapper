"""Button layouts ("profiles") that turn raw event-entity events into friendly slots.

A profile decides two things:
  * the layout shown in the UI (groups of slots), and
  * how an incoming (event entity, event_type) pair resolves to a slot plus a repeat count ``n``
    (``n`` is the number of scroll-wheel detents for rotary remotes, otherwise 1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import re
from typing import Any

from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import MAX_REPEAT

_MULTI_PRESS_RE = re.compile(r"^multi_press_(\d+)$")
_NATURAL_RE = re.compile(r"(\d+)")


def natural_key(value: str) -> list[Any]:
    """Sort key that orders button_2 before button_10."""
    return [int(part) if part.isdigit() else part.lower() for part in _NATURAL_RE.split(value)]


@dataclass(frozen=True, slots=True)
class Slot:
    """One assignable gesture."""

    key: str
    label: str
    icon: str
    scroll: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the websocket API."""
        return {"key": self.key, "label": self.label, "icon": self.icon, "scroll": self.scroll}


@dataclass(slots=True)
class Group:
    """A visual group of slots (a channel, or one physical button)."""

    key: str
    label: str
    slots: list[Slot] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the websocket API."""
        return {"key": self.key, "label": self.label, "slots": [s.as_dict() for s in self.slots]}


@dataclass(frozen=True, slots=True)
class Resolved:
    """Result of resolving an incoming event."""

    slot: str
    n: int = 1


def event_types_for(hass_states: Any, entry: er.RegistryEntry) -> list[str]:
    """Return the event types an event entity can emit (registry capabilities first, then state)."""
    caps = entry.capabilities or {}
    types = caps.get("event_types")
    if not types:
        state = hass_states.get(entry.entity_id)
        types = state.attributes.get("event_types") if state else None
    if not isinstance(types, (list, tuple)):
        return []
    return [t for t in types if isinstance(t, str) and t]


def pretty_event_type(event_type: str) -> tuple[str, str]:
    """Human label + icon for a raw event type."""
    if m := _MULTI_PRESS_RE.match(event_type):
        count = int(m.group(1))
        labels = {1: "Press", 2: "Double press", 3: "Triple press"}
        icons = {1: "mdi:gesture-tap", 2: "mdi:gesture-double-tap"}
        return labels.get(count, f"{count}× press"), icons.get(count, f"mdi:numeric-{min(count, 9)}-circle-outline")
    known = {
        "press": ("Press", "mdi:gesture-tap"),
        "initial_press": ("Press down", "mdi:gesture-tap-button"),
        "short_release": ("Short press", "mdi:gesture-tap"),
        "long_press": ("Hold", "mdi:gesture-tap-hold"),
        "long_release": ("Release after hold", "mdi:gesture-tap-hold"),
        "repeat": ("Held (repeat)", "mdi:repeat"),
        "double_press": ("Double press", "mdi:gesture-double-tap"),
        "triple_press": ("Triple press", "mdi:numeric-3-circle-outline"),
        "rotate_left": ("Rotate left", "mdi:rotate-left"),
        "rotate_right": ("Rotate right", "mdi:rotate-right"),
    }
    if event_type in known:
        return known[event_type]
    return event_type.replace("_", " ").strip().capitalize() or event_type, "mdi:gesture-tap"


def _entity_label(entry: er.RegistryEntry) -> str:
    return entry.name or entry.original_name or entry.entity_id.split(".", 1)[-1]


class Profile:
    """Base profile."""

    profile_id: str = ""
    name: str = ""

    def __init__(self, entities: Sequence[er.RegistryEntry], hass_states: Any) -> None:
        """Initialise with the device's (enabled) event entities."""
        self.entities = list(entities)
        self.hass_states = hass_states
        self.groups: list[Group] = []
        self._build()

    @staticmethod
    def matches(device: dr.DeviceEntry, entities: Sequence[er.RegistryEntry]) -> bool:
        """Return True if this profile suits the device."""
        raise NotImplementedError

    def _build(self) -> None:
        raise NotImplementedError

    def resolve(self, entity_id: str, event_type: str) -> Resolved | None:
        """Map an incoming event to a slot."""
        raise NotImplementedError

    @property
    def slot_keys(self) -> set[str]:
        """All slot keys in this layout."""
        return {s.key for g in self.groups for s in g.slots}

    def slot(self, key: str) -> Slot | None:
        """Look up a slot by key."""
        for group in self.groups:
            for slot in group.slots:
                if slot.key == key:
                    return slot
        return None

    def group_of(self, key: str) -> Group | None:
        """Group containing a slot."""
        for group in self.groups:
            if any(s.key == key for s in group.slots):
                return group
        return None

    def layout(self) -> dict[str, Any]:
        """Serialise the layout."""
        return {"profile": self.profile_id, "name": self.name, "groups": [g.as_dict() for g in self.groups]}


class GenericProfile(Profile):
    """Works with any device that exposes event entities: one group per entity, one slot per event type."""

    profile_id = "generic"
    name = "Generic (one group per button)"

    @staticmethod
    def matches(device: dr.DeviceEntry, entities: Sequence[er.RegistryEntry]) -> bool:
        return bool(entities)

    def _build(self) -> None:
        self._by_entity: dict[str, str] = {}
        for entry in sorted(self.entities, key=lambda e: natural_key(e.entity_id)):
            group = Group(key=entry.id, label=_entity_label(entry))
            for event_type in event_types_for(self.hass_states, entry):
                label, icon = pretty_event_type(event_type)
                group.slots.append(Slot(key=f"{entry.id}:{event_type}", label=label, icon=icon))
            if group.slots:
                self.groups.append(group)
                self._by_entity[entry.entity_id] = entry.id

    def resolve(self, entity_id: str, event_type: str) -> Resolved | None:
        reg_id = self._by_entity.get(entity_id)
        if reg_id is None:
            return None
        key = f"{reg_id}:{event_type}"
        return Resolved(key) if key in self.slot_keys else None


_ENDPOINT_RE = re.compile(r"-(\d+)-GenericSwitch-", re.IGNORECASE)


def _endpoint_key(entry: er.RegistryEntry) -> tuple[int, list[Any]]:
    m = _ENDPOINT_RE.search(entry.unique_id or "")
    return (int(m.group(1)) if m else 10_000, natural_key(entry.entity_id))


class BilresaScrollProfile(Profile):
    """IKEA BILRESA scroll wheel (Matter): 3 channels × scroll up / scroll down / press button."""

    profile_id = "ikea_bilresa_scroll"
    name = "IKEA BILRESA scroll wheel (3 channels)"

    _GESTURES: tuple[tuple[str, str, str, bool], ...] = (
        ("up", "Scroll up", "mdi:rotate-right", True),
        ("down", "Scroll down", "mdi:rotate-left", True),
        ("single", "Press", "mdi:gesture-tap", False),
        ("double", "Double press", "mdi:gesture-double-tap", False),
        ("triple", "Triple press", "mdi:numeric-3-circle-outline", False),
        ("hold", "Hold", "mdi:gesture-tap-hold", False),
    )

    @staticmethod
    def matches(device: dr.DeviceEntry, entities: Sequence[er.RegistryEntry]) -> bool:
        model = f"{device.model or ''} {device.model_id or ''}".lower()
        return "bilresa" in model and "scroll" in model and len(entities) == 9

    def _build(self) -> None:
        ordered = sorted(self.entities, key=_endpoint_key)
        self._index = {entry.entity_id: i for i, entry in enumerate(ordered[:9])}
        for ch in (1, 2, 3):
            group = Group(key=f"ch{ch}", label=f"Channel {ch}")
            for key, label, icon, scroll in self._GESTURES:
                group.slots.append(Slot(key=f"ch{ch}_{key}", label=label, icon=icon, scroll=scroll))
            self.groups.append(group)

    def resolve(self, entity_id: str, event_type: str) -> Resolved | None:
        idx = self._index.get(entity_id)
        if idx is None:
            return None
        ch, pos = idx // 3 + 1, idx % 3
        m = _MULTI_PRESS_RE.match(event_type)
        presses = int(m.group(1)) if m else 0
        if pos in (0, 1):
            if presses < 1:
                return None
            return Resolved(f"ch{ch}_{'up' if pos == 0 else 'down'}", max(1, min(presses, MAX_REPEAT)))
        if 1 <= presses <= 3:
            return Resolved(f"ch{ch}_{('single', 'double', 'triple')[presses - 1]}")
        if event_type == "long_press":
            return Resolved(f"ch{ch}_hold")
        return None  # long_release, 4+ presses


# Most specific first; GenericProfile is the fallback.
PROFILES: dict[str, type[Profile]] = {
    BilresaScrollProfile.profile_id: BilresaScrollProfile,
    GenericProfile.profile_id: GenericProfile,
}


def detect_profile(device: dr.DeviceEntry, entities: Sequence[er.RegistryEntry]) -> str:
    """Pick the best profile for a device."""
    for profile_id, cls in PROFILES.items():
        if cls.matches(device, entities):
            return profile_id
    return GenericProfile.profile_id


def compatible_profiles(device: dr.DeviceEntry, entities: Sequence[er.RegistryEntry]) -> list[str]:
    """Profiles that can be used for a device."""
    return [pid for pid, cls in PROFILES.items() if cls.matches(device, entities)]
