"""Per-entry runtime state shared by services, websocket commands and listeners."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .chat_store import ChatStore
from .const import DOMAIN
from .database import Database
from .store import NotificationStore


@dataclass
class SentinelData:

    entry: ConfigEntry
    db: Database
    store: NotificationStore
    chat_store: ChatStore
    user_names: dict[str, str] = field(default_factory=dict)
    cache: dict[str, Any] = field(default_factory=dict)
    live_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    @property
    def options(self) -> dict[str, Any]:
        return self.entry.options

    @property
    def storage_dir(self) -> Path:
        return self.db.path.parent

    def live_lock(self, live_id: str) -> asyncio.Lock:
        return self.live_locks.setdefault(live_id, asyncio.Lock())


type SentinelConfigEntry = ConfigEntry[SentinelData]


def get_runtime(hass: HomeAssistant) -> SentinelData | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return entry.runtime_data
    return None


def require_runtime(hass: HomeAssistant) -> SentinelData:
    runtime = get_runtime(hass)
    if runtime is None:
        raise ServiceValidationError("Sentinel is not loaded")
    return runtime
