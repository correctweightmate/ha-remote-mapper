"""Constants for Remote Mapper."""

from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "remote_mapper"
VERSION: Final = "1.0.0"
LOGGER = logging.getLogger(__package__)

CONF_DEVICE_ID: Final = "device_id"
CONF_PROFILE: Final = "profile"
CONF_EDIT_TIMEOUT: Final = "edit_timeout"

PROFILE_AUTO: Final = "auto"
DEFAULT_EDIT_TIMEOUT: Final = 600  # seconds; edit mode switches itself off after this much inactivity
MIN_EDIT_TIMEOUT: Final = 60
MAX_EDIT_TIMEOUT: Final = 3600

# Some remotes (e.g. IKEA BILRESA over Matter) report the same event twice ~1 ms apart.
DEDUPE_WINDOW: Final = 0.25  # seconds
# Ignore event-entity states older than this (restored state after a reconnect is not a press).
STALE_EVENT_SECONDS: Final = 15
MAX_REPEAT: Final = 10  # cap on scroll detents / repeated volume steps
MAX_PENDING: Final = 10  # queued actions per remote before new presses are dropped
EXEC_TIMEOUT: Final = 15  # seconds per mapped action
MAX_MAP_ENTRIES: Final = 300

STORAGE_VERSION: Final = 1

STATIC_URL: Final = "/remote_mapper_static"
FRONTEND_FILE: Final = "remote-mapper.js"
PANEL_URL: Final = "remote-mapper"
PANEL_COMPONENT: Final = "remote-mapper-panel"

EVENT_BUTTON: Final = "remote_mapper_button"

DATA_SETUP_DONE: Final = f"{DOMAIN}_setup_done"
