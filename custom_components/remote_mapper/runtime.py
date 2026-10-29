"""Per-remote runtime: listens to the remote's event entities, learns or dispatches presses."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Context, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .actions import async_execute_spec, validate_map, validate_spec
from .const import (
    CONF_DEVICE_ID,
    DEDUPE_WINDOW,
    DOMAIN,
    EVENT_BUTTON,
    EXEC_TIMEOUT,
    LOGGER,
    MAX_PENDING,
    STALE_EVENT_SECONDS,
    STORAGE_VERSION,
)
from .profiles import Profile

Listener = Callable[[dict[str, Any]], None]


class RemoteRuntime:
    """State and behaviour for one mapped remote (one config entry)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        profile: Profile,
        edit_timeout: int,
        battery_entity: str | None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.profile = profile
        self.edit_timeout = edit_timeout
        self.battery_entity = battery_entity
        self.device_id: str = entry.data[CONF_DEVICE_ID]
        self.button_map: dict[str, dict[str, Any]] = {}
        self.edit_mode = False
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._listeners: set[Listener] = set()
        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._last_seen: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()
        self._pending = 0
        self._save_lock = asyncio.Lock()

    # ---------------------------------------------------------------- lifecycle
    async def async_start(self) -> None:
        """Load the map and start listening."""
        stored = await self._store.async_load() or {}
        raw = stored.get("map") if isinstance(stored, dict) else None
        self.button_map = self._load_tolerant(raw)
        entity_ids = [e.entity_id for e in self.profile.entities]
        if entity_ids:
            self._unsub_state = async_track_state_change_event(self.hass, entity_ids, self._async_state_changed)

    def _load_tolerant(self, raw: Any) -> dict[str, dict[str, Any]]:
        """Load a stored map, dropping entries the current layout or validator no longer accepts."""
        if not isinstance(raw, dict):
            return {}
        keys = self.profile.slot_keys
        out: dict[str, dict[str, Any]] = {}
        for key, spec in raw.items():
            if key not in keys:
                LOGGER.info(
                    "%s: ignoring stored slot %s (not in the %s layout)", self.entry.title, key, self.profile.profile_id
                )
                continue
            try:
                out[key] = validate_spec(spec)
            except vol.Invalid as err:
                LOGGER.warning("%s: dropping invalid stored mapping %s: %s", self.entry.title, key, err)
        return out

    @callback
    def async_stop(self) -> None:
        """Stop listening (entry unload)."""
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        self._cancel_timer()
        self.edit_mode = False
        self._listeners.clear()

    async def async_remove_storage(self) -> None:
        """Delete the stored map (entry removal)."""
        await self._store.async_remove()

    # ---------------------------------------------------------------- listeners (websocket + entities)
    @callback
    def async_add_listener(self, listener: Listener) -> CALLBACK_TYPE:
        """Subscribe to runtime messages. Returns an unsubscribe callback."""
        self._listeners.add(listener)

        @callback
        def remove() -> None:
            self._listeners.discard(listener)

        return remove

    @callback
    def _notify(self, message: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(message)
            except Exception:  # noqa: BLE001 - one broken subscriber must not break the others
                LOGGER.exception("Remote Mapper listener failed")

    # ---------------------------------------------------------------- edit mode
    @callback
    def async_set_edit_mode(self, enabled: bool, reason: str = "user") -> None:
        """Turn learn/edit mode on or off. While on, presses are reported instead of executed."""
        self._cancel_timer()
        changed = self.edit_mode != enabled
        self.edit_mode = enabled
        if enabled:
            self._arm_timer()
        if changed:
            self._notify({"type": "edit_mode", "enabled": enabled, "reason": reason})

    @callback
    def async_touch(self) -> None:
        """Extend the edit-mode timeout (activity in the editor)."""
        if self.edit_mode:
            self._cancel_timer()
            self._arm_timer()

    @callback
    def _arm_timer(self) -> None:
        @callback
        def _expired(_now: Any) -> None:
            self._unsub_timer = None
            LOGGER.debug("%s: edit mode timed out", self.entry.title)
            self.async_set_edit_mode(False, reason="timeout")

        self._unsub_timer = async_call_later(self.hass, self.edit_timeout, _expired)

    @callback
    def _cancel_timer(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    # ---------------------------------------------------------------- map
    async def async_save_map(self, raw: Any) -> dict[str, dict[str, Any]]:
        """Validate and persist a new map. Raises vol.Invalid."""
        async with self._save_lock:
            new_map = validate_map(raw, self.profile.slot_keys)
            await self._store.async_save({"map": new_map})
            self.button_map = new_map
        self._notify({"type": "saved", "map": new_map})
        return new_map

    # ---------------------------------------------------------------- presses
    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        old = event.data["old_state"]
        if new is None or old is None:
            return
        if new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) or old.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return  # (re)connect / restore, not a press
        if new.state == old.state:
            return  # attribute-only update
        fired = dt_util.parse_datetime(new.state)
        if fired is not None and (dt_util.utcnow() - fired).total_seconds() > STALE_EVENT_SECONDS:
            return
        event_type = new.attributes.get("event_type")
        if not isinstance(event_type, str):
            return

        now = time.monotonic()
        dedupe_key = (new.entity_id, event_type)
        if now - self._last_seen.get(dedupe_key, float("-inf")) < DEDUPE_WINDOW:
            return
        self._last_seen[dedupe_key] = now

        resolved = self.profile.resolve(new.entity_id, event_type)
        if resolved is None:
            return

        if self.edit_mode:
            self.async_touch()
            self._notify({"type": "press", "slot": resolved.slot, "n": resolved.n, "event_type": event_type})
            return

        spec = self.button_map.get(resolved.slot)
        slot = self.profile.slot(resolved.slot)
        self.hass.bus.async_fire(
            EVENT_BUTTON,
            {
                "entry_id": self.entry.entry_id,
                "device_id": self.device_id,
                "slot": resolved.slot,
                "slot_label": slot.label if slot else resolved.slot,
                "n": resolved.n,
                "event_type": event_type,
                "entity_id": new.entity_id,
                "mapped": spec is not None,
            },
            context=new.context,
        )
        if spec is None:
            return
        self._dispatch(spec, resolved.n, Context(parent_id=new.context.id))

    @callback
    def _dispatch(self, spec: dict[str, Any], n: int, context: Context) -> None:
        if self._pending >= MAX_PENDING:
            LOGGER.warning("%s: %d actions already queued, dropping press", self.entry.title, self._pending)
            return
        self._pending += 1
        self.entry.async_create_background_task(
            self.hass, self._run(spec, n, context), f"{DOMAIN} {self.entry.entry_id}"
        )

    async def _run(self, spec: dict[str, Any], n: int, context: Context) -> None:
        try:
            async with self._lock:  # keep scroll detents in order
                async with asyncio.timeout(EXEC_TIMEOUT):
                    await async_execute_spec(self.hass, spec, n, context)
        except TimeoutError:
            LOGGER.warning("%s: %s timed out after %ss", self.entry.title, spec.get("kind"), EXEC_TIMEOUT)
        except (HomeAssistantError, vol.Invalid, ValueError) as err:
            LOGGER.warning("%s: %s failed: %s", self.entry.title, spec.get("kind"), err)
        finally:
            self._pending -= 1

    async def async_test(self, raw_spec: Any) -> None:
        """Run a spec once immediately (the editor's Test button). Raises on failure."""
        spec = validate_spec(raw_spec)
        async with asyncio.timeout(EXEC_TIMEOUT):
            await async_execute_spec(self.hass, spec, 1, Context())

    # ---------------------------------------------------------------- serialisation
    def as_dict(self) -> dict[str, Any]:
        """Payload for the editor."""
        return {
            "entry_id": self.entry.entry_id,
            "title": self.entry.title,
            "device_id": self.device_id,
            "battery_entity": self.battery_entity,
            "layout": self.profile.layout(),
            "map": self.button_map,
            "edit_mode": self.edit_mode,
            "edit_timeout": self.edit_timeout,
        }
